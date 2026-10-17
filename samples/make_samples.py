# -*- coding: utf-8 -*-
"""造两个测试样本：报告式 CSV（长表）和随访式 XLSX（宽表）。

XLSX 用手写的最小写入器生成（没有 openpyxl），并且刻意放两个陷阱：
  · 「日期」列用 Excel 序列号 + 日期格式
  · 「尿酸」列是普通数字 450 —— 如果按数值范围猜日期，450 会被误读成 1931 年
"""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_write(path: str, sheet: list[list], date_cols: set[int] = frozenset()):
    """sheet: 行列表；单元格为 str / float / int / None。date_cols 里的 float 按日期格式写。"""
    strings: list[str] = []
    sidx: dict[str, int] = {}

    def sid(s: str) -> int:
        if s not in sidx:
            sidx[s] = len(strings)
            strings.append(s)
        return sidx[s]

    rows_xml = []
    for ri, row in enumerate(sheet, start=1):
        cells = []
        for ci, val in enumerate(row):
            if val is None or val == "":
                continue
            ref = f"{col_letter(ci)}{ri}"
            if isinstance(val, str):
                cells.append(f'<c r="{ref}" t="s"><v>{sid(val)}</v></c>')
            elif ci in date_cols:
                cells.append(f'<c r="{ref}" s="1"><v>{val}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
        rows_xml.append(f'<row r="{ri}">{"".join(cells)}</row>')

    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    sst = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           f'count="{len(strings)}" uniqueCount="{len(strings)}">'
           + "".join(f"<si><t>{esc(s)}</t></si>" for s in strings) + "</sst>")

    # cellXfs: 0=默认, 1=日期(numFmtId 14)
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
              '<borders count="1"><border/></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2">'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '</cellXfs></styleSheet>')

    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 f'<sheetData>{"".join(rows_xml)}</sheetData></worksheet>')

    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
              '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
              '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
              '</Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wbrels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/styles.xml", styles)


def excel_serial(y, m, d):
    from datetime import datetime
    return (datetime(y, m, d) - datetime(1899, 12, 30)).days


def main():
    os.makedirs(HERE, exist_ok=True)

    # ---- 样本 1：检验报告（长表式 CSV），带报告元信息和脚注 ----
    csv_text = """某市第一医院  检验报告单
姓名：张三    性别：男    年龄：45    门诊号：M001
采样时间：2026-03-15 08:30    报告时间：2026-03-15 14:00
科室：内分泌科    检验方法：酶法

项目名称,结果,单位,参考范围
糖化血红蛋白,8.2,%,4.0-6.0
空腹血糖,7.8,mmol/L,3.9-6.1
总胆固醇,5.2,mmol/L,<5.2
甘油三酯,2.1,mmol/L,<1.7
肌酐,88,umol/L,57-97
尿酸,450,umol/L,208-428
高密度脂蛋白胆固醇,0.9,mmol/L,>1.0
低密度脂蛋白胆固醇,3.6,mmol/L,<3.4
eGFR,72,mL/min/1.73m2,>90
尿微量白蛋白,<0.5,mg/L,<30

检验科  王医生
"""
    p1 = os.path.join(HERE, "sample_report.csv")
    open(p1, "w", encoding="utf-8-sig").write(csv_text)

    # ---- 样本 2：随访宽表（XLSX），日期是序列号，尿酸是普通数字 450 ----
    header = ["日期", "体重(kg)", "收缩压(mmHg)", "舒张压(mmHg)",
              "糖化血红蛋白(%)", "尿酸(umol/L)"]
    body = [
        (excel_serial(2026, 1, 10), 81.5, 142, 88, 8.0, 460),
        (excel_serial(2026, 2, 10), 80.8, 138, 86, None, 445),
        (excel_serial(2026, 3, 10), 80.0, 136, 84, 7.6, 450),
    ]
    p2 = os.path.join(HERE, "sample_followup.xlsx")
    xlsx_write(p2, [header] + [list(r) for r in body], date_cols={0})

    print("生成：")
    print("  ", p1)
    print("  ", p2)


if __name__ == "__main__":
    main()
