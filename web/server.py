# -*- coding: utf-8 -*-
"""Web 前端后端 —— 选择数据源 + 提问 + 看结果。

    python web/server.py            # 默认 http://127.0.0.1:8100
    python web/server.py --port 9000 --backend stub

设计上刻意保持"薄"：它不碰任何分析逻辑，只做三件事
    ① 列出可选的数据（Excel/CSV 文件、Synthetic Hospital 患者）
    ② 把选中的数据和问题交给 pipeline
    ③ 把结果原样吐给前端（含框架层、命题、图、报告、校验）
所有判断仍然发生在 pipeline 里，前端不会绕过任何校验。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))

from fastapi import FastAPI, HTTPException           # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel                       # noqa: E402

from app import pipeline                             # noqa: E402
from app.adapters.synthetic_hospital import SyntheticHospital  # noqa: E402
from app.llm import get_backend, StubBackend, LLMError         # noqa: E402

app = FastAPI(title="慢性病多源数据分析")

TERMS = pipeline.load_terms()
_SH: SyntheticHospital | None = None
_BACKEND = None          # 由命令行决定
_BACKEND_NAME = "auto"
_CACHE: dict[str, object] = {}      # 后端缓存 —— 本地模型 8GB，绝不能每个请求重载一遍


def backend_for(name: str | None):
    """按名字取后端，带缓存。

    本地 Qwen3-4B 加载一次要 11 秒、占 7.5GB 显存；如果每个请求都重新构造，
    网页点两次就把显存耗光了。所以这里必须缓存。
    """
    global _BACKEND, _BACKEND_NAME
    want = name or _BACKEND_NAME
    if want in ("auto", _BACKEND_NAME) and _BACKEND is not None:
        return _BACKEND
    if want not in _CACHE:
        _CACHE[want] = (StubBackend(TERMS) if want == "stub"
                        else get_backend(TERMS, None if want == "auto" else want))
    return _CACHE[want]


def sh() -> SyntheticHospital:
    global _SH
    if _SH is None:
        _SH = SyntheticHospital(terms=TERMS)
    return _SH


def sh_available() -> bool:
    try:
        sh()
        return True
    except FileNotFoundError:
        return False


# ==========================================================================
# 数据源
# ==========================================================================
@app.get("/api/sources")
def sources():
    files = []
    for pat in ("samples/*.csv", "samples/*.xlsx", "*.csv"):
        for p in sorted(glob.glob(os.path.join(ROOT, pat))):
            files.append({"name": os.path.relpath(p, ROOT).replace("\\", "/"),
                          "size_kb": round(os.path.getsize(p) / 1024, 1)})
    return {
        "sources": [
            {"id": "sh", "name": "Synthetic Hospital",
             "desc": "1,268 名纵向患者 · 开放 · 医生验证",
             "available": sh_available()},
            {"id": "files", "name": "本地 Excel / CSV 报告",
             "desc": "上一轮做的识别器，支持多文件合并",
             "available": bool(files)},
        ],
        "files": files,
        "backends": ["stub", "local", "openai"],
        "current_backend": _BACKEND_NAME,
    }


SEX_CN = {"F": "女", "M": "男", "Female": "女", "Male": "男"}


@app.get("/api/patients")
def patients(limit: int = 60):
    """患者列表。

    刻意**不显示英文诊断** —— 数据集里有 2111 种细碎英文诊断，翻不全，
    而且选患者时真正该看的是"他有什么数据"，不是诊断名。
    """
    if not sh_available():
        raise HTTPException(404, "Synthetic Hospital 数据未就绪")
    ps = sh().patients(limit=limit)
    return {"patients": [
        {"patient_id": p["patient_id"],
         "age": p["age"],
         "sex": SEX_CN.get(p["sex"], p["sex"]),
         "n_encounters": p["num_encounters"],
         "n_lab_sections": p["n_lab_sections"]}
        for p in ps]}


@app.get("/api/patient/{pid}/preview")
def preview(pid: int):
    """抽取结果预览 —— 让使用者先看到这个患者**有什么数据**。

    指标一律给中文名（界面不出现 hba1c/sbp 这类内部 id）。
    """
    s = sh()
    recs = s.load(pid)
    info = s.meta(pid)

    metric: dict[str, list] = {}
    for r in recs:
        metric.setdefault(r["metric"], []).append(r)

    items = []
    for m, v in metric.items():
        meta = TERMS["metrics"].get(m, {})
        times = [x["time"] for x in v]
        items.append({
            "id": m,
            "name": meta.get("name", m),
            "unit": meta.get("unit", ""),
            "n": len(v),
            "first": min(times).strftime("%Y-%m-%d"),
            "last": max(times).strftime("%Y-%m-%d"),
            "sources": sorted({x["source"] for x in v if x["source"]}),
            "flags": sorted({f for x in v for f in x["flags"]}),
        })
    items.sort(key=lambda x: (-x["n"], x["name"]))

    return {
        "patient_id": pid,
        "meta": {"age": info.get("age"), "sex": info.get("sex"),
                 "n_encounters": info.get("num_encounters")},
        "n_records": len(recs),
        "n_metrics": len(items),
        "items": items,
        "dropped_units": dict(s.dropped_units),
    }


# ==========================================================================
# 分析
# ==========================================================================
class AskBody(BaseModel):
    source: str                     # 'sh' | 'files'
    question: str
    patient_id: int | None = None
    files: list[str] | None = None
    backend: str | None = None
    dataset_version: int = 1
    merge_groups: bool = True       # 收缩压/舒张压是否合并到一张图


@app.post("/api/analyze")
def analyze(body: AskBody):
    try:
        backend = backend_for(body.backend)
    except LLMError as e:
        raise HTTPException(400, str(e))

    pipe = pipeline.Pipeline(terms=TERMS, backend=backend,
                             dataset_version=body.dataset_version,
                             merge_chart_groups=body.merge_groups)
    try:
        if body.source == "sh":
            if body.patient_id is None:
                raise HTTPException(400, "没选患者")
            s = sh()
            recs = s.load(body.patient_id)
            if not recs:
                raise HTTPException(400, f"患者 {body.patient_id} 没抽出任何可用记录")
            info = s.meta(body.patient_id)
            pipe.load_records(recs, {"SyntheticHospital": {
                "患者": str(body.patient_id), "年龄": str(info.get("age")),
                "性别": str(info.get("sex")),
                "就诊次数": str(info.get("num_encounters"))}})
            label = f"SH#{body.patient_id}"
        else:
            files = [os.path.join(ROOT, f) if not os.path.isabs(f) else f
                     for f in (body.files or [])]
            if not files:
                raise HTTPException(400, "没选文件")
            pipe.load_files(files)
            label = os.path.basename(files[0])

        result = pipe.run(body.question)
        result["source_label"] = label
        result["backend"] = backend.name
        return JSONResponse(json.loads(json.dumps(result, ensure_ascii=False,
                                                  default=str)))
    except HTTPException:
        raise
    except Exception as e:                      # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/api/health")
def health():
    return {"ok": True, "backend": _BACKEND_NAME,
            "sh_available": sh_available(),
            "models": sorted(os.path.basename(p) for p in
                             glob.glob(os.path.join(ROOT, "models", "*")))}


# ==========================================================================
@app.get("/", response_class=HTMLResponse)
def index():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
    return open(p, encoding="utf-8").read()


def _open_when_ready(host: str, port: int):
    """轮询 /api/health，服务真的起来了再开浏览器。

    比在 bat 里 sleep 固定秒数可靠 —— 服务没就绪时打开会看到错误页。
    """
    import time
    import urllib.request
    import webbrowser

    base = f"http://{host}:{port}"
    for _ in range(120):                       # 最多等 60 秒
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            break
        except Exception:                      # noqa: BLE001
            time.sleep(0.5)
    webbrowser.open(base)


def main():
    global _BACKEND, _BACKEND_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--backend", default="stub",
                    choices=["auto", "stub", "local", "openai"])
    ap.add_argument("--open", action="store_true", help="就绪后自动打开浏览器")
    a = ap.parse_args()
    _BACKEND_NAME = a.backend
    _BACKEND = (StubBackend(TERMS) if a.backend == "stub"
                else get_backend(TERMS, None if a.backend == "auto" else a.backend))
    print(f"LLM 后端: {_BACKEND.name}")
    print(f"Synthetic Hospital: {'可用' if sh_available() else '未就绪'}")
    print(f"\n  →  http://{a.host}:{a.port}\n")
    if a.open:
        import threading
        threading.Thread(target=_open_when_ready, args=(a.host, a.port),
                         daemon=True).start()
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
