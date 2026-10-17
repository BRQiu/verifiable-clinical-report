# -*- coding: utf-8 -*-
"""
ingest.py — Excel / CSV 识别与转换（纯标准库，零依赖）

只做两件事：
  1. 认出一个杂乱表格的结构：表头在哪、每一列是什么、上面有没有报告元信息
  2. 转成标准长表：metric | time | value | unit_internal | source | flags

遵守系统约定：
  · 数值与名词分离 —— value 存裸数字，metric 只存标准名，单位另外给
  · 缺失严格为 None —— 绝不当 0（血糖缺失被当 0 会造出不存在的低血糖事件）
  · 单位统一留痕 —— 原值留在 raw，换算因子记在 flags
  · 不猜 —— 识别结果用 --profile 打出来给人看

用法：
    python ingest.py 报告.xlsx --profile
    python ingest.py 报告.csv 报告2.xlsx -o long.csv
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

MISSING_TOKENS = {"", "-", "--", "—", "/", "n/a", "na", "null", "none", "nan",
                  "未测", "未查", "未做", "无", "缺"}

# 列角色关键词。顺序有意义：先匹配到的优先。
COLUMN_HINTS = [
    ("time",     ["采样时间", "采样日期", "检验时间", "检测时间", "报告时间", "送检时间",
                  "测量时间", "测定时间", "记录时间", "日期", "时间", "date", "time", "datetime"]),
    ("metric",   ["项目名称", "检验项目", "项目", "指标", "检查项目", "测定项目",
                  "item", "test", "analyte", "metric"]),
    ("value",    ["结果", "数值", "测定值", "检测值", "测量值", "值", "result", "value"]),
    ("unit",     ["单位", "unit", "units"]),
    ("ignore",   ["参考范围", "参考区间", "参考值", "正常范围", "提示", "备注", "方法",
                  "reference", "ref", "flag", "note"]),
    ("meta",     ["姓名", "性别", "年龄", "出生", "门诊号", "住院号", "病案号", "患者",
                  "医院", "科室", "医生", "医师", "床号", "id", "name", "sex", "age"]),
]

TIME_RE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
NUM_RE = re.compile(r"^([<>≤≥]=?)?\s*([-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(.*)$")
# 定性结果：整词匹配，不能用字符类（否则「阴性」两个字符匹配不上）
QUALITATIVE_RE = re.compile(r"^(阴性|阳性|弱阳性|正常|异常|未见异常|未见明显异常|"
                            r"阴性\(-\)|阳性\(\+\)|\++|-+|\+/-)$")
FULLWIDTH = str.maketrans("０１２３４５６７８９．－", "0123456789.-")

# Excel 内置日期格式 id
EXCEL_DATE_FMT_IDS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}


# ===========================================================================
# 读取
# ===========================================================================
def _col_index(ref: str) -> int:
    """'AB12' -> 27 (0-based 列号)"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _xlsx_date_style_indices(z: zipfile.ZipFile) -> set[int]:
    """解析 styles.xml，找出哪些 cellXfs 下标是日期格式。

    必须这么做：把数字 450 当日期会毁掉尿酸这类指标，所以只能靠格式判断。
    """
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except KeyError:
        return set()

    custom_dates = set()
    for nf in root.iter(f"{NS}numFmt"):
        code = (nf.get("formatCode") or "").lower()
        fid = int(nf.get("numFmtId") or -1)
        if re.search(r"[ymd]", re.sub(r"\[[^\]]*\]|\"[^\"]*\"", "", code)):
            custom_dates.add(fid)

    date_fmt_ids = EXCEL_DATE_FMT_IDS | custom_dates

    # 必须只在 cellXfs 里数下标 —— cellStyleXfs 也有 <xf>，
    # 混在一起数会让 cell 的 s= 指向错位，日期就认不出来。
    cellxfs = root.find(f"{NS}cellXfs")
    if cellxfs is None:
        return set()
    out = set()
    for i, xf in enumerate(cellxfs.findall(f"{NS}xf")):
        if int(xf.get("numFmtId") or -1) in date_fmt_ids:
            out.add(i)
    return out


def _serial_to_datetime(n: float) -> datetime:
    """Excel 序列号 -> datetime（含 1900 闰年 bug 修正）"""
    base = datetime(1899, 12, 30)
    return base + timedelta(days=float(n))


def read_xlsx(path: str) -> list[list[str]]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.iter(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))

        date_styles = _xlsx_date_style_indices(z)

        # 第一个工作表
        sheet_name = next((n for n in z.namelist()
                           if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), None)
        if not sheet_name:
            return []
        sroot = ET.fromstring(z.read(sheet_name))

        rows: list[list[str]] = []
        for row in sroot.iter(f"{NS}row"):
            cells: dict[int, str] = {}
            for c in row.iter(f"{NS}c"):
                ref = c.get("r") or ""
                idx = _col_index(ref) if ref else len(cells)
                ctype = c.get("t")
                style = c.get("s")

                if ctype == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{NS}t"))
                else:
                    v = c.find(f"{NS}v")
                    raw = v.text if v is not None and v.text is not None else ""
                    if ctype == "s":
                        val = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else raw
                    elif ctype == "b":
                        val = "TRUE" if raw == "1" else "FALSE"
                    else:
                        val = raw
                        # 只有格式是日期时才转换 —— 否则 450 会被误当日期
                        if raw and style is not None and int(style) in date_styles:
                            try:
                                dt = _serial_to_datetime(float(raw))
                                val = dt.strftime("%Y-%m-%d %H:%M" if dt.hour or dt.minute
                                                  else "%Y-%m-%d")
                            except (ValueError, OverflowError):
                                pass
                cells[idx] = (val or "").strip()

            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(i, "") for i in range(width)])

        # 去掉尾部全空行
        while rows and not any(x for x in rows[-1]):
            rows.pop()
        return rows


def read_csv(path: str) -> list[list[str]]:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        from charset_normalizer import from_bytes
        text = str(from_bytes(raw).best())

    rows = [[(c or "").strip() for c in row]
            for row in csv.reader(text.splitlines())]
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def read_grid(path: str) -> list[list[str]]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(path)
    if ext in (".csv", ".txt", ".tsv"):
        return read_csv(path)
    if ext == ".xls":
        raise ValueError("旧版 .xls 不支持 —— 请先另存为 .xlsx 或 .csv")
    raise ValueError(f"不支持的格式：{ext}")


# ===========================================================================
# 值解析
# ===========================================================================
def parse_value(text: str) -> tuple[float | None, str | None, list[str]]:
    """'<0.5 mmol/L' -> (0.5, 'mmol/L', ['lt'])

    value 是裸数字。缺失返回 (None, None, ['missing'])，绝不返回 0。
    """
    if text is None:
        return None, None, ["missing"]
    s = str(text).strip().translate(FULLWIDTH)
    if s.lower() in MISSING_TOKENS:
        return None, None, ["missing"]

    # 报告里常见的定性结果 —— 不是数字，但不能当缺失
    if QUALITATIVE_RE.match(s):
        return None, None, ["non_numeric", f"raw:{s}"]

    m = NUM_RE.match(s)
    if not m:
        return None, None, ["unparsed", f"raw:{s}"]

    op, num, rest = m.group(1), m.group(2).replace(",", ""), m.group(3).strip()
    flags = []
    if op:
        flags.append({"<": "lt", "≤": "le", ">": "gt", "≥": "ge"}.get(op[0], "cmp"))
    unit = rest if rest and len(rest) <= 12 else None
    if rest and not unit:
        flags.append(f"trailing:{rest}")
    return float(num), unit, flags


def parse_time(text: str) -> datetime | None:
    if not text:
        return None
    s = str(text).strip().translate(FULLWIDTH)

    m = TIME_RE.search(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        hm = re.search(r"(\d{1,2}):(\d{2})", s)
        try:
            return datetime(y, mo, d, int(hm.group(1)) if hm else 0,
                            int(hm.group(2)) if hm else 0)
        except ValueError:
            return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ===========================================================================
# 结构识别
# ===========================================================================
def _norm(s: str) -> str:
    return re.sub(r"[\s()（）:：\[\]【】_\-]", "", str(s or "")).lower()


UNIT_IN_NAME = re.compile(r"[（(]\s*([^（()）]{1,24})\s*[)）]\s*$")


def split_name_unit(text: str) -> tuple[str, str | None]:
    """'体重(kg)' -> ('体重', 'kg')；'糖化血红蛋白(%)' -> ('糖化血红蛋白', '%')

    宽表把单位写在列名里，这是白送的信息 —— 用它做单位统一。
    """
    t = (text or "").strip()
    m = UNIT_IN_NAME.search(t)
    if m:
        return t[:m.start()].strip(), m.group(1).strip()
    return t, None


def find_header(rows: list[list[str]]) -> int:
    """找表头行。返回行下标；找不到返回 -1。"""
    best, best_score = -1, 0
    for i, row in enumerate(rows[:30]):
        cells = [_norm(c) for c in row if c]
        if len(cells) < 2:
            continue
        score = 0
        for c in cells:
            for _role, hints in COLUMN_HINTS:
                if any(_norm(h) in c for h in hints):
                    score += 2
                    break
        # 表头通常短、且下一行不像表头
        avg_len = sum(len(c) for c in cells) / len(cells)
        if avg_len < 12:
            score += 1
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 3 else -1


def classify_columns(rows: list[list[str]], hdr_idx: int, terms: dict) -> dict[int, str]:
    """给每一列定角色。返回 {列号: 角色}，角色形如 time/metric/value/unit/meta/ignore/metric:<id>。"""
    header = rows[hdr_idx]
    body = [r for r in rows[hdr_idx + 1:] if any(r)]
    alias = _alias_index(terms)

    roles: dict[int, str] = {}
    for ci, cell in enumerate(header):
        name_only, _unit = split_name_unit(cell)
        key = _norm(cell)
        key_name = _norm(name_only)
        role = None

        if key:
            for r, hints in COLUMN_HINTS:
                if any(_norm(h) in key for h in hints):
                    role = r
                    break
            # 先按去单位后的名字查术语表（宽表：'体重(kg)' -> weight）
            if role is None and key_name in alias:
                role = f"metric:{alias[key_name]}"
            if role is None and key in alias:
                role = f"metric:{alias[key]}"

        # 用样本数据兜底：整列多数像时间 -> time；整列多数像数字 -> value
        if role is None:
            col = [r[ci] for r in body[:20] if ci < len(r) and r[ci]]
            if col:
                time_hit = sum(1 for v in col if parse_time(v))
                num_hit = sum(1 for v in col if parse_value(v)[0] is not None)
                if time_hit >= max(2, len(col) * 0.6):
                    role = "time"
                elif num_hit >= len(col) * 0.8 and key:
                    role = "metric_unmapped"
        roles[ci] = role or ("ignore" if not key else "unknown")
    return roles


def _alias_index(terms: dict) -> dict[str, str]:
    idx = {}
    for mid, meta in terms["metrics"].items():
        for a in [meta["name"]] + meta.get("aliases", []):
            idx[_norm(a)] = mid
    return idx


META_KEYS = ["姓名", "性别", "年龄", "出生日期", "门诊号", "住院号", "病案号",
             "医院", "科室", "送检医生", "采样时间", "检验时间", "检测时间",
             "报告时间", "检验方法", "设备", "床号"]


def extract_meta(rows: list[list[str]], hdr_idx: int) -> dict[str, str]:
    """表头之上的行，抽取 '键：值' 形式的报告元信息。

    值必须**在下一个键之前**结束，否则 '张三    性别' 会被整个吞掉。
    """
    meta: dict[str, str] = {}
    alt = "|".join(re.escape(k) for k in sorted(META_KEYS, key=len, reverse=True))
    pat = re.compile(rf"({alt})\s*[:：]\s*(.*?)(?=\s*(?:{alt})\s*[:：]|$)")

    for row in rows[:hdr_idx]:
        line = " ".join(c for c in row if c)
        for m in pat.finditer(line):
            val = m.group(2).strip(" \t,，;；")
            if val:
                meta[m.group(1)] = val
        # 也可能是 键 | 值 分成两格
        cells = [c for c in row if c]
        for i in range(len(cells) - 1):
            k = cells[i].rstrip(":：").strip()
            if k in META_KEYS:
                meta[k] = cells[i + 1].strip()
    return meta


def meta_time(meta: dict[str, str]):
    """报告式表格的时间不在列里，在元信息里。"""
    for k in ("采样时间", "检验时间", "检测时间", "报告时间"):
        if meta.get(k):
            t = parse_time(meta[k])
            if t:
                return t
    return None


def detect_layout(roles: dict[int, str]) -> str:
    """record = 每行一条测量（报告式）；wide = 每列一个指标"""
    mapped = [r for r in roles.values() if r.startswith("metric:")]
    if any(r == "metric" for r in roles.values()) and mapped == []:
        return "record"
    if len(mapped) >= 2:
        return "wide"
    if "metric" in roles.values():
        return "record"
    return "unknown"


# ===========================================================================
# 长表化
# ===========================================================================
def _convert_unit(value, unit, mid, terms):
    """统一到术语表里的内部单位。返回 (值, 内部单位, flags)"""
    internal = terms["metrics"][mid]["unit"]
    if value is None:
        return None, internal, []
    if not unit or _norm(unit) == _norm(internal):
        return value, internal, []

    for conv in terms.get("unit_conversions", {}).values():
        if not isinstance(conv, dict) or "factor" not in conv:
            continue
        if mid in conv.get("metric_group", []) and _norm(conv["from"]) == _norm(unit):
            f = conv["factor"]
            if f is None:
                return value, internal, [f"unit_unconverted:{unit}"]
            return round(value * f, 6), internal, [f"converted_from:{unit}", f"factor:{f}"]
    return value, internal, [f"unit_unknown:{unit}"]


def to_long(rows, hdr_idx, roles, terms, source, patient="", fallback_time=None):
    header = rows[hdr_idx]
    body = rows[hdr_idx + 1:]
    layout = detect_layout(roles)
    out: list[dict] = []

    time_cols = [c for c, r in roles.items() if r == "time"]
    metric_col = next((c for c, r in roles.items() if r == "metric"), None)
    value_col = next((c for c, r in roles.items() if r == "value"), None)
    unit_col = next((c for c, r in roles.items() if r == "unit"), None)

    def cell(row, ci):
        return row[ci] if ci is not None and ci < len(row) else ""

    for row in body:
        if not any(row):
            continue
        t = next((pt for c in time_cols if (pt := parse_time(cell(row, c)))), None)
        if t is None:
            # 报告式表格没有时间列 —— 用报告元信息里的采样时间
            t = fallback_time

        if layout == "record":
            raw_name = cell(row, metric_col)
            raw_val = cell(row, value_col)
            # 一条测量必须有值 —— 没有值的是脚注行（"检验科 王医生"）或空行
            if not raw_name or not raw_val:
                continue
            mid = _alias_index(terms).get(_norm(raw_name))
            if mid is None:
                out.append({"metric": "", "raw_metric": raw_name, "time": t,
                            "value": None, "unit_internal": "", "source": source,
                            "flags": ["metric_unmapped"]})
                continue
            val, inline_unit, flags = parse_value(raw_val)
            unit = cell(row, unit_col) or inline_unit
            val, iunit, uflags = _convert_unit(val, unit, mid, terms)
            out.append({"metric": mid, "raw_metric": raw_name, "time": t,
                        "value": val, "unit_internal": iunit, "source": source,
                        "flags": flags + uflags})

        elif layout == "wide":
            for ci, role in roles.items():
                if not role.startswith("metric:"):
                    continue
                mid = role.split(":", 1)[1]
                raw = cell(row, ci)
                val, inline_unit, flags = parse_value(raw)
                if val is None and "missing" in flags:
                    continue                      # 宽表里的空格是"这一列没测"，不是缺失记录
                # 单位优先取单元格里的，其次取列名括号里的
                _n, col_unit = split_name_unit(header[ci] if ci < len(header) else "")
                val, iunit, uflags = _convert_unit(val, inline_unit or col_unit, mid, terms)
                out.append({"metric": mid, "raw_metric": header[ci], "time": t,
                            "value": val, "unit_internal": iunit, "source": source,
                            "flags": flags + uflags})
    return out


# ===========================================================================
# 顶层
# ===========================================================================
def analyze(path: str, terms: dict) -> dict:
    rows = read_grid(path)
    hdr = find_header(rows)
    if hdr < 0:
        return {"file": path, "error": "找不到表头", "rows": len(rows)}
    roles = classify_columns(rows, hdr, terms)
    meta = extract_meta(rows, hdr)
    return {
        "file": os.path.basename(path),
        "sheets_rows": len(rows),
        "header_row": hdr + 1,
        "header": [c for c in rows[hdr]],
        "columns": {rows[hdr][c] if c < len(rows[hdr]) else f"col{c}": r
                    for c, r in sorted(roles.items())},
        "layout": detect_layout(roles),
        "report_meta": meta,
        "data_rows": len([r for r in rows[hdr + 1:] if any(r)]),
    }


def ingest(paths: list[str], terms_path: str = "terms.json") -> list[dict]:
    terms = json.load(open(terms_path, encoding="utf-8"))
    records: list[dict] = []
    for p in paths:
        rows = read_grid(p)
        hdr = find_header(rows)
        if hdr < 0:
            print(f"[warn] {p}: 找不到表头，跳过", file=sys.stderr)
            continue
        roles = classify_columns(rows, hdr, terms)
        meta = extract_meta(rows, hdr)
        patient = meta.get("姓名", "") or meta.get("门诊号", "")
        records += to_long(rows, hdr, roles, terms, os.path.basename(p), patient,
                           fallback_time=meta_time(meta))
    return records


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    profile = "--profile" in argv
    argv = [a for a in argv if a != "--profile"]
    out = None
    if "-o" in argv:
        i = argv.index("-o")
        out = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    paths = argv[1:]

    terms = json.load(open("terms.json", encoding="utf-8"))

    if profile:
        for p in paths:
            info = analyze(p, terms)
            print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    recs = ingest(paths)
    if out:
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["metric", "time", "value", "unit_internal", "source", "flags"])
            for r in recs:
                w.writerow([r["metric"], r["time"].isoformat(sep=" ") if r["time"] else "",
                            r["value"] if r["value"] is not None else "",
                            r["unit_internal"], r["source"], "|".join(r["flags"])])
        print(f"写出 {len(recs)} 条 -> {out}")
    else:
        for r in recs[:40]:
            print(r)
        print(f"... 共 {len(recs)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
