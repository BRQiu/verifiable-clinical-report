# -*- coding: utf-8 -*-
"""CLI 入口。

    python run.py --files samples/sample_report.csv samples/sample_followup.xlsx \
                  --ask "看看张三最近的血糖和体重变化"

    python run.py --files ... --ask "..." --json out.json     # 存完整结果
    python run.py --files ... --ask "..." --svg out/          # 导出图表
    python run.py --demo                                      # 用样本数据跑一次
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import pipeline  # noqa: E402
from app.llm import get_backend, LLMError  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    ap = argparse.ArgumentParser(description="慢性病多源数据多智能体分析系统")
    ap.add_argument("--files", nargs="*", default=[],
                    help="输入的 Excel/CSV 报告文件")
    ap.add_argument("--ask", default="", help="医生的问题（自然语言）")
    ap.add_argument("--backend", default=None,
                    choices=["auto", "stub", "local", "openai"],
                    help="LLM 后端（默认 auto：能用本地就用本地）")
    ap.add_argument("--json", dest="json_out", default=None, help="结果存 JSON")
    ap.add_argument("--svg", dest="svg_dir", default=None, help="导出图表到目录")
    ap.add_argument("--demo", action="store_true", help="用内置样本跑一次")
    args = ap.parse_args(argv)

    files = list(args.files)
    ask = args.ask

    if args.demo or not files:
        files = [os.path.join(ROOT, "samples", "sample_report.csv"),
                 os.path.join(ROOT, "samples", "sample_followup.xlsx")]
        ask = ask or "看看张三最近一年的血糖、糖化血红蛋白和体重变化"

    if not os.path.exists(files[0]):
        print(f"找不到文件：{files[0]}", file=sys.stderr)
        return 2

    terms = pipeline.load_terms()
    try:
        backend = get_backend(terms, None if args.backend in (None, "auto")
                              else args.backend)
    except LLMError as e:
        print(f"[llm] {e}", file=sys.stderr)
        return 3

    pipe = pipeline.Pipeline(terms=terms, backend=backend)
    pipe.load_files(files)
    result = pipe.run(ask)

    print(pipeline.render_text(result))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
        print(f"\n结果已写入 {args.json_out}")

    if args.svg_dir:
        os.makedirs(args.svg_dir, exist_ok=True)
        for sid, svg in result["charts"].items():
            p = os.path.join(args.svg_dir, f"{sid}.svg")
            open(p, "w", encoding="utf-8").write(svg)
            print(f"图表 {p}")

    return 0 if result["verification"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
