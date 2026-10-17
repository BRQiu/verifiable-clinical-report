# -*- coding: utf-8 -*-
"""产物 schema —— 系统里传递的不是自然语言，是这四种结构化产物。

设计约定（前面讨论定下来的）：
  · 数值与名词分离：value 是裸数值，metric 只是引用（单位查术语表）
  · 每个产物都带引用，引用完整性是零成本的校验
  · 衍生值必须带算式（operands），否则没法重算
  · 缺失严格为 None，绝不为 0

四种产物：
    Requirement  需求 R     ← 意图解析产出
    Claim        命题       ← 分析产出（所有计算的结果）
    ChartSpec    图表规格   ← 选图产出
    Sentence     报告句     ← 报告产出（数字以占位符引用）
"""
from __future__ import annotations

import re
from datetime import datetime

# --------------------------------------------------------------------------
# 需求
# --------------------------------------------------------------------------
ASPECTS = ("trend", "latest", "compare", "distribution")

REQUIREMENT_FIELDS = {
    "rid": str,          # 需求 id，如 r1
    "metric": str,       # 标准指标名，必须在术语表里
    "window_days": int,  # 观察窗口天数
    "aspect": str,       # trend | latest | compare | distribution
}


def validate_requirement(r: dict, terms: dict) -> list[str]:
    p = []
    for f, t in REQUIREMENT_FIELDS.items():
        if f not in r:
            p.append(f"缺字段 {f}")
        elif not isinstance(r[f], t):
            p.append(f"{f} 类型应为 {t.__name__}")
    if r.get("metric") and r["metric"] not in terms["metrics"]:
        p.append(f"指标 {r['metric']} 不在术语表里")
    if r.get("aspect") and r["aspect"] not in ASPECTS:
        p.append(f"aspect {r['aspect']} 非法，应为 {ASPECTS}")
    if isinstance(r.get("window_days"), int) and not (1 <= r["window_days"] <= 3650):
        p.append("window_days 超出 1~3650 天")
    return p


# --------------------------------------------------------------------------
# 命题
# --------------------------------------------------------------------------
STATISTICS = ("latest", "mean", "slope", "delta", "min", "max", "count")

CLAIM_FIELDS = {
    "cid": str,
    "rid": str,
    "metric": str,
    "statistic": str,
    "value": (int, float, type(None)),
    "unit": str,
    "time": (str, type(None)),
    "window_start": (str, type(None)),
    "window_end": (str, type(None)),
    "n": int,
    "missing_rate": (int, float),
    "display": str,
    "operands": list,     # 衍生命题：依赖的 cid 列表
    "source_refs": list,  # 用到的数据来源
    "flags": list,
}


def validate_claim(c: dict, terms: dict) -> list[str]:
    p = []
    for f, t in CLAIM_FIELDS.items():
        if f not in c:
            p.append(f"缺字段 {f}")
        elif not isinstance(c[f], t):
            p.append(f"{f} 类型应为 {t}")
    if c.get("metric") and c["metric"] not in terms["metrics"]:
        p.append(f"指标 {c['metric']} 不在术语表里")
    if c.get("statistic") and c["statistic"] not in STATISTICS:
        p.append(f"statistic {c['statistic']} 非法")
    # 单位必须和术语表一致 —— 统一单位之后不该再有别的单位
    # （斜率例外：它是"单位/天"，量纲本来就不一样）
    if c.get("metric") in terms["metrics"]:
        want = terms["metrics"][c["metric"]]["unit"]
        got = c.get("unit")
        if c.get("statistic") == "slope":
            if got != f"{want}/月":
                p.append(f"斜率单位应为 {want}/月，实际 {got}")
        elif got != want:
            p.append(f"单位 {got} 与术语表 {want} 不一致")
    # 有值就必须有时间和样本量
    if c.get("value") is not None:
        if not c.get("time") and c.get("statistic") != "slope":
            p.append("有值但缺 time")
        if c.get("n", 0) <= 0:
            p.append("有值但 n<=0")
    # 衍生值必须带算式
    if c.get("statistic") == "delta" and not c.get("operands"):
        p.append("delta 命题缺 operands（无法重算）")
    return p


def fmt_display(value, unit: str, digits: int = 1) -> str:
    """显示值在分析层一次算好 —— 报告层不做四舍五入。

    绝不用科学计数法：医生读不了 2.45e-34。小到没有意义就显示 0。
    """
    if value is None:
        return "—"
    a = abs(value)
    if a < 0.005:            # 比最小可显示单位还小 → 就是 0，别硬凑有效位
        s = "0"
    elif a >= 100:
        s = f"{value:.0f}"
    elif a >= 1:
        s = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    elif a >= 0.01:
        s = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        s = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{s} {unit}".strip()


# --------------------------------------------------------------------------
# 图表规格
# --------------------------------------------------------------------------
CHART_TYPES = ("line", "scatter", "bar", "box")


def validate_chart(s: dict, terms: dict, claim_ids: set[str]) -> list[str]:
    p = []
    if s.get("chart_type") not in CHART_TYPES:
        p.append(f"图型 {s.get('chart_type')} 非法")

    # 单指标图有 metric；合并图（如血压=收缩压+舒张压）走 series_defs
    defs = s.get("series_defs") or []
    metrics = [sd.get("metric") for sd in defs] or \
              ([s["metric"]] if s.get("metric") else [])
    if not metrics:
        p.append("图表没有任何指标")
    for m in metrics:
        if m not in terms["metrics"]:
            p.append(f"指标 {m} 不在术语表里")

    # 合并图的所有成员必须单位一致，否则共用一根纵轴就是错的
    units = {terms["metrics"][m]["unit"] for m in metrics if m in terms["metrics"]}
    if len(units) > 1:
        p.append(f"合并图的指标单位不一致 {units}，不能共用纵轴")
    if s.get("unit") and units and s["unit"] not in units:
        p.append(f"声明单位 {s['unit']} 与成员单位 {units} 不符")

    for cid in s.get("claims", []):
        if cid not in claim_ids:
            p.append(f"图表引用了不存在的命题 {cid}")
    for pt in s.get("series", []):
        if len(pt) < 3:
            p.append("数据点缺少来源标注")
    return p


# --------------------------------------------------------------------------
# 报告句
# --------------------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)(?:\.([a-zA-Z_]+))?\}")

# 越界词表：出现即视为临床评价，系统不该说这些
BANNED_WORDS = [
    "控制不佳", "控制良好", "控制尚可", "控制差", "不达标", "达标",
    "异常", "偏高", "偏低", "过高", "过低", "显著异常",
    "提示", "建议", "考虑", "推荐", "需要调整", "应调整",
    "恶化", "好转", "改善", "加重", "缓解",
    "危险", "严重", "不良", "风险高",
]


def find_banned(text: str) -> list[str]:
    return [w for w in BANNED_WORDS if w in (text or "")]


def placeholders(text: str) -> list[str]:
    """提取占位符里的 cid 列表"""
    return [m.group(1) for m in PLACEHOLDER_RE.finditer(text or "")]


def numbers_in(text: str) -> list[float]:
    """报告句里的裸数字（用来和命题比对）。忽略占位符内部。"""
    stripped = PLACEHOLDER_RE.sub(" ", text or "")
    return [float(x) for x in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)", stripped)]


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
def dt(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None
