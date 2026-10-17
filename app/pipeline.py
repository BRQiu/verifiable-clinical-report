# -*- coding: utf-8 -*-
"""编排 —— 把六个智能体串起来，但注意：它们之间不传消息，只读写共享事实库。

流程：
    医生的话
      →[IntentAgent LLM]→ 需求 R          → 写入（契约校验）
      →[PrecheckAgent]  → 通过/缺口        ← 只看元数据
      →[AnalyzeAgent]   → 命题（含衍生值）  → 写入（重算校验）
      →[ChartSelectAgent]→ 图表规格         ← 只看需求+术语表
      →[RenderAgent]    → SVG
      →[ReportAgent LLM]→ 报告句（占位符）  → 代码替换数字
      →[verify]         → 零成本校验 + 出口语义校验
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ingest  # noqa: E402

from . import agents, schemas, verify
from .llm import get_backend
from .store import FactStore

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TERMS_PATH = os.path.join(ROOT, "terms.json")


def load_terms(path: str = TERMS_PATH) -> dict:
    return json.load(open(path, encoding="utf-8"))


class Pipeline:
    def __init__(self, terms: dict | None = None, backend=None, dataset_version: int = 1,
                 merge_chart_groups: bool = True):
        self.terms = terms or load_terms()
        self.backend = backend or get_backend(self.terms)
        self.store = FactStore(self.terms, dataset_version)
        # 收缩压/舒张压这类同组指标默认合并到一张图；关掉就每个指标一张
        self.merge_chart_groups = merge_chart_groups

        self.intent = agents.IntentAgent(self.backend)
        self.precheck = agents.PrecheckAgent()
        self.analyze = agents.AnalyzeAgent(self.backend)
        self.select = agents.ChartSelectAgent()
        self.render = agents.RenderAgent()
        self.reporter = agents.ReportAgent(self.backend)

        self.trace: list[str] = []

    # ---------------- 数据 ----------------
    def load_files(self, paths: list[str]):
        recs = ingest.ingest(paths, TERMS_PATH)
        meta = {}
        for p in paths:
            rows = ingest.read_grid(p)
            h = ingest.find_header(rows)
            if h >= 0:
                meta[os.path.basename(p)] = ingest.extract_meta(rows, h)
        self.store.load_records(recs, meta)
        self.trace.append(f"载入 {len(paths)} 个文件，{len(recs)} 条记录，"
                          f"{len(self.store.series)} 个指标")
        return recs

    def load_records(self, records: list[dict], meta: dict | None = None):
        """直接装标准长表记录 —— 给适配器（如 Synthetic Hospital）用。

        适配器负责"数据源 → 标准长表"，进了这里之后流程完全一致。
        """
        self.store.load_records(records, meta)
        self.trace.append(f"载入 {len(records)} 条记录，{len(self.store.series)} 个指标")
        return records

    # ---------------- 跑一遍 ----------------
    def run(self, question: str) -> dict:
        t0 = datetime.now()
        s = self.store

        # ① 意图解析（LLM）
        raw_reqs = self.intent.run(question, s)
        accepted = []
        for r in raw_reqs:
            if s.put_requirement(r, schemas.validate_requirement):
                accepted.append(r)
        self.trace.append(f"意图解析：{len(raw_reqs)} 条需求，接受 {len(accepted)} 条"
                          + (f"，拒绝 {len(raw_reqs)-len(accepted)} 条" if len(raw_reqs) != len(accepted) else ""))

        # ② 可满足性预检（代码，只看元数据）
        passed, gaps = self.precheck.run(accepted, s)
        for r in passed:
            if r.get("flags"):
                s.requirements[r["rid"]] = r
        self.trace.append(f"预检：通过 {len(passed)}，缺口 {len(gaps)}")

        # ③ 分析（LLM 规划 + 代码计算）
        all_claims = []
        for r in passed:
            cs = self.analyze.compute(r, s)
            for c in cs:
                if s.put_claim(c, schemas.validate_claim):
                    all_claims.append(c)
        self.trace.append(f"分析：产出 {len(all_claims)} 条命题"
                          f"（其中衍生 {sum(1 for c in all_claims if c['operands'])} 条）")

        # ④ 选图 + 画图
        specs = []
        for r in passed:
            spec = self.select.pick(r, s)
            if spec:
                specs.append(spec)
        if self.merge_chart_groups:
            specs = agents.merge_by_group(specs, s)

        svgs = {}
        for spec in specs:
            svg = self.render.render(spec, s, all_claims)
            if s.put_chart(spec, schemas.validate_chart):
                svgs[spec["sid"]] = svg
        merged = sum(1 for sp in specs if len(sp.get("series_defs", [])) > 1)
        self.trace.append(f"图表：{len(svgs)} 张"
                          + (f"（其中 {merged} 张为同组合并）" if merged else ""))

        # ⑤ 报告（LLM 生成占位符 + 代码替换）
        raw_sentences = self.reporter.run(s)
        resolved = self.reporter.resolve(raw_sentences, s)
        for sent in resolved:
            s.put_sentence(sent)
        self.trace.append(f"报告：{len(resolved)} 句")

        # ⑥ 校验
        report = verify.verify_all(s, resolved)

        return {
            "question": question,
            "requirements": list(s.requirements.values()),
            "gaps": gaps,
            "claims": list(s.claims.values()),
            "charts": {k: v for k, v in svgs.items()},
            "sentences": resolved,
            "verification": report,
            "framing": self.framing(),
            "rejected_writes": s.audit,
            "summary": s.summary(),
            "elapsed_ms": int((datetime.now() - t0).total_seconds() * 1000),
            "backend": self.backend.name,
            "trace": self.trace,
        }

    # ---------------- 框架层：系统替医生做了什么口径决定 ----------------
    def framing(self) -> dict:
        """这些是"医生没说、系统补的"口径 —— 必须可见，否则医生无从判断。"""
        s = self.store
        inv = s.inventory()
        # "使用"必须是需求真正用到的指标，不是碰巧载入的全部指标
        used = sorted({r["metric"] for r in s.requirements.values()})
        return {
            "dataset_version": s.dataset_version,
            "指标范围": {
                "使用": used,
                "该患者共有": sorted(inv),
                "未使用": sorted(set(inv) - set(used)),
            },
            "窗口": {r["metric"]: f"{r['window_days']} 天" for r in s.requirements.values()},
            "基线": {m: (min(p[0] for p in pts).isoformat(sep=" ")
                         if (pts := s.values(m)) else None) for m in used},
            "数据源": {m: sorted({p[2] for p in s.values(m)}) for m in used},
            "数据完整度": {m: {"n": inv[m]["n"], "跨度天": inv[m]["span_days"],
                               "来源": inv[m]["sources"]} for m in used if m in inv},
            "来源文件": list(s.report_meta),
        }


def render_text(result: dict) -> str:
    """把结果渲染成给人看的文本。"""
    L = []
    L.append("=" * 72)
    L.append(f"问题：{result['question']}")
    L.append(f"后端：{result['backend']}   耗时：{result['elapsed_ms']} ms")
    L.append("=" * 72)

    L.append("\n【需求】")
    for r in result["requirements"]:
        L.append(f"  {r['rid']}  {r['metric']}  {r['window_days']}天  {r['aspect']}"
                 + (f"  {r.get('flags')}" if r.get("flags") else ""))

    if result["gaps"]:
        L.append("\n【预检缺口 —— 系统不假装覆盖】")
        for g in result["gaps"]:
            L.append(f"  {g['rid']} {g['metric']}: {g['reason']}")

    L.append("\n【框架层 —— 系统替医生做的口径决定】")
    f = result["framing"]
    L.append(f"  指标使用：{f['指标范围']['使用']}")
    L.append(f"  该患者共有：{f['指标范围']['该患者共有']}")
    L.append(f"  未使用：{f['指标范围']['未使用']}")
    L.append(f"  窗口：{f['窗口']}")
    L.append(f"  数据源：{f['数据源']}")

    L.append(f"\n【命题】{len(result['claims'])} 条")
    for c in result["claims"][:14]:
        deriv = f"  ←{c['operands']}" if c["operands"] else ""
        L.append(f"  {c['cid']} {c['metric']:18s} {c['statistic']:6s} "
                 f"{str(c['value']):>9s} {c['unit']:12s} n={c['n']} "
                 f"miss={c['missing_rate']:.0%}{deriv}")
    if len(result["claims"]) > 14:
        L.append(f"  ... 还有 {len(result['claims'])-14} 条")

    L.append(f"\n【报告】{len(result['sentences'])} 句")
    for sent in result["sentences"]:
        mark = "✗" if sent["unresolved"] else " "
        L.append(f" {mark} {sent['resolved']}")

    v = result["verification"]
    L.append(f"\n【校验】reject={v['counts']['reject']} flag={v['counts']['flag']} "
             f"info={v['counts']['info']}  →  {'通过' if v['passed'] else '未通过'}")
    for fnd in v["rejects"][:10]:
        L.append(f"  [reject] {fnd['check']} @{fnd['target']}: {fnd['detail']}")
    for fnd in v["flags"][:10]:
        L.append(f"  [flag]   {fnd['check']} @{fnd['target']}: {fnd['detail']}")
    for fnd in v["info"][:5]:
        L.append(f"  [info]   {fnd['check']} @{fnd['target']}: {fnd['detail']}")

    if result["rejected_writes"]:
        L.append("\n【被拒绝的写入 —— 契约在写入时就拦下】")
        for a in result["rejected_writes"][:8]:
            L.append(f"  {a}")

    return "\n".join(L)
