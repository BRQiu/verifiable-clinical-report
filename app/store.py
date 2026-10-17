# -*- coding: utf-8 -*-
"""共享事实库 —— 智能体之间不传消息，只读写这里。

这是前面讨论的核心架构决策：
  · 没有"数据传输"，数据在库里不动，各环节按需读取
  · 只传引用（cid / rid），所以"传输失真"在原理上不可能发生
  · 写入时校验（契约），不是传递后校验 —— 那一刻上游信息还在
  · 数据集带版本戳，所以"报告静默过时"是可检测的
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime


class FactStore:
    def __init__(self, terms: dict, dataset_version: int = 1):
        self.terms = terms
        self.dataset_version = dataset_version

        self.requirements: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.charts: dict[str, dict] = {}
        self.sentences: list[dict] = []

        self.series: dict[str, list[tuple]] = defaultdict(list)  # metric -> [(time, value, source)]
        self.report_meta: dict[str, dict] = {}
        self.audit: list[str] = []          # 写入被拒的记录

    # ---------------- 数据层 ----------------
    def load_records(self, records: list[dict], source_meta: dict | None = None):
        """装填清洗后的长表。value 是裸数值，单位在术语表里。"""
        for r in records:
            if not r.get("metric") or r.get("value") is None or r.get("time") is None:
                continue
            self.series[r["metric"]].append((r["time"], r["value"],
                                             r.get("source", ""),
                                             "|".join(r.get("flags", []))))
        for k, v in self.series.items():
            v.sort(key=lambda x: x[0])
        if source_meta:
            self.report_meta.update(source_meta)

    def values(self, metric: str) -> list[tuple]:
        return self.series.get(metric, [])

    def inventory(self) -> dict:
        """元数据清单 —— 预检和选图只看这个，不碰数据值。"""
        inv = {}
        for m, pts in self.series.items():
            if not pts:
                continue
            times = [p[0] for p in pts]
            span = (max(times) - min(times)).days or 1
            inv[m] = {
                "n": len(pts),
                "first": min(times).isoformat(),
                "last": max(times).isoformat(),
                "span_days": span,
                "sources": sorted({p[2] for p in pts if p[2]}),
            }
        return inv

    # ---------------- 写入（带契约检查） ----------------
    def _reject(self, kind: str, obj: dict, problems: list[str]):
        self.audit.append(f"REJECT {kind} {obj.get('cid') or obj.get('rid') or obj.get('sid')}: {problems}")

    def put_requirement(self, r: dict, validator) -> bool:
        p = validator(r, self.terms)
        if p:
            self._reject("requirement", r, p)
            return False
        self.requirements[r["rid"]] = r
        return True

    def put_claim(self, c: dict, validator) -> bool:
        p = validator(c, self.terms)
        if p:
            self._reject("claim", c, p)
            return False
        if c["rid"] not in self.requirements:
            self._reject("claim", c, [f"rid {c['rid']} 不存在"])
            return False
        for op in c.get("operands", []):
            if op not in self.claims:
                self._reject("claim", c, [f"operand {op} 不存在"])
                return False
        self.claims[c["cid"]] = c
        return True

    def put_chart(self, s: dict, validator) -> bool:
        p = validator(s, self.terms, set(self.claims))
        if p:
            self._reject("chart", s, p)
            return False
        self.charts[s["sid"]] = s
        return True

    def put_sentence(self, sent: dict):
        self.sentences.append(sent)

    # ---------------- 导出 ----------------
    def to_long_csv(self, path: str):
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["metric", "time", "value", "unit_internal", "source", "flags"])
            for m, pts in sorted(self.series.items()):
                unit = self.terms["metrics"][m]["unit"]
                for t, v, s, fl in pts:
                    w.writerow([m, t.isoformat(sep=" "), v, unit, s, fl])

    def summary(self) -> dict:
        return {
            "dataset_version": self.dataset_version,
            "metrics": len(self.series),
            "points": sum(len(v) for v in self.series.values()),
            "requirements": len(self.requirements),
            "claims": len(self.claims),
            "charts": len(self.charts),
            "sentences": len(self.sentences),
            "rejected": len(self.audit),
        }
