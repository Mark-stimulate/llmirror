import asyncio
import csv
import hashlib
import io
import ipaddress
import json
import logging
import platform
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, field_validator
from public_transport import public_transport

from benchmark import CASES, PROMPTS, completion_url, measured_request, percentile, redact, performance_prompt
from store import Store, now, password_hash, password_ok
from presets import SCENARIOS, apply_scenario
from model_catalog import catalog_urls, catalog_models, provider_error
from quality_cases import VERSION as QUALITY_VERSION

CN = timezone(timedelta(hours=8))
VERSION = "1.2.3"
ACTIVE = ("queued", "running", "cancelling")


class BrowserAssets(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # On Windows, registry MIME associations can mislabel .js as text/plain.
        # nosniff then prevents the browser from executing the entire login UI.
        suffix = Path(path).suffix.lower()
        media = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml"}
        if suffix in media:
            response.headers["Content-Type"] = media[suffix]
        response.headers["Cache-Control"] = "no-store"
        return response


def day_key():
    return datetime.now(CN).strftime("%Y-%m-%d")


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=6, max_length=128)


class EndpointInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=2048)
    tag: str = Field(default="", max_length=80)
    platform: str = Field(default="天翼云", min_length=1, max_length=40)
    schedule_minutes: int = Field(default=0, ge=0, le=1440)
    schedule_scenario: str = Field(default="probe", pattern=r"^(probe|chat|knowledge|workflow)$")

    @field_validator("base_url")
    @classmethod
    def url_valid(cls, value):
        completion_url(value)
        return value.strip().rstrip("/")

    @field_validator("schedule_minutes")
    @classmethod
    def interval_valid(cls, value):
        if 0 < value < 5:
            raise ValueError("采样间隔至少为5分钟")
        return value


class ModelSourceInput(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=2048)
    source_id: int | None = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        completion_url(value)
        return value.strip().rstrip("/")


class BatchModelsInput(ModelSourceInput):
    models: list[str] = Field(min_length=1, max_length=100)
    tag: str = Field(default="", max_length=80)
    platform: str = Field(default="天翼云", min_length=1, max_length=40)

    @field_validator("models")
    @classmethod
    def validate_models(cls, models):
        result = list(dict.fromkeys(m.strip() for m in models if m.strip()))
        if not result or any(len(m) > 120 for m in result):
            raise ValueError("请选择1至100个模型，每个模型名称不超过120个字符")
        return result


class EndpointDeleteInput(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)


class JobDeleteInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value):
        if any(not jid or len(jid) > 64 for jid in value):
            raise ValueError("测试记录ID需为1至64个字符")
        return list(dict.fromkeys(value))


class EndpointGroupsDeleteInput(BaseModel):
    platforms: list[str] = Field(min_length=1, max_length=200)

    @field_validator("platforms")
    @classmethod
    def validate_platforms(cls, value):
        names = list(dict.fromkeys(name.strip() for name in value))
        if any(not name or len(name) > 40 for name in names):
            raise ValueError("接口组名称需为1至40个字符")
        return names


class RunInput(BaseModel):
    endpoint_id: int
    suite: str = Field(default="quick", pattern=r"^(quick|standard|quality|custom)$")
    profile: str = Field(default="short", pattern=r"^(short|long_input|long_output|custom)$")
    count: int = Field(default=5, ge=1, le=200)
    concurrency: int = Field(default=1, ge=1, le=16)
    scenario: str = Field(default="chat", pattern=r"^(probe|chat|knowledge|workflow)$")
    max_tokens: int = Field(default=8192, ge=32, le=131072)
    timeout: int = Field(default=180, ge=5, le=600)
    temperature: float | None = Field(default=None, ge=0, le=2)
    prompt: str = Field(default="", max_length=40000)
    extra: dict = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def extra_valid(cls, value):
        allowed = {"messages", "thinking", "enable_thinking", "reasoning_effort", "top_p", "top_k",
                   "seed", "stop", "presence_penalty", "frequency_penalty", "response_format", "prompt_cache_key"}
        if set(value) - allowed:
            raise ValueError("高级参数含不支持的字段；model、stream、max_tokens等由测试配置管理")
        if len(json.dumps(value, ensure_ascii=False)) > 60000:
            raise ValueError("高级参数过大")
        if "messages" in value:
            msgs = value["messages"]
            if not isinstance(msgs, list) or not msgs:
                raise ValueError("messages 必须为非空数组")
            for message in msgs:
                if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant") or not isinstance(message.get("content"), str):
                    raise ValueError("首版客户案例仅支持 system/user/assistant 的文本 messages")
        return value


class TemporaryEndpointInput(BaseModel):
    name: str = Field(default="临时接口", min_length=1, max_length=60)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    api_key: SecretStr = Field(default=SecretStr(""), max_length=2048)

    @field_validator("name", "model")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("名称或模型 ID 不能为空")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        completion_url(value)
        return value.strip().rstrip("/")


class TemporaryRunInput(RunInput):
    endpoint_id: int = 0
    temporary_endpoint: TemporaryEndpointInput


class SettingsInput(BaseModel):
    node_name: str = Field(min_length=1, max_length=80)
    global_concurrency: int = Field(ge=1, le=16)
    daily_request_limit: int = Field(ge=1, le=20000)
    retention_days: int = Field(ge=1, le=365)
    temporary_daily_request_limit: int = Field(default=100, ge=1, le=20000)


class UserInput(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=6, max_length=128)
    role: str = Field(default="member", pattern=r"^(admin|member|viewer)$")


class PasswordInput(BaseModel):
    current: str = Field(max_length=128)
    password: str = Field(min_length=6, max_length=128)


def create_app(data_dir, static_dir=None):
    store = Store(data_dir)
    running = {}
    cancelled = set()
    children = defaultdict(set)
    active_requests = 0
    scheduler_task = None
    auth_failures = defaultdict(list)
    global_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal scheduler_task
        store.recover()
        scheduler_task = asyncio.create_task(scheduler())
        try:
            yield
        finally:
            scheduler_task.cancel()
            for task in list(running.values()):
                task.cancel()
            await asyncio.gather(scheduler_task, *list(running.values()), return_exceptions=True)
            store.recover()

    app = FastAPI(title="词镜 llmirror · API 实测看板", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo credential-bearing input objects in validation responses.
        return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": e["msg"],
                             "type": e["type"]} for e in exc.errors()]}, status_code=422)

    def visible(job, user):
        return job.get("private_owner_id") is None or job["private_owner_id"] == user["id"]

    def get_job(jid, user):
        job = store.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not job or not visible(job, user):
            raise HTTPException(404, "任务不存在")
        return job

    @app.middleware("http")
    async def guard(request, call_next):
        if request.url.path.startswith("/api/"):
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 128000:
                return JSONResponse({"detail": "请求体超过128KB上限"}, status_code=413)
            # Only JSON mutations are accepted. This also blocks form-based cross-site requests.
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if not request.headers.get("content-type", "").lower().startswith("application/json"):
                    return JSONResponse({"detail": "请求必须使用JSON格式"}, status_code=415)
                chunks = []
                received = 0
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > 128000:
                        return JSONResponse({"detail": "请求体超过128KB上限"}, status_code=413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
                origin = request.headers.get("origin")
                if origin:
                    from urllib.parse import urlsplit
                    if urlsplit(origin).netloc != request.headers.get("host"):
                        return JSONResponse({"detail": "不允许跨站操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def auth(request, admin=False, write=False, self_service=False):
        token = (request.cookies.get("llmirror_session") or request.cookies.get("pulse_session", ""))
        digest = hashlib.sha256(token.encode()).hexdigest()
        user = store.one("SELECT u.id,u.username,u.role,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?", (digest, now()))
        if not user:
            raise HTTPException(401, "请先登录")
        if admin and user["role"] != "admin":
            raise HTTPException(403, "此操作需要管理员权限")
        if write:
            if user["role"] == "viewer" and not self_service:
                raise HTTPException(403, "只读账号不能发起测试或修改数据")
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), user["csrf"]):
                raise HTTPException(403, "页面凭证已失效，请刷新后重试")
        return user

    def issue_session(user, request):
        token = secrets.token_urlsafe(40)
        csrf = secrets.token_urlsafe(24)
        store.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user["id"], csrf, now() + 12 * 3600))
        result = JSONResponse({"username": user["username"], "role": user["role"], "csrf": csrf})
        # Proxy headers are accepted only from loopback by the launcher.
        result.set_cookie("llmirror_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=12 * 3600)
        return result

    def local(request):
        try:
            return ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            return False

    @app.get("/api/health")
    async def health():
        return {"ok": True, "product": "llmirror", "version": VERSION}

    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        initialized = bool(store.one("SELECT id FROM users LIMIT 1"))
        info = {"initialized": initialized, "local": local(request), "version": VERSION, "platform": platform.system()}
        if initialized:
            try:
                info["user"] = auth(request)
            except HTTPException:
                pass
        return info

    @app.post("/api/auth/setup")
    async def setup(data: Credentials, request: Request):
        if not local(request):
            raise HTTPException(403, "首次设置仅限部署电脑本机，请在该电脑打开本地地址")
        hashed = await asyncio.to_thread(password_hash, data.password)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM users LIMIT 1").fetchone():
                raise HTTPException(409, "系统已初始化，请登录")
            uid = db.execute("INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)", (data.username, hashed, "admin", now())).lastrowid
        return issue_session({"id": uid, "username": data.username, "role": "admin"}, request)

    @app.post("/api/auth/login")
    async def login(data: Credentials, request: Request):
        address = request.client.host
        auth_failures[address] = [t for t in auth_failures[address] if t > now() - 300]
        if len(auth_failures[address]) >= 10:
            raise HTTPException(429, "尝试次数过多，请5分钟后重试")
        user = store.one("SELECT * FROM users WHERE username=?", (data.username,))
        valid = await asyncio.to_thread(password_ok, data.password, user["password"] if user else dummy_password)
        if not user or not valid:
            auth_failures[address].append(now())
            raise HTTPException(401, "账号或密码不正确")
        auth_failures.pop(address, None)
        return issue_session(user, request)

    dummy_password = password_hash(secrets.token_urlsafe(16))

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        auth(request, write=True, self_service=True)
        store.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256((request.cookies.get("llmirror_session") or request.cookies.get("pulse_session", "")).encode()).hexdigest(),))
        result = JSONResponse({"ok": True})
        result.delete_cookie("llmirror_session")
        result.delete_cookie("pulse_session")
        return result

    @app.post("/api/auth/password")
    async def change_password(data: PasswordInput, request: Request):
        user = auth(request, write=True)
        current = store.one("SELECT password FROM users WHERE id=?", (user["id"],))
        if not await asyncio.to_thread(password_ok, data.current, current["password"]):
            raise HTTPException(400, "当前密码不正确")
        hashed = await asyncio.to_thread(password_hash, data.password)
        with store.connection() as db:
            db.execute("UPDATE users SET password=? WHERE id=?", (hashed, user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        return {"ok": True}

    @app.get("/api/endpoints")
    async def get_endpoints(request: Request):
        auth(request)
        today = datetime.now(CN).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        totals = {r['endpoint_id']: r for r in store.rows(
            "SELECT s.endpoint_id,COUNT(*) AS scheduled_today_requests,"
            "SUM(COALESCE(s.prompt_tokens,0)+COALESCE(s.output_tokens,0)) AS scheduled_today_tokens,"
            "SUM(CASE WHEN s.prompt_tokens IS NULL OR s.output_tokens IS NULL THEN 1 ELSE 0 END) AS scheduled_usage_missing "
            "FROM samples s JOIN jobs j ON j.id=s.job_id WHERE j.owner='定时采样' AND s.created>=? "
            "AND s.status!='cancelled' GROUP BY s.endpoint_id", (today,))}
        return [{**endpoint, **totals.get(endpoint['id'], {'scheduled_today_requests': 0,
                'scheduled_today_tokens': 0, 'scheduled_usage_missing': 0})} for endpoint in store.endpoints()]

    @app.post("/api/endpoints")
    async def add_endpoint(data: EndpointInput, request: Request):
        auth(request, admin=True, write=True)
        eid = store.execute("INSERT INTO endpoints(name,base_url,model,api_key,tag,created,schedule_minutes,next_run,platform,schedule_scenario) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (data.name.strip(), data.base_url, data.model.strip(), store.encrypt(data.api_key.strip()), data.tag.strip(), now(), data.schedule_minutes, now() + data.schedule_minutes * 60, data.platform.strip() or '天翼云', data.schedule_scenario))
        return store.endpoint(eid)

    def resolve_source(data):
        if data.api_key.strip():
            return data.api_key.strip()
        if data.source_id is not None:
            endpoint = store.endpoint(data.source_id, secret=True)
            if not endpoint:
                raise HTTPException(400, "所选接口不存在")
            if completion_url(endpoint["base_url"]) != completion_url(data.base_url):
                raise HTTPException(400, "复用凭证时，接口地址必须与所选接口一致")
            return store.decrypt(endpoint["api_key"])
        return ""

    async def fetch_models(data, key, public_only=False):
        if not key:
            raise HTTPException(400, "请填写 API Key，或选择一个已保存凭证的接口后读取模型列表")
        try:
            urls = catalog_urls(data.base_url)
            async with asyncio.timeout(35):
                async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False,
                        transport=public_transport() if public_only else None) as client:
                    for index, url in enumerate(urls):
                        response = await client.get(url, headers={"Authorization": "Bearer " + key,
                            "Accept": "application/json", "Content-Type": "application/json"})
                        # Only a missing route permits another path, always on the same origin.
                        if response.status_code in (404, 405) and index + 1 < len(urls):
                            continue
                        break
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if response.status_code != 200:
                detail = redact(provider_error(payload), key)[:240]
                hint = {401: "凭证无效或已过期", 403: "凭证没有读取模型列表的权限", 404: "当前地址未提供模型列表接口",
                        405: "当前地址不支持 GET 模型列表", 429: "模型列表读取被限流，请稍后再试"}.get(response.status_code, "服务商返回错误")
                raise HTTPException(400, f"读取模型列表失败：HTTP {response.status_code}，{hint}" + (f"；{detail}" if detail else ""))
            return {"models": catalog_models(payload)}
        except HTTPException:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise HTTPException(504, "读取模型列表超时（后台上限35秒）；请检查该接口的网络连接后重试")
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(400, "无法读取模型列表：" + redact(str(exc), key)[:250])

    @app.post("/api/endpoints/discover")
    async def discover_models(data: ModelSourceInput, request: Request):
        auth(request, admin=True, write=True)
        return await fetch_models(data, resolve_source(data))

    @app.post("/api/temporary/discover")
    async def temporary_models(data: ModelSourceInput, request: Request):
        user = auth(request, write=True)
        if data.source_id is not None:
            raise HTTPException(400, "临时接口需自行填写凭证，不能读取公共接口凭证")
        return await fetch_models(data, data.api_key.strip(), user["role"] != "admin")

    @app.get("/api/temporary/budget")
    async def temporary_budget(request: Request):
        user = auth(request)
        row = store.one("SELECT reserved FROM temporary_budget WHERE day=? AND user_id=?", (day_key(), user["id"]))
        return {"used": row["reserved"] if row else 0,
                "limit": store.settings()["temporary_daily_request_limit"]}

    @app.post("/api/temporary/jobs")
    async def temporary_job(data: TemporaryRunInput, request: Request):
        user = auth(request, write=True)
        temp = data.temporary_endpoint
        endpoint = {"id": -secrets.randbelow(2**40)-1, "name": temp.name.strip(),
                    "base_url": temp.base_url, "model": temp.model.strip(), "api_key": ""}
        return start_job(data, user["username"], temporary=endpoint, user=user,
                         temporary_key=temp.api_key.get_secret_value().strip())

    @app.post("/api/endpoints/batch")
    async def batch_models(data: BatchModelsInput, request: Request):
        auth(request, admin=True, write=True)
        key = resolve_source(data)
        normalized = completion_url(data.base_url)
        existing = store.rows("SELECT base_url,model,api_key FROM endpoints WHERE demo=0")
        known = set()
        for item in existing:
            if completion_url(item["base_url"]) == normalized and secrets.compare_digest(
                    store.decrypt(item["api_key"]).encode(), key.encode()):
                known.add(item["model"])
        created, skipped = [], []
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for model in data.models:
                if model in known:
                    skipped.append(model)
                    continue
                db.execute("INSERT INTO endpoints(name,base_url,model,api_key,tag,created,schedule_minutes,next_run,platform) VALUES(?,?,?,?,?,?,0,0,?)",
                           (model[:60], data.base_url, model, store.encrypt(key), data.tag.strip(), now(), data.platform.strip() or '天翼云'))
                created.append(model)
                known.add(model)
        return {"created": created, "skipped": skipped}

    @app.put("/api/endpoints/{eid}")
    async def update_endpoint(eid: int, data: EndpointInput, request: Request):
        auth(request, admin=True, write=True)
        row = store.endpoint(eid, secret=True)
        if not row:
            raise HTTPException(400, "接口不存在")
        if store.one("SELECT id FROM jobs WHERE endpoint_id=? AND status IN ('queued','running','cancelling')", (eid,)):
            raise HTTPException(409, "请先等待或停止该接口的运行任务")
        key = store.encrypt(data.api_key.strip()) if data.api_key.strip() else row["api_key"]
        store.execute("UPDATE endpoints SET name=?,base_url=?,model=?,api_key=?,tag=?,schedule_minutes=?,next_run=?,platform=?,schedule_scenario=? WHERE id=?",
                      (data.name.strip(), data.base_url, data.model.strip(), key, data.tag.strip(), data.schedule_minutes, now() + data.schedule_minutes * 60, data.platform.strip() or '天翼云', data.schedule_scenario, eid))
        return store.endpoint(eid)

    def remove_endpoints(db, ids):
        ids = list(dict.fromkeys(ids))
        if not ids:
            return 0
        slots = ','.join('?' for _ in ids)
        if db.execute(f"SELECT id FROM jobs WHERE endpoint_id IN ({slots}) AND status IN ('queued','running','cancelling') LIMIT 1", ids).fetchone():
            raise HTTPException(409, "所选接口包含运行中的任务，请先停止或等待完成；本次未删除任何接口")
        return db.execute(f"DELETE FROM endpoints WHERE id IN ({slots})", ids).rowcount

    @app.post("/api/endpoints/delete")
    async def delete_endpoints(data: EndpointDeleteInput, request: Request):
        auth(request, admin=True, write=True)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = remove_endpoints(db, data.ids)
        return {"ok": True, "deleted": deleted}

    @app.post("/api/endpoint-groups/delete")
    async def delete_endpoint_groups(data: EndpointGroupsDeleteInput, request: Request):
        auth(request, admin=True, write=True)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            slots = ','.join('?' for _ in data.platforms)
            ids = [row[0] for row in db.execute(f"SELECT id FROM endpoints WHERE demo=0 AND platform IN ({slots})", data.platforms)]
            deleted = remove_endpoints(db, ids)
        return {"ok": True, "deleted": deleted}

    @app.delete("/api/endpoints/{eid}")
    async def delete_endpoint(eid: int, request: Request):
        auth(request, admin=True, write=True)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = remove_endpoints(db, [eid])
        return {"ok": True, "deleted": deleted}

    async def acquire_slot(cancel_id):
        nonlocal active_requests
        while cancel_id not in cancelled:
            async with global_lock:
                if active_requests < store.settings()["global_concurrency"]:
                    active_requests += 1
                    return True
            await asyncio.sleep(0.05)
        return False

    async def execute_job(jid, endpoint, cfg, temporary_key=None, public_only=False):
        nonlocal active_requests
        store.execute("UPDATE jobs SET status='running',started=? WHERE id=?", (now(), jid))
        local_slots = asyncio.Semaphore(cfg["concurrency"])
        work_items = ([(case["prompt"], case) for _ in range(cfg["count"]) for case in CASES]
                      if cfg["suite"] == "quality" else
                      [(performance_prompt(cfg["profile"], i + cfg.get("probe_index", 0), cfg["prompt"], cfg.get("extra", {}).get("messages"), cfg.get("scenario")), None) for i in range(cfg["count"])])
        key = temporary_key if temporary_key is not None else store.decrypt(endpoint["api_key"])
        temporary_key = None
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg["timeout"], connect=min(15, cfg["timeout"])),
            follow_redirects=False, trust_env=False, transport=public_transport() if public_only else None,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))

        async def item(prompt, case):
            nonlocal active_requests
            queued = time.perf_counter()
            async with local_slots:
                if not await acquire_slot(jid):
                    return
                try:
                    queue_ms = round((time.perf_counter() - queued) * 1000, 2)
                    sample = await measured_request(endpoint, key, cfg, prompt, case, client=client)
                    if cfg.get("temporary") and key:
                        sample = {k: v.replace(key, "[凭证已隐藏]") if isinstance(v, str) else v
                                  for k, v in sample.items()}
                    sample.update(job_id=jid, endpoint_id=endpoint["id"], demo=0, queue_ms=queue_ms)
                    store.add_sample(sample)
                except asyncio.CancelledError:
                    sample = dict(job_id=jid, endpoint_id=endpoint["id"], demo=0, created=now(), status="cancelled", prompt=prompt, error="用户停止或程序关闭；请求可能已产生平台用量")
                    store.add_sample(sample)
                    raise
                finally:
                    async with global_lock:
                        active_requests -= 1

        try:
            for prompt, case in work_items:
                task = asyncio.create_task(item(prompt, case))
                children[jid].add(task)
            await asyncio.gather(*children[jid])
            status = "cancelled" if jid in cancelled else "completed"
            store.execute("UPDATE jobs SET status=?,ended=? WHERE id=?", (status, now(), jid))
        except asyncio.CancelledError:
            for task in children[jid]:
                task.cancel()
            await asyncio.gather(*children[jid], return_exceptions=True)
            store.execute("UPDATE jobs SET status=?,ended=? WHERE id=?", ("cancelled" if jid in cancelled else "interrupted", now(), jid))
        except Exception:
            if cfg.get("temporary"):
                logging.getLogger("llmirror.jobs").error("Temporary job failed: %s", jid)
            else:
                logging.getLogger("llmirror.jobs").exception("Background job failed: %s", jid)
            store.execute("UPDATE jobs SET status='failed',ended=? WHERE id=?", (now(), jid))
            for task in children[jid]:
                task.cancel()
            await asyncio.gather(*children[jid], return_exceptions=True)
        finally:
            if client is not None:
                await client.aclose()
            running.pop(jid, None)
            children.pop(jid, None)
            cancelled.discard(jid)

    def start_job(data, owner, temporary=None, user=None, temporary_key=None):
        endpoint = temporary or store.endpoint(data.endpoint_id, secret=True)
        if not endpoint:
            raise HTTPException(404, "接口不存在")
        if len(running) >= 20:
            raise HTTPException(429, "运行任务已达20个，请等待完成")
        if data.suite == "quality":
            if data.count > 5:
                raise HTTPException(400, "质量回归每题最多重复5次")
            if data.extra.get("messages"):
                raise HTTPException(400, "质量回归使用固定题库，不能覆盖messages")
        planned = data.count * (len(CASES) if data.suite == "quality" else 1)
        limits = store.settings()
        if temporary:
            refusal = store.reserve_temporary(day_key(), planned, limits["daily_request_limit"],
                                              user["id"], limits["temporary_daily_request_limit"])
            if refusal:
                raise HTTPException(429, "已达到本人今日临时接口请求上限" if refusal == "member" else "已达到今日全局请求预算")
        elif not store.reserve(day_key(), planned, limits["daily_request_limit"]):
            raise HTTPException(429, "已达到今日真实请求预算；请减少测试次数或联系管理员调整")
        cfg = apply_scenario(data.model_dump(exclude={"temporary_endpoint"}))
        if temporary:
            cfg.update(temporary=True, endpoint_id=endpoint["id"])
        if cfg["scenario"] == "probe":
            sequence = store.one("SELECT COUNT(*) AS n FROM jobs WHERE endpoint_id=? AND json_extract(config,'$.scenario')='probe'", (endpoint["id"],))["n"]
            cfg.update(probe_version="inspection-v2", probe_index=sequence,
                       endpoint_base_url=completion_url(endpoint["base_url"]))
        if data.suite == "quality":
            cfg.update(quality_version=QUALITY_VERSION, quality_case_count=len(CASES), output_limit_policy="provider_default")
        if data.suite != "quality" and data.profile != "custom" and not data.prompt and not data.extra.get("messages"):
            cfg["prompt_version"] = "scenarios-v3"
        jid = secrets.token_hex(8)
        store.execute("INSERT INTO jobs(id,endpoint_id,endpoint_name,model,demo,suite,profile,status,planned,created,node,config,owner) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (jid, endpoint["id"], endpoint["name"], endpoint["model"], 0, data.suite, cfg["profile"], "queued", planned, now(), store.settings()["node_name"], json.dumps(cfg, ensure_ascii=False), owner))
        if temporary:
            store.execute("UPDATE jobs SET private_owner_id=?,temporary=1 WHERE id=?", (user["id"], jid))
        running[jid] = asyncio.create_task(execute_job(jid, endpoint, cfg,
            temporary_key=temporary_key, public_only=bool(temporary and user["role"] != "admin")))
        def settled(task):
            # Cancellation can arrive before the task enters its try/finally block.
            if jid in running:
                store.execute("UPDATE jobs SET status=?,ended=? WHERE id=?", ("cancelled" if jid in cancelled else "interrupted", now(), jid))
                running.pop(jid, None)
                children.pop(jid, None)
                cancelled.discard(jid)
        running[jid].add_done_callback(settled)
        return store.one("SELECT * FROM jobs WHERE id=?", (jid,))

    @app.post("/api/jobs")
    async def create_job(data: RunInput, request: Request):
        user = auth(request, write=True)
        return start_job(data, user["username"])

    @app.get("/api/jobs")
    async def jobs(request: Request, limit: int = 50):
        user = auth(request)
        return store.rows("SELECT * FROM jobs WHERE demo=0 AND (private_owner_id IS NULL OR private_owner_id=?) ORDER BY created DESC LIMIT ?", (user["id"], max(1, min(limit, 200))))

    def delete_records(ids, user):
        # Check the entire selection before deleting anything. A task finishing
        # or permissions changing must not result in a partially deleted batch.
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for jid in ids:
                job = db.execute("SELECT owner,status,private_owner_id FROM jobs WHERE id=?", (jid,)).fetchone()
                if not job or not visible(dict(job), user):
                    raise HTTPException(404, "记录不存在或已被删除，请刷新列表")
                if user["role"] != "admin" and job["owner"] != user["username"]:
                    raise HTTPException(403, "只能删除自己发起的记录")
                if job["status"] in ACTIVE:
                    raise HTTPException(409, "所选记录包含未结束任务，请先停止任务并等待结束")
            db.executemany("DELETE FROM samples WHERE job_id=?", ((jid,) for jid in ids))
            db.executemany("DELETE FROM jobs WHERE id=?", ((jid,) for jid in ids))

    @app.post("/api/jobs/delete")
    async def delete_jobs(data: JobDeleteInput, request: Request):
        user = auth(request, write=True)
        delete_records(data.ids, user)
        return {"ok": True, "deleted": len(data.ids)}

    @app.get("/api/jobs/{jid}")
    async def job_detail(jid: str, request: Request):
        user = auth(request)
        job = get_job(jid, user)
        job["samples"] = store.rows("SELECT * FROM samples WHERE job_id=? ORDER BY id", (jid,))
        return job

    @app.delete("/api/jobs/{jid}")
    async def delete_job(jid: str, request: Request):
        user = auth(request, write=True)
        delete_records([jid], user)
        return {"ok": True}

    @app.post("/api/jobs/{jid}/cancel")
    async def cancel_job(jid: str, request: Request):
        user = auth(request, write=True)
        job = get_job(jid, user)
        if user["role"] != "admin" and job["owner"] != user["username"]:
            raise HTTPException(403, "只能停止自己发起的任务")
        if jid in running:
            cancelled.add(jid)
            store.execute("UPDATE jobs SET status='cancelling' WHERE id=?", (jid,))
            running[jid].cancel()
        return {"ok": True}

    @app.post("/api/inspections/run-all")
    async def inspect_all(request: Request):
        user = auth(request, write=True)
        # Temporary endpoints are not saved and never participate in patrols.
        endpoints = store.rows("SELECT id FROM endpoints WHERE demo=0 ORDER BY id")
        active = {row["endpoint_id"] for row in store.rows(
            "SELECT endpoint_id FROM jobs WHERE status IN ('queued','running','cancelling')")}
        pending = [e["id"] for e in endpoints if e["id"] not in active]
        result = {"created": 0, "skipped": len(endpoints) - len(pending),
                  "unstarted": 0, "reason": "", "jobs": []}
        for index, eid in enumerate(pending):
            try:
                job = start_job(RunInput(endpoint_id=eid, suite="quick", scenario="probe",
                                         count=1, concurrency=1), user["username"])
            except HTTPException as exc:
                # Keep existing budget and task caps; report partial acceptance
                # explicitly rather than claiming every model was dispatched.
                if exc.status_code != 429:
                    raise
                result.update(unstarted=len(pending) - index, reason=exc.detail)
                break
            result["created"] += 1
            result["jobs"].append(job["id"])
        return result

    @app.get("/api/inspections")
    async def inspections(request: Request, hours: int = 6, endpoint_id: int = 0):
        user = auth(request)
        hours = hours if hours in (6, 24, 168, 720, 2160, 8760) else 6
        end = now()
        begin = end - hours * 3600
        endpoints = store.endpoints()
        known = {e["id"]: e for e in endpoints}
        rows = store.rows("SELECT s.id,s.job_id,s.endpoint_id,s.created,s.status,s.ttft_ms,s.answer_ms,s.e2e_ms,s.output_tokens,s.prompt_tokens,s.cached_tokens,s.quality_pass,j.model AS job_model,json_extract(j.config,'$.endpoint_base_url') AS job_base "
                          "FROM samples s JOIN jobs j ON j.id=s.job_id WHERE s.demo=0 AND s.created>=? "
                          "AND json_extract(j.config,'$.probe_version')='inspection-v2' AND (j.private_owner_id IS NULL OR j.private_owner_id=?)", (begin - hours * 3600, user["id"]))
        rows = [r for r in rows if r['endpoint_id'] in known and r['job_model'] == known[r['endpoint_id']]['model']
                and r['job_base'] == completion_url(known[r['endpoint_id']]['base_url'])
                and (not endpoint_id or r['endpoint_id'] == endpoint_id)]
        current = [r for r in rows if r['created'] >= begin]
        previous = [r for r in rows if r['created'] < begin]
        summary = inspection_summary(current)
        summary['previous'] = inspection_summary(previous)
        table = []
        for endpoint in endpoints:
            if endpoint_id and endpoint['id'] != endpoint_id:
                continue
            group = [r for r in current if r['endpoint_id'] == endpoint['id']]
            latest = max(group, key=lambda r:r['created']) if group else None
            interval = endpoint['schedule_minutes'] * 60
            table.append({**endpoint, **inspection_summary(group),
                          'last_seen': latest['created'] if latest else None,
                          'last_status': latest['status'] if latest else 'unknown',
                          'last_job': latest['job_id'] if latest else None,
                          'stale': bool(latest and end-latest['created'] > max(3600, interval*2))})
        count = {720:30, 2160:90, 8760:52}.get(hours,24)
        width = hours*3600/count
        buckets = []
        for i in range(count):
            t = begin+i*width
            group = [r for r in current if t <= r['created'] < t+width]
            buckets.append({'time':t, **inspection_summary(group)})
        budget = store.one("SELECT reserved FROM daily_budget WHERE day=?", (day_key(),))
        return {'summary':summary, 'endpoints':table, 'buckets':buckets, 'bucket_width':width,
                'server_time':end, 'hours':hours, 'settings':store.settings(),
                'budget_used':budget['reserved'] if budget else 0}

    @app.get("/api/dashboard")
    async def dashboard(request: Request, hours: int = 24, endpoint_id: int = 0, profile: str = "all", suite: str = "all", scenario: str = "all"):
        user = auth(request)
        hours = hours if hours in (6, 24, 168, 720, 2160, 8760) else 24
        end = now()
        begin = end - hours * 3600
        cond = "s.demo=? AND s.created>=? AND (j.private_owner_id IS NULL OR j.private_owner_id=?)"
        args = [0, begin - hours * 3600, user["id"]]
        if endpoint_id:
            cond += " AND s.endpoint_id=?"
            args.append(endpoint_id)
        if profile in ("short", "long_input", "long_output", "custom"):
            cond += " AND j.profile=?"
            args.append(profile)
        if suite in ("quick", "standard", "quality", "custom"):
            cond += " AND j.suite=?"
            args.append(suite)
        if scenario in SCENARIOS:
            cond += " AND json_extract(j.config,'$.scenario')=?"
            args.append(scenario)
        rows = store.rows("SELECT s.id,s.job_id,s.endpoint_id,s.demo,s.created,s.status,s.ttft_ms,s.tps,s.e2e_ms,s.prompt_tokens,s.output_tokens,s.cached_tokens,s.quality_pass,j.node,j.endpoint_name,j.model,j.suite,j.profile,j.temporary FROM samples s JOIN jobs j ON s.job_id=j.id WHERE " + cond, args)
        current = [r for r in rows if r["created"] >= begin]
        previous = [r for r in rows if r["created"] < begin]
        summary = summarize(current)
        summary["previous"] = summarize(previous)
        table = []
        for endpoint in store.endpoints():
            if endpoint_id and endpoint["id"] != endpoint_id:
                continue
            group = [r for r in current if r["endpoint_id"] == endpoint["id"]]
            aggregate = summarize(group)
            latest = max(group, key=lambda r: r["created"]) if group else None
            table.append({**endpoint, **aggregate, "last_seen": latest["created"] if latest else None,
                          "last_status": latest["status"] if latest else "unknown"})
        # Keep deleted endpoints visible in historical reports.
        known = {e["id"] for e in table}
        for eid in {r["endpoint_id"] for r in current} - known:
            group = [r for r in current if r["endpoint_id"] == eid]
            latest = max(group, key=lambda r: r["created"])
            table.append({"id": eid, "name": latest["endpoint_name"] + (" · 临时接口" if latest.get("temporary") else "（已删除）"), "temporary": latest.get("temporary", 0), "model": latest["model"], "tag": "", "demo": latest["demo"], "last_seen": latest["created"], "last_status": latest["status"], **summarize(group)})
        buckets = []
        bucket_count = {720: 30, 2160: 90, 8760: 52}.get(hours, 24)
        width = hours * 3600 / bucket_count
        for i in range(bucket_count):
            t = begin + i * width
            group = [r for r in current if t <= r["created"] < t + width]
            buckets.append({"time": t, **summarize(group)})
        errors = defaultdict(int)
        for r in current:
            if r["status"] != "ok":
                errors[r["status"]] += 1
        budget = store.one("SELECT reserved FROM daily_budget WHERE day=?", (day_key(),))
        scenarios = []
        for suite, profile in sorted({(r["suite"], r["profile"]) for r in current}):
            group = [r for r in current if r["suite"] == suite and r["profile"] == profile]
            scenarios.append({"suite": suite, "profile": profile, **summarize(group)})
        return {"summary": summary, "endpoints": table, "buckets": buckets, "bucket_width": width, "errors": dict(errors),
                "scenarios": scenarios, "settings": store.settings(), "budget_used": budget["reserved"] if budget else 0,
                "active_jobs": len(running), "active_requests": active_requests,
                "latest": sorted(current, key=lambda r: r["created"], reverse=True)[:8], "server_time": end}

    @app.get("/api/export/{jid}")
    async def export_job(jid: str, request: Request):
        user = auth(request)
        job = get_job(jid, user)
        samples = store.rows("SELECT * FROM samples WHERE job_id=? ORDER BY id", (jid,))
        columns = ["id", "created", "status", "ttft_ms", "answer_ms", "e2e_ms", "finish_ms", "tps", "max_gap_ms", "queue_ms", "prompt_tokens", "output_tokens", "cached_tokens", "token_source", "http_status", "finish_reason", "request_id", "case_name", "quality_pass", "quality_note"]
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(["词镜 llmirror 报告", "实测数据", "任务", jid, "接口", safe_cell(job["endpoint_name"]), "模型", safe_cell(job["model"]), "测试节点", safe_cell(job["node"])])
        writer.writerow(["说明", "单位ms；tps为接口用量与流式接收时间计算；不包含提示词、回答和凭证；取消请求可能已计费；质量评分仅针对完整回答。"])
        writer.writerow(columns)
        for sample in samples:
            writer.writerow([safe_cell(sample.get(c)) for c in columns])
        return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="llmirror-{jid}.csv"'})

    @app.get("/api/settings")
    async def get_settings(request: Request):
        auth(request)
        return {**store.settings(), "version": VERSION}

    @app.put("/api/settings")
    async def save_settings(data: SettingsInput, request: Request):
        auth(request, admin=True, write=True)
        store.set_settings(data.model_dump())
        return store.settings()

    @app.post("/api/backup")
    async def backup(request: Request):
        auth(request, admin=True, write=True)
        folder = store.backup()
        return {"ok": True, "folder": "data/backups/" + folder.name, "message": "包含数据库及凭证密钥，请将备份文件夹作为敏感资料保管。"}

    @app.post("/api/cleanup")
    async def cleanup(request: Request):
        auth(request, admin=True, write=True)
        store.cleanup()
        return {"ok": True}

    @app.get("/api/users")
    async def users(request: Request):
        auth(request, admin=True)
        return store.rows("SELECT id,username,role,created FROM users ORDER BY id")

    @app.post("/api/users")
    async def add_user(data: UserInput, request: Request):
        auth(request, admin=True, write=True)
        if store.one("SELECT id FROM users WHERE username=?", (data.username,)):
            raise HTTPException(409, "该账号已存在")
        hashed = await asyncio.to_thread(password_hash, data.password)
        store.execute("INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)", (data.username, hashed, data.role, now()))
        return {"ok": True}

    @app.delete("/api/users/{uid}")
    async def delete_user(uid: int, request: Request):
        user = auth(request, admin=True, write=True)
        if uid == user["id"]:
            raise HTTPException(400, "不能删除当前账号")
        with store.connection() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            # SQLite may reuse a deleted user's integer ID. Orphan private history first.
            db.execute("UPDATE jobs SET private_owner_id=-ABS(private_owner_id) WHERE private_owner_id=?", (uid,))
            db.execute("DELETE FROM temporary_budget WHERE user_id=?", (uid,))
            db.execute("DELETE FROM users WHERE id=?", (uid,))
        return {"ok": True}

    @app.get("/api/cases")
    async def cases(request: Request):
        auth(request)
        return CASES

    @app.get("/api/scenarios")
    async def scenarios(request: Request):
        auth(request)
        return SCENARIOS

    async def scheduler():
        last_cleanup = 0
        while True:
            await asyncio.sleep(15)
            if now() - last_cleanup > 3600:
                store.cleanup()
                last_cleanup = now()
            for endpoint in store.rows("SELECT * FROM endpoints WHERE schedule_minutes>=5 AND next_run<=? AND demo=0", (now(),)):
                if any(j["endpoint_id"] == endpoint["id"] for j in store.rows("SELECT endpoint_id FROM jobs WHERE status IN ('queued','running','cancelling')")):
                    continue
                store.execute("UPDATE endpoints SET next_run=? WHERE id=?", (now() + endpoint["schedule_minutes"] * 60, endpoint["id"]))
                try:
                    start_job(RunInput(endpoint_id=endpoint["id"], count=1, scenario=endpoint["schedule_scenario"]), "定时采样")
                except HTTPException:
                    # A skipped schedule is never retried in a tight loop and never bypasses budget.
                    pass

    static_dir = Path(static_dir or Path(__file__).parent / "static")
    app.mount("/", BrowserAssets(directory=str(static_dir), html=True), name="static")
    return app


def safe_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def summarize(rows):
    attempted = [r for r in rows if r["status"] != "cancelled"]
    good = [r for r in attempted if r["status"] == "ok"]
    measured = [r for r in attempted if r["status"] in ("ok", "truncated")]
    responses = [r for r in attempted if r["status"] in ("ok", "truncated", "filtered", "empty")]
    truncated = sum(1 for r in attempted if r["status"] == "truncated")
    scored = [r for r in attempted if r.get("quality_pass") is not None]
    cache_known = [r for r in attempted if r.get("cached_tokens") is not None]
    tps = [r["tps"] for r in measured if r.get("tps") is not None]
    return {"count": len(attempted), "ok": len(good), "cancelled": len(rows) - len(attempted),
            "success_rate": round(len(good) / len(attempted) * 100, 2) if attempted else None,
            "response_ok": len(responses), "response_rate": round(len(responses) / len(attempted) * 100, 2) if attempted else None,
            "truncated": truncated, "truncation_rate": round(truncated / len(attempted) * 100, 2) if attempted else None,
            "ttft_p50": percentile([r.get("ttft_ms") for r in measured], 0.5),
            "ttft_p90": percentile([r.get("ttft_ms") for r in measured], 0.9),
            "ttft_p95": percentile([r.get("ttft_ms") for r in measured], 0.95),
            "tps_avg": sum(tps) / len(tps) if tps else None,
            "e2e_p50": percentile([r.get("e2e_ms") for r in good], 0.5),
            "tokens": sum((r.get("output_tokens") or 0) + (r.get("prompt_tokens") or 0) for r in attempted),
            "quality_rate": round(sum(r["quality_pass"] for r in scored) / len(scored) * 100, 2) if scored else None,
            "quality_count": len(scored), "tps_count": len(tps),
            "cache_known": len(cache_known), "cache_hit": sum(1 for r in cache_known if r["cached_tokens"] > 0)}


def inspection_summary(rows):
    result = summarize(rows)
    # Enough streamed output is needed for a useful client-side generation rate.
    valid = [r for r in rows if r['status'] in ('ok','truncated')
             and r.get('output_tokens') is not None and r['output_tokens'] >= 64
             and r.get('ttft_ms') is not None and r.get('e2e_ms') is not None
             and r['e2e_ms']-r['ttft_ms'] >= 500]
    seconds = sum(r['e2e_ms']-r['ttft_ms'] for r in valid)/1000
    tokens = sum(r['output_tokens']-1 for r in valid)
    result['tps_avg'] = round(tokens/seconds,2) if seconds else None
    result['tps_count'] = len(valid)
    result['answer_p50'] = percentile([r.get('answer_ms') for r in rows if r['status'] in ('ok','truncated')], .5)
    attempted = [r for r in rows if r['status'] != 'cancelled']
    result['usage_missing'] = sum(r.get('prompt_tokens') is None or r.get('output_tokens') is None for r in attempted)
    result['errors'] = {status:sum(r['status']==status for r in attempted)
                        for status in ('error','timeout','rate_limited','truncated','empty','filtered')}
    return result
