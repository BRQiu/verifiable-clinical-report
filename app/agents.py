# -*- coding: utf-8 -*-
"""六个智能体。

注意"智能体"不等于"LLM"：
    IntentAgent       LLM    自然语言 -> 结构化需求
    PrecheckAgent     代码    只用元数据，不碰数据值
    CleanAgent        代码    ← 复用 ingest.py
    AnalyzeAgent      LLM规划 + 代码计算（所有计算都在这里）
    ChartSelectAgent  规则    需求 + 术语表 -> 图型
    RenderAgent       代码    规格 -> SVG（纯映射，不做任何变换）
    ReportAgent       LLM    命题 -> 自然语言（数字用占位符，一个都不生成）

七个环节里只有三个用到 LLM。中间那一段全是确定性的 —— 这正是
"上游 LLM 不可能污染下游 LLM"的物理保证。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from . import schemas
from .store import FactStore

import re as _re

# 模型经常把好几句挤在一行，或者带编号。按句号切，并清掉编号和项目符号。
_SENT_SPLIT = _re.compile(r"(?<=[。！？；;])\s*|\n+")
_SENT_CLEAN = _re.compile(r"^\s*(?:[-*•]|\d+[.、)]|（?\d+）?)\s*")


def split_sentences(text: str) -> list[str]:
    out = []
    for part in _SENT_SPLIT.split(text or ""):
        s = _SENT_CLEAN.sub("", (part or "").strip())
        if len(s) >= 2:
            out.append(s)
    return out

# ==========================================================================
# 1. 意图解析（LLM）
# ==========================================================================
INTENT_SYSTEM = """你是医疗数据分析系统的意图解析器。
把医生的问题解析成结构化需求。只做解析，不做任何医学判断。

可用的指标（只能用这些标准名）：
{metrics}

输出 JSON：
{{"requirements": [{{"rid": "r1", "metric": "<标准指标名>",
    "window_days": <整数天数>, "aspect": "trend|latest|compare|distribution"}}]}}

规则：
- 医生没说的部分用默认值：window_days=365，aspect=trend
- 医生一次问了多个指标就输出多条需求
- 不要输出任何解释，只输出 JSON"""


class IntentAgent:
    kind = "llm"

    def __init__(self, backend):
        self.backend = backend

    def run(self, question: str, store: FactStore) -> list[dict]:
        metrics = "\n".join(
            f"- {mid}：{m['name']}（单位 {m['unit']}）"
            for mid, m in store.terms["metrics"].items()
        )
        system = INTENT_SYSTEM.format(metrics=metrics)
        data = self.backend.generate_json(system, question, max_tokens=768)
        reqs = data.get("requirements") or []
        out = []
        for i, r in enumerate(reqs):
            r.setdefault("rid", f"r{i+1}")
            out.append(r)
        return out


# ==========================================================================
# 2. 可满足性预检（代码）—— 只看元数据
# ==========================================================================
class PrecheckAgent:
    kind = "deterministic"

    def run(self, reqs: list[dict], store: FactStore) -> tuple[list[dict], list[dict]]:
        inv = store.inventory()
        passed, gaps = [], []
        for r in reqs:
            m = r["metric"]
            info = inv.get(m)
            if not info:
                # 完全没数据 —— 标记 unmet，不进分析，也不在覆盖性检查里重复报错
                gaps.append({"rid": r["rid"], "metric": m, "reason": "该指标没有任何数据"})
                passed.append(dict(r, unmet=True, flags=["no_data"]))
                continue

            # 医生没明确说时间段时，**用全量历史**，而不是拿一个默认窗口去截断。
            # 否则 4 年的数据会被 365 天截成 1 个点，报告退化成"仅 1 个数据点"。
            if not r.get("explicit_window"):
                if info["span_days"] > r["window_days"]:
                    r = dict(r, window_days=info["span_days"],
                             flags=list(r.get("flags", [])) + ["window_expanded_to_full_span"])
            need = r["window_days"]
            if info["span_days"] < need * 0.5:
                gaps.append({"rid": r["rid"], "metric": m,
                             "reason": f"数据仅覆盖 {info['span_days']} 天，"
                                       f"不足请求窗口 {need} 天的一半",
                             "covered_days": info["span_days"]})
                # 仍然放行，但降级窗口并记录 —— 不静默假装覆盖全窗口
                r = dict(r, window_days=info["span_days"],
                         flags=["window_shortened"])
            if info["n"] < 3:
                gaps.append({"rid": r["rid"], "metric": m,
                             "reason": f"数据点仅 {info['n']} 个", "n": info["n"]})
            passed.append(r)
        return passed, gaps


# ==========================================================================
# 3. 分析（LLM 规划 + 代码计算）
# ==========================================================================
PLAN_SYSTEM = """你是数据分析规划器。给定分析意图，决定要计算哪些统计量。

可用的统计量：latest, mean, slope, min, max, count

- trend：趋势，通常要 slope，再加 latest
- latest：当前状态，要 latest
- compare：与前值比较，要 delta 和 latest
- distribution：分布，要 min, max, mean

输出 JSON：{"statistics": ["latest", "slope"]}
只输出 JSON。"""

# 每种意图的保底统计量 —— LLM 规划失败或给空时用这个，不让流程断掉
DEFAULT_PLAN = {
    "trend": ["latest", "slope"],
    "latest": ["latest"],
    "compare": ["latest", "delta"],
    "distribution": ["min", "max", "mean"],
}


class AnalyzeAgent:
    kind = "llm+deterministic"

    def __init__(self, backend):
        self.backend = backend

    # ---- LLM 只负责"算什么"，绝不碰数字 ----
    def plan(self, req: dict) -> list[str]:
        try:
            data = self.backend.generate_json(
                PLAN_SYSTEM, f"意图：{req['aspect']}", max_tokens=128)
            stats = [s for s in (data.get("statistics") or [])
                     if s in schemas.STATISTICS]
        except Exception:
            stats = []
        # LLM 没给出可用规划时退回保底 —— 注意不能先塞 latest，
        # 否则 stats 永远非空，保底分支形同虚设
        if not stats:
            stats = list(DEFAULT_PLAN.get(req["aspect"], ["latest"]))
        if "latest" not in stats:
            stats.insert(0, "latest")
        return stats

    # ---- 数字全部由代码算出来 ----
    def compute(self, req: dict, store: FactStore) -> list[dict]:
        metric = req["metric"]
        meta = store.terms["metrics"][metric]
        unit = meta["unit"]

        pts = store.values(metric)
        if not pts:
            return []

        end = max(p[0] for p in pts)
        start = end - timedelta(days=req["window_days"])
        win = [p for p in pts if p[0] >= start]
        if not win:
            win = pts[-1:]

        n_all = len(pts)
        n = len(win)
        missing_rate = round(1 - n / n_all, 4) if n_all else 0.0
        sources = sorted({p[2] for p in win if p[2]})
        flags = list(req.get("flags", []))

        vals = [p[1] for p in win]
        times = [p[0] for p in win]
        claims = []
        idx = len(store.claims)

        def mk(stat, value, when=None, extra_flags=(), operands=()):
            nonlocal idx
            idx += 1
            cid = f"c{idx}"
            # 斜率的量纲是"单位/天"，不是原单位 —— 显示成 kg 会被读成"变化了 0 kg"
            u = f"{unit}/月" if stat == "slope" else unit
            # 斜率按「每天」算出来太小（% /天 是 1e-3 量级），慢病医生看的是
            # 每月/每年变化。这里直接存成「每月」，显示和重算都按这个口径。
            if stat == "slope" and value is not None:
                value = float(value) * 30.0
            return {
                "cid": cid, "rid": req["rid"], "metric": metric,
                "statistic": stat,
                "value": None if value is None else round(float(value), 6),
                "unit": u,
                "time": (when or end).isoformat(sep=" "),
                "window_start": start.isoformat(sep=" "),
                "window_end": end.isoformat(sep=" "),
                "n": n,
                "missing_rate": missing_rate,
                "display": schemas.fmt_display(value, u),
                "operands": list(operands),
                "source_refs": sources,
                "flags": flags + list(extra_flags),
            }

        plan = set(self.plan(req))
        by_stat = {}

        if "latest" in plan:
            c = mk("latest", vals[-1], times[-1])
            by_stat["latest"] = c
            claims.append(c)

        if "mean" in plan:
            claims.append(mk("mean", sum(vals) / n))

        if "min" in plan:
            claims.append(mk("min", min(vals), times[vals.index(min(vals))]))

        if "max" in plan:
            claims.append(mk("max", max(vals), times[vals.index(max(vals))]))

        if "count" in plan:
            claims.append(mk("count", n))

        # 趋势：数据点不足时明确说"算不出"，而不是硬给一个斜率
        if "slope" in plan:
            if n < 3:
                claims.append(mk("slope", None, extra_flags=["insufficient_data"]))
            else:
                t0 = times[0]
                xs = [(t - t0).days for t in times]
                mx, my = sum(xs) / n, sum(vals) / n
                den = sum((x - mx) ** 2 for x in xs)
                sl = 0.0 if den == 0 else sum((x - mx) * (v - my)
                                              for x, v in zip(xs, vals)) / den
                claims.append(mk("slope", sl))

        # 与前值比较 —— 差值是衍生命题，带 operands，所以可重算
        if "delta" in plan and len(win) >= 2:
            cur = claims[0]
            prev_pts = [p for p in pts if p[0] < times[-1]]
            if prev_pts:
                pv = prev_pts[-1][1]
                d = mk("delta", vals[-1] - pv, times[-1], operands=[cur["cid"]])
                d["prev_value"] = round(float(pv), 6)
                d["prev_time"] = prev_pts[-1][0].isoformat(sep=" ")
                claims.append(d)

        return claims


# ==========================================================================
# 4. 选图（规则）—— 需求 + 术语表 -> 图型，不碰数据值
# ==========================================================================
# 数据点太少时不画折线：连线会暗示趋势，而 3 个点支撑不了趋势
MIN_POINTS_FOR_LINE = 5

ASPECT_CHART = {
    "trend": "line",
    "latest": "scatter",
    "compare": "scatter",
    "distribution": "box",
}

# 多序列配色（色盲友好，且打印成灰度也能区分）
SERIES_COLORS = ["#2b6cb0", "#c05621", "#2f855a", "#6b46c1", "#b7791f"]


def group_of(terms: dict, metric: str):
    """这个指标属于哪个可合并组。返回 (组id, 组定义) 或 (None, None)。"""
    for gid, g in (terms.get("chart_groups") or {}).items():
        if gid.startswith("_"):
            continue
        if metric in g.get("members", []):
            return gid, g
    return None, None


class ChartSelectAgent:
    kind = "deterministic"

    def pick(self, req: dict, store: FactStore) -> dict | None:
        metric = req["metric"]
        pts = store.values(metric)
        if not pts:
            return None

        meta = store.terms["metrics"][metric]
        n = len(pts)
        chart_type = ASPECT_CHART.get(req["aspect"], "line")
        flags = []

        # 准入检查：数据支不支持这个图型
        if chart_type == "line" and n < MIN_POINTS_FOR_LINE:
            chart_type = "scatter"
            flags.append(f"n={n}<{MIN_POINTS_FOR_LINE}_改用散点")
        temporal = meta.get("temporal", "instant")
        if temporal == "window_mean":
            flags.append("窗口平均值_相邻点时间窗重叠")
        if req.get("window_days", 0) > 0:
            flags.append(f"window={req['window_days']}d")

        gid, grp = group_of(store.terms, metric)
        return {
            "sid": f"s_{req['rid']}",
            "rid": req["rid"],
            "metric": metric,
            "chart_type": chart_type,
            "window_days": req["window_days"],
            "unit": meta["unit"],
            "claims": [],
            "series": [],
            "flags": flags,
            "group": gid,
            "group_name": grp["name"] if grp else None,
            "series_defs": [{"metric": metric, "name": meta["name"], "claims": []}],
        }


def merge_by_group(specs: list[dict], store: FactStore) -> list[dict]:
    """把同一组的图表规格合并成一张多序列图。

    收缩压/舒张压本来就是同一次测量的两个值，单位也一样 —— 分成两张图
    不仅浪费版面，还让医生没法直接看脉压差。血脂四项同理。
    """
    out: list[dict] = []
    buckets: dict[str, list[dict]] = {}
    for s in specs:
        g = s.get("group")
        if g:
            buckets.setdefault(g, []).append(s)
        else:
            out.append(s)

    for gid, group in buckets.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        base = dict(group[0])
        base["sid"] = f"g_{gid}"
        base["series_defs"] = [sd for s in group for sd in s["series_defs"]]
        base["window_days"] = max(s["window_days"] for s in group)
        base["unit"] = group[0]["unit"]
        base["rid"] = [s["rid"] for s in group]
        # 只要有一条能用折线，整张图就用折线（其余序列各自按点画）
        base["chart_type"] = ("line" if any(s["chart_type"] == "line" for s in group)
                              else group[0]["chart_type"])
        base["flags"] = sorted({f for s in group for f in s["flags"]})
        base["metrics"] = [sd["metric"] for sd in base["series_defs"]]
        out.append(base)
    return out


# ==========================================================================
# 5. 画图（代码）—— 纯映射，不做平滑/插值/聚合
# ==========================================================================
class RenderAgent:
    kind = "deterministic"
    W, H = 760, 320
    PAD_L, PAD_R, PAD_T, PAD_B = 74, 26, 46, 62

    # ---- 刻度：取"好看"的整数步长 ----
    @staticmethod
    def _nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
        if not (hi > lo):
            hi = lo + 1
        raw = (hi - lo) / max(target, 2)
        mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
        step = mag * 10
        for m in (1, 2, 2.5, 5, 10):
            if m * mag >= raw:
                step = m * mag
                break
        start = math.floor(lo / step) * step
        out, v = [], start
        while v <= hi + step * 0.5 and len(out) < 12:
            out.append(round(v, 10))
            v += step
        return out

    @staticmethod
    def _trim(v: float) -> str:
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        if abs(v - round(v)) < 1e-9:
            return f"{round(v):d}"
        return f"{v:.2f}".rstrip("0").rstrip(".")

    def render(self, spec: dict, store: FactStore, claims: list[dict]) -> str:
        defs = spec.get("series_defs") or [{"metric": spec["metric"],
                                            "name": store.terms["metrics"][spec["metric"]]["name"]}]
        # 所有序列共用一条时间轴：从最早的时间点往前推 window_days
        all_vals = [p for sd in defs for p in store.values(sd["metric"])]
        if not all_vals:
            return "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>"
        end = max(p[0] for p in all_vals)
        start = end - timedelta(days=spec["window_days"])

        series, flat = [], []
        for i, sd in enumerate(defs):
            metric = sd["metric"]
            meta = store.terms["metrics"][metric]
            pts = [p for p in store.values(metric) if p[0] >= start]
            if not pts:
                pts = store.values(metric)[-1:]
            series.append({
                "metric": metric, "name": sd.get("name") or meta["name"],
                "unit": meta["unit"], "pts": pts,
                "color": SERIES_COLORS[i % len(SERIES_COLORS)],
            })
            flat += [(p[0].isoformat(sep=" "), p[1], p[2]) for p in pts]

        spec["series"] = flat
        spec["claims"] = [c["cid"] for c in claims
                          if c["metric"] in {sd["metric"] for sd in defs}]

        xmax = max(((p[0] - start).days for p in flat and
                    [x for sd in series for x in sd["pts"]]), default=1) or 1
        ys_all = [v for sd in series for v in (p[1] for p in sd["pts"])]

        yticks = self._nice_ticks(min(ys_all), max(ys_all), 5)
        ylo, yhi = yticks[0], yticks[-1]

        X0, X1 = self.PAD_L, self.W - self.PAD_R
        Y0, Y1 = self.H - self.PAD_B, self.PAD_T

        def px(x): return X0 + (x / xmax) * (X1 - X0)

        def py(y):
            if yhi == ylo:
                return (Y0 + Y1) / 2
            return Y0 - ((y - ylo) / (yhi - ylo)) * (Y0 - Y1)

        unit = spec.get("unit") or series[0]["unit"]
        # 只有真的多条序列时才用组名（如「血压」）；
        # 单序列图必须用自己的指标名，否则拆开看时会全叫「血压」分不清是哪条
        if len(series) > 1 and spec.get("group_name"):
            title = spec["group_name"]
        else:
            title = "、".join(s["name"] for s in series)

        P: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.W}" '
            f'height="{self.H}" viewBox="0 0 {self.W} {self.H}" '
            f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">',
            f'<rect width="{self.W}" height="{self.H}" fill="#fff"/>',
            f'<text x="{X0}" y="20" font-size="14" font-weight="600" fill="#111">'
            f'{title}</text>',
        ]
        # 图例：多序列时必须标出哪条线是谁
        lx = X0 + 26 + len(title) * 14
        for s in series:
            P.append(f'<circle cx="{lx}" cy="15" r="4" fill="{s["color"]}"/>')
            P.append(f'<text x="{lx+9}" y="19" font-size="11" fill="#444">'
                     f'{s["name"]}</text>')
            lx += 20 + len(s["name"]) * 12

        sub = f'{start.date()} ~ {end.date()}   n={len(flat)}'
        P.append(f'<text x="{X1}" y="20" font-size="11" fill="#888" '
                 f'text-anchor="end">{sub}</text>')

        # Y 轴网格 + 刻度
        for t in yticks:
            y = py(t)
            P.append(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" '
                     f'stroke="#eceff3" stroke-width="1"/>')
            P.append(f'<line x1="{X0-5}" y1="{y:.1f}" x2="{X0}" y2="{y:.1f}" '
                     f'stroke="#9aa4b2" stroke-width="1"/>')
            P.append(f'<text x="{X0-9}" y="{y+4:.1f}" font-size="11" fill="#555" '
                     f'text-anchor="end">{self._trim(t)}</text>')

        ylab = f'{"、".join(s["name"] for s in series)}（{unit}）'
        P.append(f'<text x="16" y="{(Y0+Y1)/2:.1f}" font-size="11.5" fill="#333" '
                 f'text-anchor="middle" '
                 f'transform="rotate(-90 16 {(Y0+Y1)/2:.1f})">{ylab}</text>')

        # X 轴刻度：按跨度选密度与格式
        nticks = 5 if xmax > 400 else max(3, min(6, len(flat)))
        fmt = "%Y" if xmax > 1100 else ("%Y-%m" if xmax > 120 else "%m-%d")
        seen = set()
        for i in range(nticks):
            xv = round(xmax * i / max(nticks - 1, 1))
            lab = (start + timedelta(days=xv)).date().strftime(fmt)
            if lab in seen:
                continue
            seen.add(lab)
            x = px(xv)
            P.append(f'<line x1="{x:.1f}" y1="{Y0}" x2="{x:.1f}" y2="{Y0+5}" '
                     f'stroke="#9aa4b2" stroke-width="1"/>')
            P.append(f'<text x="{x:.1f}" y="{Y0+20}" font-size="11" fill="#555" '
                     f'text-anchor="middle">{lab}</text>')

        P.append(f'<line x1="{X0}" y1="{Y0}" x2="{X1}" y2="{Y0}" '
                 f'stroke="#5b6472" stroke-width="1.3"/>')
        P.append(f'<line x1="{X0}" y1="{Y0}" x2="{X0}" y2="{Y1}" '
                 f'stroke="#5b6472" stroke-width="1.3"/>')
        P.append(f'<text x="{(X0+X1)/2:.1f}" y="{self.H-14}" font-size="11.5" '
                 f'fill="#333" text-anchor="middle">日期</text>')

        # 数据：每个序列各画各的
        for s in series:
            xs = [(p[0] - start).days for p in s["pts"]]
            ys = [p[1] for p in s["pts"]]
            if spec["chart_type"] == "line" and len(s["pts"]) >= 2:
                d = " ".join(f'{"M" if i == 0 else "L"}{px(x):.1f},{py(y):.1f}'
                             for i, (x, y) in enumerate(zip(xs, ys)))
                P.append(f'<path d="{d}" fill="none" stroke="{s["color"]}" '
                         f'stroke-width="1.8" stroke-linejoin="round"/>')
            for x, y, p in zip(xs, ys, s["pts"]):
                flagged = any(k in p[3] for k in
                              ("lt", "gt", "converted_from", "unit_"))
                P.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="3.2" '
                         f'fill="{"#c53030" if flagged else s["color"]}"/>')

        short = [s["name"] for s in series if len(s["pts"]) < 5]
        if short:
            P.append(f'<text x="{X1}" y="{Y1+4}" font-size="10.5" fill="#c53030" '
                     f'text-anchor="end">数据点不足：{"、".join(short)}</text>')

        P.append("</svg>")
        return "".join(P)


# ==========================================================================
# 6. 报告（LLM + 占位符替换）
# ==========================================================================
REPORT_SYSTEM = """你是医疗数据报告生成器。把结构化命题组织成给医生看的自然语言。

**最重要的规则**：数字只能写成占位符，绝对不要写字面的数字。
  ✅ 正确：糖化血红蛋白最近一次为 {c3}。
  ❌ 错误：糖化血红蛋白最近一次为 8.2 %。
  ✅ 正确：体重呈下降趋势（{c6}）。
  ❌ 错误：体重呈下降趋势（-0.025 kg/天）。

**禁止**：任何临床评价词（控制不佳、不达标、异常、提示、建议、恶化、好转）。
**禁止**：诊断、治疗建议、因果推断。
**禁止**：用 null 或"无"表示缺失 —— 值缺失的命题不要写进报告。

输入是命题列表，每条有 cid 和 statistic。输出要求：
- 一行一句，不要编号、不要标题、不要解释
- 每句至少包含一个 {cid} 占位符
- statistic=slope 且值为 null 的命题，写成"仅 {cid.n} 个数据点，未计算趋势"
- 只输出句子本身

示例输入：{"claims":[{"cid":"c1","label":"体重","statistic":"latest","value":80.0},
                     {"cid":"c2","label":"体重","statistic":"slope","value":0.05}]}
示例输出：
体重最近一次为 {c1}。
体重窗口内呈上升趋势（{c2}）。"""

EXAMPLES = {
    "latest": "{label}最近一次为 {{{cid}}}。",
    "mean": "{label}窗口内均值为 {{{cid}}}。",
    "min": "{label}窗口内最低值为 {{{cid}}}。",
    "max": "{label}窗口内最高值为 {{{cid}}}。",
    "delta": "{label}较上一次的变化为 {{{cid}}}。",
}


class ReportAgent:
    kind = "llm"

    def __init__(self, backend):
        self.backend = backend
        self.fallback_used = False

    # ---- 确定性兜底模板 ----
    # 报告层是"模板 + 填空"，不是自由生成。LLM 没按规矩写占位符时，
    # 退回模板比放行一段无依据的文字安全得多。
    def template(self, store: FactStore) -> list[str]:
        lines = []
        for c in store.claims.values():
            label = store.terms["metrics"][c["metric"]]["name"]
            if c["statistic"] == "slope":
                if c["value"] is None:
                    lines.append(f"{label}仅 {{{c['cid']}.n}} 个数据点，未计算趋势。")
                elif abs(c["value"]) < 1e-9:
                    lines.append(f"{label}窗口内近似持平（{{{c['cid']}}}）。")
                else:
                    d = "上升" if c["value"] > 0 else "下降"
                    lines.append(f"{label}窗口内呈{d}趋势（{{{c['cid']}}}）。")
            elif c["value"] is None:
                continue
            else:
                tpl = EXAMPLES.get(c["statistic"], "{label}的{{{cid}}}。")
                lines.append(tpl.format(label=label, cid=c["cid"]))
        return lines

    def run(self, store: FactStore) -> list[dict]:
        payload = [{"cid": c["cid"],
                    "label": store.terms["metrics"][c["metric"]]["name"],
                    "statistic": c["statistic"],
                    "unit": c["unit"],
                    "value": c["value"]}
                   for c in store.claims.values()
                   if c["value"] is not None or c["statistic"] == "slope"]

        lines: list[str] = []
        try:
            raw = self.backend.generate(
                REPORT_SYSTEM, json.dumps({"claims": payload}, ensure_ascii=False),
                max_tokens=900)
            lines = split_sentences(raw)
        except Exception:
            lines = []

        # 每句都必须带占位符 —— 否则这句子没有依据
        good = bool(lines) and all(schemas.placeholders(l) for l in lines)
        if not good:
            self.fallback_used = True
            n_bad = sum(1 for l in lines if not schemas.placeholders(l))
            lines = self.template(store)

        return [{"raw": l, "claim_ids": schemas.placeholders(l)} for l in lines]

    # ---- 占位符替换：由代码做，LLM 从未见过任何一个数字 ----
    def resolve(self, sentences: list[dict], store: FactStore) -> list[dict]:
        """支持两种占位符：
              {c3}        -> 显示值（已格式化，含单位）
              {c3.n}      -> 命题字段（n / value / time / missing_rate）

        字段占位符让"数据不足"这类句子也能挂在命题上，而不是变成无依据的空话。
        """
        out = []
        for s in sentences:
            text, unresolved = s["raw"], []
            for m in schemas.PLACEHOLDER_RE.finditer(s["raw"]):
                cid, field = m.group(1), m.group(2)
                c = store.claims.get(cid)
                if not c:
                    unresolved.append(cid)
                    continue
                if field is None:
                    repl = c["display"]
                elif field == "missing_rate":
                    repl = f"{c['missing_rate']:.0%}"
                elif field in c and c[field] is not None:
                    repl = f"{c[field]:g}" if isinstance(c[field], (int, float)) else str(c[field])
                else:
                    unresolved.append(m.group(0))
                    continue
                text = text.replace(m.group(0), repl)
            out.append({"resolved": text, "raw": s["raw"],
                        "claim_ids": s["claim_ids"], "unresolved": unresolved})
        return out
