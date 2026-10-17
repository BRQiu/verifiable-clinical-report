# -*- coding: utf-8 -*-
"""验证意图解析：不再因为别名互相包含而分析医生没问的指标。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.llm import StubBackend  # noqa: E402
from app.pipeline import load_terms  # noqa: E402

be = StubBackend(load_terms())

CASES = [
    ("看看这个患者的糖化血红蛋白变化", ["hba1c"], "含'血红蛋白'的别名不该带出 hemoglobin"),
    ("看看血红蛋白", ["hemoglobin"], "只说血红蛋白就只出 hemoglobin"),
    ("看看这个患者近两年的糖化血红蛋白和血压变化",
     ["hba1c", "sbp", "dbp"], "血压展开成收缩+舒张，且不带出 hemoglobin"),
    ("看看血糖", ["glucose_fasting", "glucose_postprandial", "glucose_random"],
     "笼统'血糖'展开成三个候选"),
    ("看看空腹血糖", ["glucose_fasting"], "明确说了空腹就只出空腹"),
    ("看看血脂和肌酐", ["tc", "tg", "hdl", "ldl", "creatinine"], "血脂展开成四项"),
]

print(f"{'问题':<40} {'解析出的指标':<52} 判定")
print("-" * 110)
allok = True
for q, want, why in CASES:
    got = [r["metric"] for r in be._intent(q)["requirements"]]
    ok = sorted(got) == sorted(want)
    allok = allok and ok
    extra = sorted(set(got) - set(want))
    miss = sorted(set(want) - set(got))
    tag = "✓" if ok else f"✗ 多{sorted(extra)} 少{sorted(miss)}"
    print(f"{q:<40} {str(got):<52} {tag}")
    if not ok:
        print(f"{'':<40} （{why}）")

print("-" * 110)
print("全部正确" if allok else "仍有问题")
