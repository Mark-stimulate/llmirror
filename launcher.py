"""Portable entry point for the Windows executable and Linux Python runtime."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn
from app import create_app
from store import Store, password_hash


def root_dir():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def bundle_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def acquire_lock(folder):
    handle = open(folder / "app.lock", "a+b")
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        handle.close()
        return None
    return handle


def open_when_ready(url):
    for _ in range(100):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + "/api/health", timeout=1) as response:
                if json.load(response).get("product") == "llmirror":
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.3)


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="词镜 llmirror · Windows / Linux API 实测看板")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    args = parser.parse_args()
    root = root_dir()
    config_path = root / "server.json"
    config = {"host": "127.0.0.1", "port": 8765}
    if config_path.exists():
        config.update(json.loads(config_path.read_text(encoding="utf-8-sig")))
    else:
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    host = args.host or config["host"]
    port = args.port or int(config["port"])
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在1—65535之间")
    data_dir = Path(args.data_dir).resolve() if args.data_dir else root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.backup:
        migration_lock = None
        if not (data_dir / "llmirror.db").exists() and (data_dir / "pulse.db").exists():
            migration_lock = acquire_lock(data_dir)
            if migration_lock is None:
                raise RuntimeError("请先停止旧实例，再执行首次数据迁移或备份。")
        try:
            print("备份已创建：", Store(data_dir).backup())
        finally:
            if migration_lock is not None:
                migration_lock.close()
        return
    lock = acquire_lock(data_dir)
    url = f"http://127.0.0.1:{port}"
    if lock is None:
        print("词镜已在运行。请先停止已有窗口或 Windows 服务后，再调整运行方式。")
        if not args.no_browser:
            open_when_ready(url)
        return
    if args.reset_password:
        import getpass
        store = Store(data_dir)
        username = input("要重置的账号：").strip()
        user = store.one("SELECT id FROM users WHERE username=?", (username,))
        if not user:
            print("账号不存在。")
            return
        first = getpass.getpass("新密码（至少6个字符，不显示输入）：")
        second = getpass.getpass("再输入一次：")
        if first != second or not 6 <= len(first) <= 128:
            print("两次密码不一致，或长度不符合要求。未修改。")
            return
        with store.connection() as db:
            db.execute("UPDATE users SET password=? WHERE id=?", (password_hash(first), user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        print("密码已重置。请重新启动词镜。")
        return
    logs = data_dir / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(logs / "runtime.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")], format="%(asctime)s %(levelname)s %(name)s %(message)s")
    assets = root / "static"
    if not (assets / "index.html").is_file():
        assets = bundle_dir() / "static"
    app = create_app(data_dir, assets)
    print("\n  词镜 llmirror · API 实测看板 v1.2.3\n")
    print("  本机访问：", url)
    if host == "0.0.0.0":
        try:
            addresses = sorted({x[4][0] for x in socket.getaddrinfo(socket.gethostname(), port, socket.AF_INET)})
            for address in addresses:
                if not address.startswith("127."):
                    print(f"  局域网参考：http://{address}:{port} （需放行防火墙）")
        except OSError:
            pass
    print("  数据目录：", data_dir)
    print("  首次使用请在本机网页创建管理员账号。\n  此窗口保持运行；按 Ctrl+C 可停止。\n")
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    try:
        uvicorn.run(app, host=host, port=port, workers=1, access_log=False, log_level="warning",
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1", timeout_keep_alive=5,
                    timeout_graceful_shutdown=15, limit_concurrency=100)
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("启动失败：", str(exc))
        print("请确认目录可写、端口未占用，或查看使用说明。")
        if sys.stdin and sys.stdin.isatty():
            input("按回车退出…")
        raise SystemExit(1)
