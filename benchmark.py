"""Measured OpenAI-compatible streaming requests; no automatic retries."""
import asyncio
import json
import math
import random
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx


from quality_cases import CASES, grade

PROMPTS = {
    "short": "请用约200字解释大模型API的首字延迟与生成速度的区别，分两段回答。",
    "long_input": "阅读以下巡检记录，最后总结异常及建议。\n" + "\n".join(f"记录{i:03d}：接口连接正常，状态码200，已完成例行检查。" for i in range(1, 301)) + "\n其中记录157补充：晚高峰出现5次超时。请用约200字总结。",
    "long_output": "请写一份约1200字的大模型API性能排查指南，按网络、请求参数、并发与限流、流式处理、历史对比五个方面说明。",
    "custom": "你好，请介绍一下自己。",
}

def performance_prompt(profile, index, custom="", messages=None, scenario=None):
    """Use readable, varied performance prompts without changing user cases."""
    if custom or profile == "custom" or messages:
        return custom or PROMPTS[profile]
    if scenario == "probe":
        topics = ["资料不足时的客户回复", "业务工单的信息记录", "客服转人工时的交接", "新文档发布前的检查", "重复客户反馈的处理"]
        quantity = secrets.randbelow(81) + 20
        topic = topics[index % len(topics)]
        return (f"主题：{topic}。一家企业每天接到{quantity}条客户咨询。"
                "请为新入职客服写一份约180字的操作说明，分成三个完整段落，"
                "分别说明需要确认的信息、具体处理步骤、结束前的核对事项。"
                "只给出说明正文，内容应完整，不要只列关键词，不需要展开推理。")
    if scenario == "knowledge":
        return knowledge_prompt(index)
    if scenario == "workflow":
        tasks = ["客户报障受理与升级", "企业知识库文档更新", "合同审批与归档", "服务巡检与异常告警", "客户反馈分类与分派"]
        task = tasks[index % 5]
        batch = secrets.randbelow(81) + 20
        return (f"请为企业设计{task}的智能体工作流，每批处理{batch}条业务记录。"
                "只输出JSON对象，包含goal、steps、human_review、failure_handling。"
                "steps按执行顺序列出6至8步，每步包含id、action、inputs、outputs、depends_on。"
                "注明哪些步骤需要人工复核，以及超时、信息不足和重复提交时的处理方式。"
                "本题仅生成计划，不需要实际调用工具。")
    amount = secrets.randbelow(900000) + 100000
    prompts = [
        f"某企业计划为{amount}名客户提供智能客服。请用约200字说明建设步骤和注意事项。",
        f"某企业有{amount}份内部文档，计划建立智能知识库。请用约200字说明实施步骤。",
        f"某系统每天处理约{amount}条业务记录。请用约200字说明如何设计异常告警。",
        f"某服务平台每月收到约{amount}次用户咨询。请用约200字提出提高响应效率的建议。",
        f"某团队需要分析约{amount}条客户反馈。请用约200字说明分类和总结的方法。",
    ]
    if profile == "short":
        return prompts[index % len(prompts)]
    elif profile == "long_input":
        issue = ["多次超时", "多次限流", "首字延迟增长", "输出速度下降", "回答内容截断"][index % 5]
        systems = ["客服系统", "知识库", "工单系统", "数据分析平台", "办公助手"]
        lines = []
        for i in range(1, 301):
            system = systems[(i + index) % len(systems)]
            hour, minute = divmod((8 * 60 + i * 2) % 1440, 60)
            status = issue if i == 157 else "运行正常"
            lines.append(f"{hour:02d}:{minute:02d}，{system}完成第{i}次检查，结果：{status}。")
        return (f"某企业每天处理约{amount}次系统调用。请阅读以下巡检记录，"
                "用约200字总结异常及处理建议。\n" + "\n".join(lines))
    topics = [
        "智能客服系统的建设", "企业知识库的建设", "业务系统异常监控",
        "客户服务流程优化", "客户反馈分析",
    ]
    return (f"某企业每月处理约{amount}条业务记录，计划开展{topics[index % 5]}。"
            "请写一份约1200字的实施方案，说明目标、步骤、风险和效果评估。")


def knowledge_prompt(index):
    """Synthetic policy context, varied questions, no actual retrieval stage."""
    cases = [
        ("客户报障", "客户反馈业务不可用且影响多个部门，应如何分级、响应和升级？", "工单中心", "故障负责人"),
        ("文档管理", "新文档与现有制度冲突，发布前应完成哪些审核并如何处理旧版？", "知识库", "文档负责人"),
        ("合同审批", "合同信息不完整且申请加急，应如何补充信息、审批和留痕？", "合同系统", "合同专员"),
        ("服务巡检", "巡检出现连续异常且尚未确定原因，应如何确认、上报和复查？", "监控平台", "值班工程师"),
        ("客户反馈", "多个渠道收到内容重复的高优先级反馈，应如何合并、分派和回访？", "反馈平台", "客户经理"),
    ]
    topic, question, system, owner = cases[index % 5]
    minutes = secrets.choice([10, 15, 20, 30])
    sections = [
        ("适用范围", f"本制度适用于{topic}相关的内部受理、处理和回访。所有记录统一进入{system}；邮件和口头通知只作为补充，不能替代正式记录。负责人是{owner}，转交责任需要双方确认。"),
        ("必要信息", "提交时必须包含业务名称、发生时间、影响范围、联系人、现象描述和可复核材料。材料需脱敏。信息不足时标记待补充，保留原提交时间并一次列明缺失项，不得凭空补全。"),
        ("优先级", "影响多个部门或核心业务不可用的事项为一级；影响局部业务且有替代方式的为二级；不影响现有业务的咨询或改进为三级。加急申请本身不改变分级，需根据实际影响审核。"),
        ("响应时限", f"一级事项应在{minutes}分钟内确认受理，二级在2小时内确认，三级在1个工作日内确认。确认受理不代表处理完成；不能满足时限时应提前说明原因并升级。"),
        ("审核流程", "处理人先核对事实和适用规则，再由业务负责人审核关键结论。涉及权限、费用、合同条款或大范围变更时必须人工审批。不得通过绕过审批满足加急要求。"),
        ("冲突处理", "信息存在矛盾或新旧制度冲突时，暂停受影响的执行环节，列出冲突点并提交制度负责人确认。经批准发布新版本后，将旧版本标记失效并保留可追溯记录。"),
        ("异常升级", "连续异常、超时未解决或影响范围扩大时应升级至业务负责人和值班主管。同步已知事实、已执行操作、当前影响和下一次更新时间；未核实原因只能标注待确认。"),
        ("重复事项", "依据业务对象、发生时间和现象识别重复记录。合并前保留原编号、来源渠道和联系人，主记录关联全部子记录，避免多次分派及重复执行。"),
        ("执行约束", "操作前检查权限、依赖条件和回退方案。高影响操作需要双人复核。执行失败时停止后续依赖步骤，保留错误与输入摘要；禁止无限重试或重复提交有副作用的操作。"),
        ("验收回访", "完成后由提交人或指定业务代表验收，并记录验证方法与结果。未通过则恢复处理中状态。通知相关联系人，说明已完成内容、遗留风险和后续责任人。"),
        ("审计归档", "每次状态变更记录时间、操作人、依据和结果。附件访问遵循最小权限。归档内容包括原始材料、审批记录、执行结果和回访结论，敏感信息不得出现在公开通知中。"),
        ("例外处理", "制度未覆盖的问题由业务负责人确认临时方案并记录有效期。材料未给出的费用、最终完成时限和组织联系方式不得自行推断，应明确告知需要补充或确认。"),
    ]
    context = "\n\n".join(f"第{i}条 {title}：{text}" for i, (title, text) in enumerate(sections, 1))
    return (f"请根据以下合成的《{topic}处理规范》回答：{question}\n"
            "回答控制在约400字，列出步骤并引用条款编号；资料未给出时明确说明。\n\n" + context)


def completion_url(base_url):
    value = base_url.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("请输入 HTTP/HTTPS 接口地址，不要在地址中放凭证、查询参数或片段。")
    return value if value.endswith("/chat/completions") else value + "/chat/completions"


def redact(message, key=""):
    value = str(message)
    if key:
        value = value.replace(key, "[已脱敏]")
    value = re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1[已脱敏]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|authorization|app[_-]?key)[\"'\s:=]+)[^\s,}\"']+", r"\1[已脱敏]", value)
    return value[:1200]


def percentile(values, q):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    idx = (len(vals) - 1) * q
    low = math.floor(idx)
    high = math.ceil(idx)
    return vals[low] + (vals[high] - vals[low]) * (idx - low)


async def measured_request(endpoint, key, cfg, prompt, case=None, client=None):
    sample = {"created": time.time(), "status": "error", "prompt": prompt,
              "response": "", "reasoning": "", "token_source": "unavailable"}
    if case:
        sample["case_name"] = case["name"]
    started = time.perf_counter()
    first = None
    first_answer = None
    last = None
    max_gap = 0
    finish_reason = None
    usage = {}
    completed = False
    content = []
    reasoning = []
    size = 0
    body = {"model": endpoint["model"], "messages": [{"role": "user", "content": prompt}],
            "stream": True, "stream_options": {"include_usage": True}}
    if cfg.get("temperature") is not None:
        body["temperature"] = cfg["temperature"]
    body.update(cfg.get("extra", {}))
    # Transport and output budget are owned by the runner, not by custom JSON.
    body.update(model=endpoint["model"], stream=True, stream_options={"include_usage": True})
    body.pop("max_tokens", None)
    if cfg.get("max_tokens") is not None:
        body["max_tokens"] = cfg["max_tokens"]
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise ValueError("messages 必须是非空数组")
    sample["prompt"] = json.dumps(body["messages"], ensure_ascii=False)
    owned_client = client is None
    if owned_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(cfg["timeout"], connect=min(15, cfg["timeout"])),
                                   follow_redirects=False, trust_env=False)

    def process(data):
        nonlocal first, first_answer, last, max_gap, finish_reason, usage, completed, size
        if data.strip() == "[DONE]":
            completed = True
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("流式数据不是有效 JSON") from exc
        if not isinstance(event, dict):
            raise ValueError("流式响应结构无效")
        if event.get("error"):
            raise ValueError("流式错误：" + redact(json.dumps(event["error"], ensure_ascii=False), key))
        if event.get("code") not in (None, 0, "0", 200, "200") and not event.get("choices"):
            raise ValueError("接口业务错误：" + redact(json.dumps(event, ensure_ascii=False), key))
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        sample["request_id"] = redact(str(event.get("id", sample.get("request_id", ""))), key)[:200]
        choices = event.get("choices", [])
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or choice.get("message") or {}
        text = delta.get("content") or ""
        thought = delta.get("reasoning_content") or ""
        if not isinstance(text, str) or not isinstance(thought, str):
            raise ValueError("文本增量字段类型无效")
        if text or thought:
            tick = time.perf_counter()
            if first is None:
                first = tick
            if text and first_answer is None:
                first_answer = tick
            if last is not None:
                max_gap = max(max_gap, (tick - last) * 1000)
            last = tick
            content.append(text)
            reasoning.append(thought)
            size += len(text) + len(thought)
            if size > 1_000_000:
                raise ValueError("回答超过本工具的100万字符保护上限")
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
            completed = True

    started = time.perf_counter()
    try:
        async with asyncio.timeout(cfg["timeout"]):
            async with client.stream("POST", completion_url(endpoint["base_url"]),
                                     headers={"Authorization": "Bearer " + key, "Accept": "text/event-stream"}, json=body) as response:
                sample["http_status"] = response.status_code
                if response.status_code != 200:
                    parts = bytearray()
                    async for chunk in response.aiter_bytes():
                        parts.extend(chunk[:max(0, 4096 - len(parts))])
                        if len(parts) >= 4096:
                            break
                    sample["status"] = "rate_limited" if response.status_code == 429 else "error"
                    sample["error"] = redact(f"HTTP {response.status_code}：{parts.decode('utf-8', errors='replace')}", key)
                else:
                    content_type = response.headers.get("content-type", "").lower()
                    if "application/json" in content_type:
                        parts = bytearray()
                        async for chunk in response.aiter_bytes():
                            parts.extend(chunk)
                            if len(parts) > 2_000_000:
                                raise ValueError("非流式响应超过保护上限")
                        event = json.loads(parts)
                        if event.get("error"):
                            raise ValueError("接口错误：" + redact(json.dumps(event["error"], ensure_ascii=False), key))
                        # A gateway ignoring stream=true cannot yield a measured TTFT/TPS.
                        for choice in event.get("choices", [])[:1]:
                            delta = choice.get("message", {})
                            content.append(str(delta.get("content") or ""))
                            reasoning.append(str(delta.get("reasoning_content") or ""))
                            finish_reason = choice.get("finish_reason")
                        usage = event.get("usage") or {}
                        sample["request_id"] = redact(str(event.get("id", "")), key)[:200]
                        completed = bool(event.get("choices"))
                        sample["error"] = "接口未按要求流式返回；首输出延迟与生成速度不计入统计。"
                        last = time.perf_counter()
                    else:
                        event_lines = []
                        event_bytes = 0
                        async for line in response.aiter_lines():
                            if line == "":
                                if event_lines:
                                    process("\n".join(event_lines))
                                    event_lines = []
                                    event_bytes = 0
                                continue
                            if line.startswith("data:"):
                                fragment = line[5:].lstrip(" ")
                                event_lines.append(fragment)
                                event_bytes += len(fragment)
                                if event_bytes > 2_000_000:
                                    raise ValueError("流式事件超过保护上限")
                        if event_lines:
                            process("\n".join(event_lines))
                    if not completed:
                        raise ValueError("连接提前结束：没有收到完成标记")
                    has_text = bool("".join(content).strip())
                    if finish_reason == "length":
                        sample["status"] = "truncated"
                        sample["error"] = ("接口自身输出额度耗尽；本工具未传输出上限。本条结果不完整，不计入能力评分。"
                                           if cfg.get("max_tokens") is None else
                                           f"达到本次输出上限（{cfg['max_tokens']} tokens）。思考可能占用输出预算，可改用更大的场景档位。")
                    elif finish_reason in ("content_filter", "safety"):
                        sample["status"] = "filtered"
                    elif has_text:
                        sample["status"] = "ok"
                    else:
                        sample["status"] = "empty"
    except httpx.ConnectTimeout:
        sample.update(status="timeout", error=f"建立连接超时（连接阶段上限 {min(15, cfg['timeout'])} 秒）")
    except httpx.PoolTimeout:
        sample.update(status="timeout", error="等待可用连接超时")
    except httpx.ReadTimeout:
        sample.update(status="timeout", error=f"等待流式响应超时（读取上限 {cfg['timeout']} 秒）")
    except TimeoutError:
        sample.update(status="timeout", error=f"单次请求总耗时超过 {cfg['timeout']} 秒")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        sample.update(status="error", error=redact(str(exc) or type(exc).__name__, key))
    finally:
        if owned_client:
            await client.aclose()

    ended = time.perf_counter()
    sample.update(response="".join(content), reasoning="".join(reasoning), finish_reason=finish_reason,
                  finish_ms=round((ended - started) * 1000, 2))
    if first is not None:
        sample["ttft_ms"] = round((first - started) * 1000, 2)
    if first_answer is not None:
        sample["answer_ms"] = round((first_answer - started) * 1000, 2)
    if last is not None:
        sample["e2e_ms"] = round((last - started) * 1000, 2)
    if first is not None and last is not None and last > first:
        sample["max_gap_ms"] = round(max_gap, 2)
        sample["cps"] = round((len(sample["response"]) + len(sample["reasoning"])) / (last - first), 2)
    def integer(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    sample["prompt_tokens"] = integer(usage.get("prompt_tokens"))
    sample["output_tokens"] = integer(usage.get("completion_tokens"))
    prompt_details = usage.get("prompt_tokens_details") or {}
    output_details = usage.get("completion_tokens_details") or {}
    sample["cached_tokens"] = integer(prompt_details.get("cached_tokens"))
    sample["reasoning_tokens"] = integer(output_details.get("reasoning_tokens"))
    if sample["output_tokens"] is not None:
        sample["token_source"] = "api"
        if sample["output_tokens"] > 1 and first is not None and last is not None and last > first:
            sample["tps"] = round((sample["output_tokens"] - 1) / (last - first), 2)
    if case and sample["status"] == "ok":
        sample["quality_pass"], sample["quality_note"] = grade(sample["response"], case)
    elif case:
        sample["quality_note"] = "请求未完整完成，未评分"
    return sample
