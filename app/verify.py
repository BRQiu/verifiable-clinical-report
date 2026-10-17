# -*- coding: utf-8 -*-
"""校验层 —— 校验是"边"的属性，不是流水线上的一个节点。

八个边界里七条是零成本的确定性检查，只有出口的语义忠实性需要 LLM。

要特别注意两个检查的分工（否则会重复计算或漏掉）：
  · 重算比对   —— 命题自己声明的窗口 vs 实际算出来的值
                  它只能证明"实现没 bug"，**不能**证明"窗口选对了"
                  （因为窗口是分析层写的，重算又用它，这是循环的）
  · 需求一致性 —— 命题声明的窗口/指标 vs 需求 R
                  这才是"窗口选错了"唯一能被自动发现的地方
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import schemas


class Finding:
    def __init__(self, level, check, target, detail):
        self.level = level          # reject | flag | info
        self.check = check
        self.target = target
        self.detail = detail

    def __repr__(self):
        return f"[{self.level}] {self.check} @{self.target}: {self.detail}"

    def to_dict(self):
        return {"level": self.level, "check": self.check,
                "target": self.target, "detail": self.detail}


# --------------------------------------------------------------------------
# 边界 3：重算 —— 命题声明的口径下，统计量对不对
# --------------------------------------------------------------------------
def _window_points(store, claim):
    m = claim["metric"]
    end = schemas.dt(claim["window_end"]) or max(p[0] for p in store.values(m))
    start = schemas.dt(claim["window_start"]) or (end - timedelta(days=365))
    pts = [p for p in store.values(m) if start <= p[0] <= end]
    return pts or store.values(m)[-1:]


def recompute(store, claim) -> float | None:
    pts = _window_points(store, claim)
    vals = [p[1] for p in pts]
    stat = claim["statistic"]

    if not vals:
        return None
    if stat == "latest":
        return vals[-1]
    if stat == "mean":
        return sum(vals) / len(vals)
    if stat == "min":
        return min(vals)
    if stat == "max":
        return max(vals)
    if stat == "count":
        return float(len(vals))
    if stat == "delta":
        return None if claim.get("prev_value") is None else vals[-1] - claim["prev_value"]
    if stat == "slope":
        if len(pts) < 3:
            return None
        t0 = pts[0][0]
        xs = [(p[0] - t0).days for p in pts]
        n = len(xs)
        mx, my = sum(xs) / n, sum(vals) / n
        den = sum((x - mx) ** 2 for x in xs)
        per_day = 0.0 if den == 0 else sum((x - mx) * (v - my) for x, v in zip(xs, vals)) / den
        return per_day * 30.0        # 口径与 AnalyzeAgent 一致：每月
    return None


def check_recompute(store, tol=1e-4) -> list[Finding]:
    out = []
    for c in store.claims.values():
        got = recompute(store, c)
        want = c["value"]
        if want is None and got is None:
            continue
        if want is None or got is None:
            out.append(Finding("flag", "重算", c["cid"],
                               f"值不可比：声明={want} 重算={got}"))
            continue
        scale = max(1.0, abs(want))
        if abs(got - want) / scale > tol:
            out.append(Finding("reject", "重算", c["cid"],
                               f"{c['statistic']} 声明 {want} 但重算得 {got:.6f}"))
    return out


# --------------------------------------------------------------------------
# 边界 3b：需求一致性 —— 声明的口径是否满足需求（这一条不需要数据）
# --------------------------------------------------------------------------
def check_requirement_consistency(store) -> list[Finding]:
    out = []
    for c in store.claims.values():
        r = store.requirements.get(c["rid"])
        if not r:
            out.append(Finding("reject", "需求一致性", c["cid"], f"rid {c['rid']} 不存在"))
            continue
        if c["metric"] != r["metric"]:
            out.append(Finding("reject", "需求一致性", c["cid"],
                               f"指标 {c['metric']} != 需求 {r['metric']}"))
    return out


# --------------------------------------------------------------------------
# 边界 6：数字有依据 —— 报告句里的裸数字必须来自它引用的命题
# --------------------------------------------------------------------------
def check_numbers(store, sentences) -> list[Finding]:
    out = []
    for i, s in enumerate(sentences):
        refs = s.get("claim_ids") or []
        if not refs:
            out.append(Finding("reject", "引用完整性", f"句{i+1}",
                               "这一句没有引用任何命题"))
            continue
        if s.get("unresolved"):
            out.append(Finding("reject", "占位符解析", f"句{i+1}",
                               f"无法解析：{s['unresolved']}"))
            continue

        # 句子里允许出现的数字：引用命题的 display 值
        allowed = set()
        for cid in refs:
            c = store.claims.get(cid)
            if not c:
                out.append(Finding("reject", "引用完整性", f"句{i+1}", f"引用不存在的 {cid}"))
                continue
            allowed.add(c["display"])
            allowed.add(c["display"].split(" ")[0])
            if c["value"] is not None:
                allowed.add(f"{c['value']:g}")
                allowed.add(f"{abs(c['value']):g}")
                allowed.add(f"{c['value']:.1f}")

        for num in schemas.numbers_in(s["raw"]):
            forms = {f"{num:g}", f"{num:.1f}", f"{num:.2f}", f"{int(num)}"}
            if forms & allowed:
                continue
            # n / 缺失率这类元数据数字也是允许的
            if any(f"{num:g}" in c["display"] for cid in refs
                   if (c := store.claims.get(cid))):
                continue
            out.append(Finding("reject", "数字有依据", f"句{i+1}",
                               f"数字 {num:g} 不在引用命题里"))
    return out


# --------------------------------------------------------------------------
# 边界 6b：越界 —— 不能出现临床评价词
# --------------------------------------------------------------------------
def check_boundary(store, sentences) -> list[Finding]:
    out = []
    for i, s in enumerate(sentences):
        hits = schemas.find_banned(s.get("resolved") or s.get("raw") or "")
        if hits:
            out.append(Finding("reject", "临床越界", f"句{i+1}",
                               f"出现评价性词：{hits}"))
    return out


# --------------------------------------------------------------------------
# 横切①：前向回溯 —— 每个需求都被回答了吗
# --------------------------------------------------------------------------
def check_coverage(store) -> list[Finding]:
    out = []
    for rid, r in store.requirements.items():
        # 预检已判定"没有任何数据"的需求，缺口已经报过了，这里不重复报
        if r.get("unmet"):
            continue
        has_claim = any(c["rid"] == rid for c in store.claims.values())
        has_chart = any(c["rid"] == rid for c in store.charts.values())
        if not has_claim:
            out.append(Finding("reject", "覆盖性", rid,
                               f"需求 {r['metric']} 没有任何命题"))
        elif not has_chart:
            out.append(Finding("flag", "覆盖性", rid,
                               f"需求 {r['metric']} 没有对应图表"))
    return out


# --------------------------------------------------------------------------
# 横切②：引用完整性 —— 图表引用的命题存在吗
# --------------------------------------------------------------------------
def check_references(store) -> list[Finding]:
    out = []
    ids = set(store.claims)
    for s in store.charts.values():
        for cid in s.get("claims", []):
            if cid not in ids:
                out.append(Finding("reject", "引用完整性", s["sid"],
                                   f"图表引用不存在的命题 {cid}"))
    for c in store.claims.values():
        for op in c.get("operands", []):
            if op not in ids:
                out.append(Finding("reject", "引用完整性", c["cid"],
                                   f"衍生值引用不存在的命题 {op}"))
    return out


# --------------------------------------------------------------------------
# 图表误导性 lint（纯规格检查，看图之前就能判）
# --------------------------------------------------------------------------
def check_chart_lint(store) -> list[Finding]:
    out = []
    for s in store.charts.values():
        pts = s.get("series") or []
        if not pts:
            continue
        ys = [p[1] for p in pts]
        flags = " ".join(s.get("flags", []))

        if s["chart_type"] == "line" and len(pts) < 5:
            out.append(Finding("flag", "lint:点太少画折线", s["sid"],
                               f"n={len(pts)}，连线会暗示不存在的趋势"))
        if "窗口平均值" in flags:
            out.append(Finding("flag", "lint:窗口重叠", s["sid"],
                               "该指标是窗口平均值，相邻点时间窗重叠，连线趋势会失真"))
        if "window_shortened" in flags:
            out.append(Finding("flag", "lint:窗口被缩短", s["sid"],
                               "数据不足以覆盖请求窗口，已缩短并标注"))
        if len(set(ys)) == 1 and len(ys) > 2:
            out.append(Finding("info", "lint:全平数据", s["sid"],
                               "全部数值相同，纵轴自动扩展以免退化"))
    return out


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def verify_all(store, sentences) -> dict:
    findings: list[Finding] = []
    findings += check_references(store)
    findings += check_requirement_consistency(store)
    findings += check_recompute(store)
    findings += check_numbers(store, sentences)
    findings += check_boundary(store, sentences)
    findings += check_coverage(store)
    findings += check_chart_lint(store)

    by_level = {"reject": [], "flag": [], "info": []}
    for f in findings:
        by_level[f.level].append(f)

    return {
        "findings": [f.to_dict() for f in findings],
        "rejects": [f.to_dict() for f in by_level["reject"]],
        "flags": [f.to_dict() for f in by_level["flag"]],
        "info": [f.to_dict() for f in by_level["info"]],
        "passed": not by_level["reject"],
        "counts": {k: len(v) for k, v in by_level.items()},
    }
