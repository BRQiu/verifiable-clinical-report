# -*- coding: utf-8 -*-
"""启动器 —— bat 只负责设环境变量，中文界面全在这里。

为什么中文不放 bat 里：cmd.exe 按系统 ANSI 码页（中文 Windows 是 GBK）解析
.bat 的字节，UTF-8 的中文会被拆成非法命令 —— 之前连 "python" 都被截断成 "ython"。
Python 这边原生 UTF-8，没有这个问题。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))

PORT = 8100
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:          # noqa: BLE001
        pass


def line(t=""):
    print(t, flush=True)


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def open_when_ready():
    for _ in range(120):
        try:
            urllib.request.urlopen(URL + "/api/health", timeout=1)
            break
        except Exception:      # noqa: BLE001
            time.sleep(0.5)
    webbrowser.open(URL)


def main() -> int:
    line()
    line("  " + "=" * 58)
    line("    慢性病多源数据分析")
    line("  " + "=" * 58)
    line()

    # ---- 依赖检查 ----
    pylibs = os.path.join(ROOT, ".pylibs")
    if not os.path.isdir(pylibs):
        line("  [警告] 没找到 .pylibs 依赖目录，将回退到系统 Python 的包")
        line("         如果启动失败，请按 README 的「环境安装」装一遍")
        line()
    try:
        import fastapi, uvicorn              # noqa: F401
    except ImportError as e:
        line(f"  [错误] 缺少依赖：{e.name}")
        line("         fastapi / uvicorn 必须装上，见 README")
        return 2

    # ---- 服务已经在跑？直接开页面 ----
    if port_busy(PORT):
        line("  检测到服务已经在运行，直接打开页面 ...")
        line(f"  →  {URL}")
        webbrowser.open(URL)
        time.sleep(1.5)
        return 0

    # ---- 选后端 ----
    line("  请选择推理后端：")
    line()
    line("    [1] 快速模式  —— 确定性桩，秒出结果          （默认）")
    line("    [2] 真实模型  —— Qwen3-4B，约 12 秒/次")
    line("                     首次调用要加载模型，约 30 秒")
    line()
    line("  （5 秒内不选，默认用 [1]）")
    line()

    backend = "stub"
    try:
        ans = input("  请输入 1 或 2：").strip()
        if ans == "2":
            backend = "local"
    except EOFError:
        pass
    line()
    line(f"  已选择：{backend}")
    line()

    # ---- 启动 ----
    line("  " + "-" * 58)
    line("   正在启动服务，浏览器会自动打开")
    line("   停止服务：关闭本窗口，或按 Ctrl+C")
    line("  " + "-" * 58)
    line()

    from app import pipeline
    from app.llm import LLMError, StubBackend, get_backend
    import web.server as srv

    terms = pipeline.load_terms()
    srv._BACKEND_NAME = backend
    try:
        srv._BACKEND = (StubBackend(terms) if backend == "stub"
                        else get_backend(terms, backend))
    except LLMError as e:
        line(f"  [错误] {e}")
        line("  提示：先用 [1] 快速模式，或在网页里切到别的后端")
        return 3

    line(f"  LLM 后端：{srv._BACKEND.name}")
    line(f"  Synthetic Hospital：{'可用' if srv.sh_available() else '未就绪'}")
    line(f"\n  →  {URL}\n")

    threading.Thread(target=open_when_ready, daemon=True).start()

    import uvicorn
    try:
        uvicorn.run(srv.app, host=HOST, port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass
    line()
    line("  服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
