# -*- coding: utf-8 -*-
"""Synthetic Hospital 适配器 —— 把它的自由文本检验值抽成标准长表。

为什么需要适配器层：清洗逻辑不该知道数据源的格式。这个模块负责
"Synthetic Hospital → 标准长表"，之后完全复用 ingest/store 那一套。

数据集特点（决定了抽取必须做成什么样）：
  · 数值**不在结构化表里**，埋在 encounter_ehr_sections.section_text 的
    labs / vitals 段落中
  · 同一段落里两种格式混着：
        "- Hemoglobin: 12.5 g/dL"              ← 结构化列表
        "her blood pressure is 112/76 mm Hg"   ← 自然语言
  · 数值形态很脏：千分位 "7,500/mm^3"、比较符 "<0.04 ng/mL"、分数 "112/76"

抽取策略：**规则优先，不用 LLM**。
  · 指标靠术语表别名匹配 —— 可追溯、可复现
  · 数值靠正则 —— 零成本
  · 抽到的每个值都立刻过契约校验（单位合法吗？值域合理吗？）
这符合系统约定：能用确定性手段做的，绝不交给 LLM。
"""
from __future__ import annotations

import collections
import json
import os
import re
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
import ingest  # noqa: E402

# 数据目录：默认 <repo>/data/synthetic_hospital，可用环境变量覆盖
SH_ROOT = os.environ.get(
    "SYNTHETIC_HOSPITAL_ROOT", os.path.join(_ROOT, "data", "synthetic_hospital"))
SH_DB = os.path.join(SH_ROOT, "benchmark_v1.3.db")

# 血压是"收缩/舒张"两个指标挤在一个值里，特殊处理
BP_RE = re.compile(
    r"(?:blood\s+pressure|bp)\D{0,20}?(\d{2,3})\s*/\s*(\d{2,3})\s*(?:mm\s*hg|mmHg)?", re.I)

NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"
VAL_RE = re.compile(rf"^([<>≤≥]?=?)\s*({NUM})")

# 段落文本里「指标 ... 数值 单位」的通用模式。
# 单位部分：首段不含空白；后面只允许跟**白名单内的单位词**。
# 之前允许任意字母 token，结果 "% on room" 被整个当成单位吞掉。
UNIT_TAIL_WORDS = ("Hg", "per", "minute", "min", "hour", "day", "breaths", "beats")
_TAIL = "|".join(UNIT_TAIL_WORDS)
_SINGLE = r"[A-Za-z%/\u00b5\u03bc\u00b2\u00b3^0-9\.\-]"

PAIR_RE = re.compile(
    rf"([A-Za-z][A-Za-z0-9\u00b5\u03bc ,\-/()%^]{{2,48}}?)"
    rf"\s*(?:is|are|of|:|=)\s*"
    rf"([<>≤≥]?=?\s*{NUM})\s*"
    rf"({_SINGLE}{{0,14}}(?:[ \t](?:{_TAIL})\b){{0,2}})?")

# 单位写法归一：同一个单位在文本里有好几种写法
UNIT_ALIASES = {
    "mm hg": "mmHg", "mmhg": "mmHg", "mmhg.": "mmHg",
    "kg/m^2": "kg/m²", "kg/m2": "kg/m²", "kg/m²": "kg/m²",
    "beats per minute": "bpm", "beats/min": "bpm", "/min": "bpm",
    "breaths per minute": "breaths/min", "breaths/min.": "breaths/min",
    # 注意：不要在这里把 '/µL' 归一到 '10^3/uL' —— 那中间差 1000 倍，
    # 必须走 terms.json 的 unit_conversions 带因子做，
    # 否则 12,000/µL 会被当成 12000×10³ 而看起来完全合理
    "meq/l": "mEq/L", "g/dl": "g/dL", "mg/dl": "mg/dL", "pg/ml": "pg/mL",
    "ml/min/1.73m^2": "mL/min/1.73m²",
    "% on room air": "%", "% on room": "%",
}

# 少数指标的单位写法会撞车（'/min' 对心率是 bpm，对呼吸频率是 breaths/min）
METRIC_UNIT_OVERRIDE = {
    "resp_rate": {"bpm": "breaths/min", "/min": "breaths/min"},
    "heart_rate": {"breaths/min": "bpm"},
}


def norm_unit(u: str | None, mid: str | None = None) -> str | None:
    if not u:
        return None
    s = UNIT_ALIASES.get(u.strip().rstrip(".,;:").lower(), u.strip().rstrip(".,;:"))
    if mid in METRIC_UNIT_OVERRIDE:
        s = METRIC_UNIT_OVERRIDE[mid].get(s, s)
    return s


def _norm(s: str) -> str:
    return re.sub(r"[\s()（）:：\[\]【】_\-]", "", str(s or "")).lower()


def alias_index(terms: dict) -> dict[str, str]:
    idx = {}
    for mid, meta in terms["metrics"].items():
        for a in [meta["name"]] + meta.get("aliases", []):
            idx[_norm(a)] = mid
    return idx


def best_metric(alias: dict[str, str], key: str) -> str | None:
    """挑最合适的指标。

    不能简单用"包含"匹配 —— 会撞车：
        'hba1c'         含 'hb'  -> 会被误判成 hemoglobin
        'hdlcholesterol' 含 'cholesterol' -> 会被误判成 tc

    规则：在所有命中的别名里，**取起始位置最靠前的**；同位置取更长的。
    'hdlcholesterol': 'hdl'@0 胜 'cholesterol'@3  -> ldl 类正确
    'hba1c':          'hb'@0 vs 'hba1c'@0 同位置，取长的 -> hba1c 正确
    'totalcholesterol': 只有 'cholesterol'@5 命中 -> tc 正确
    """
    if key in alias:
        return alias[key]
    best = None
    for a, mid in alias.items():
        if len(a) < 3:
            continue
        pos = key.find(a)
        if pos < 0:
            continue
        score = (pos, -len(a))       # 起始越靠前越好；同位置越长越好
        if best is None or score < best[0]:
            best = (score, mid)
    return best[1] if best else None


def _unit_ok(terms: dict, mid: str, unit: str | None) -> bool:
    """单位是否可接受：与术语表一致，或能通过换算表换过去。"""
    if not unit:
        return True
    want = terms["metrics"][mid]["unit"]
    if _norm(unit) == _norm(want):
        return True
    for conv in terms.get("unit_conversions", {}).values():
        if isinstance(conv, dict) and mid in conv.get("metric_group", []) \
                and _norm(conv["from"]) == _norm(unit):
            return True
    return False


class SyntheticHospital:
    """把 Synthetic Hospital 的一个患者转成标准长表记录。"""

    def __init__(self, db_path: str = SH_DB, terms: dict | None = None):
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"找不到 {db_path}。先下载：\n"
                f"  https://github.com/sparkcpark/synthetic_hospital")
        self.db = db_path
        self.terms = terms or json.load(
            open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "terms.json"),
                encoding="utf-8"))
        self.alias = alias_index(self.terms)
        # 因单位不明而丢弃的计数 —— 必须能被看到，不能静默丢数据
        self.dropped_units: collections.Counter = collections.Counter()

    # ---------------- 患者列表 ----------------
    def patients(self, limit: int | None = None) -> list[dict]:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        sql = """SELECT p.patient_id, p.age, p.sex, p.num_encounters,
                        p.primary_diagnoses, p.comorbidities,
                        (SELECT COUNT(*) FROM longitudinal_encounters e
                         JOIN encounter_ehr_sections s ON s.encounter_id=e.encounter_id
                         WHERE e.patient_id=p.patient_id
                           AND s.section_type IN ('labs','vitals')) AS n_lab_sections
                 FROM longitudinal_patients p
                 ORDER BY n_lab_sections DESC, p.patient_id"""
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for r in con.execute(sql):
            d = dict(r)
            try:
                d["dx"] = json.loads(d.get("primary_diagnoses") or "[]")
            except Exception:
                d["dx"] = []
            out.append(d)
        con.close()
        return out

    # ---------------- 抽取 ----------------
    def extract_text(self, text: str, when, source: str) -> list[dict]:
        """从一段 labs/vitals 文本里抽出 (指标, 值, 单位)。规则驱动，不用 LLM。"""
        recs: list[dict] = []
        text = text or ""

        # ① 血压：'112/76 mm Hg' 要拆成收缩压 + 舒张压
        if re.search(r"blood\s+pressure|\bbp\b", text, re.I):
            m = BP_RE.search(text)
            if m:
                for mid, v in (("sbp", m.group(1)), ("dbp", m.group(2))):
                    recs.append(self._rec(mid, v, "mmHg", when, source, flags=[]))

        # ② 其它「指标 ... 数值 单位」对
        for m in PAIR_RE.finditer(text):
            name, val_s, unit = m.group(1), m.group(2), m.group(3)
            mid = best_metric(self.alias, _norm(name))
            if mid is None:
                continue
            mm = VAL_RE.match(val_s.strip())
            if not mm:
                continue
            flags = []
            if mm.group(1):
                flags.append({"<": "lt", "≤": "le", ">": "gt", "≥": "ge"}.get(
                    mm.group(1)[0], "cmp"))
            value = float(mm.group(2).replace(",", ""))
            unit = norm_unit(unit, mid)
            rec = self._rec(mid, value, unit, when, source, flags)
            if rec is not None:
                recs.append(rec)

        return recs

    def _rec(self, mid, value, unit, when, source, flags):
        """单位未知就**丢弃并计数**，绝不贴个内部单位蒙混过去。

        一个单位不明的数值混进时间线，会安静地算出一条错的趋势 ——
        这正是系统要防的失败模式，所以宁可不收。
        """
        v, iunit, uflags = ingest._convert_unit(float(value), unit, mid, self.terms)
        if any(f.startswith("unit_unknown") for f in uflags):
            self.dropped_units[f"{mid}|{unit}"] += 1
            return None
        return {"metric": mid, "time": when, "value": v,
                "unit_internal": iunit, "source": source,
                "flags": flags + uflags, "raw_metric": mid}

    # ---------------- 主入口 ----------------
    def load(self, patient_id: int) -> list[dict]:
        """返回该患者的标准长表记录（可直接喂给 FactStore.load_records）。"""
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        recs: list[dict] = []
        n_sec = 0
        for r in con.execute(
                """SELECT e.encounter_date, e.encounter_type, e.department,
                          s.section_type, s.section_text, e.encounter_id
                   FROM longitudinal_encounters e
                   JOIN encounter_ehr_sections s ON s.encounter_id = e.encounter_id
                   WHERE e.patient_id = ?
                     AND s.section_type IN ('labs','vitals')
                   ORDER BY e.encounter_order, s.section_order""", (patient_id,)):
            n_sec += 1
            when = ingest.parse_time(r["encounter_date"])
            if when is None:
                continue
            src = f"SH#{r['encounter_id']}/{r['section_type']}"
            recs += self.extract_text(r["section_text"], when, src)
        con.close()

        # 同一次就诊同一指标出现多次（labs 和 vitals 各说一遍血压）→ 去重
        seen, out = set(), []
        for x in recs:
            key = (x["metric"], x["time"], x["value"])
            if key in seen:
                continue
            seen.add(key)
            out.append(x)
        out.sort(key=lambda x: (x["time"], x["metric"]))
        return out

    def meta(self, patient_id: int) -> dict:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        r = con.execute("""SELECT * FROM longitudinal_patients WHERE patient_id=?""",
                        (patient_id,)).fetchone()
        con.close()
        return dict(r) if r else {}


if __name__ == "__main__":
    sh = SyntheticHospital()
    ps = sh.patients(limit=5)
    print(f"患者总数（按 labs 段落数排序，取前 5）：")
    for p in ps:
        print(f"  #{p['patient_id']}  {p['sex']} {p['age']}岁  "
              f"{p['num_encounters']} 次就诊 / {p['n_lab_sections']} 段检验  "
              f"{p['dx'][:2]}")
    pid = ps[0]["patient_id"]
    recs = sh.load(pid)
    print(f"\n患者 {pid} 抽出 {len(recs)} 条记录：")
    for r in recs[:25]:
        print(f"  {r['time']:%Y-%m-%d}  {r['metric']:<16} {r['value']:>10} "
              f"{r['unit_internal']:<10} {r['source']:<28} {r['flags']}")
