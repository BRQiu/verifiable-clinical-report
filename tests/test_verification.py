# -*- coding: utf-8 -*-
"""错误注入测试 —— 证明校验真的会拒绝，而不是永远通过。

注入 6 类错误，每一类都必须被对应的检查抓出来：
    1. 报告里编造数字      -> 数字有依据
    2. 报告里出现临床评价  -> 临床越界
    3. 命题数值被篡改      -> 重算
    4. 报告句没有引用      -> 引用完整性
    5. 命题引用不存在的需求 -> 需求一致性
    6. 图表引用不存在的命题 -> 引用完整性
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import pipeline, schemas, verify  # noqa: E402
from app.llm import StubBackend  # noqa: E402


def fresh():
    terms = pipeline.load_terms()
    # 测试固定用桩：确定性、不加载模型、7 个用例不必各加载一次
    p = pipeline.Pipeline(terms=terms, backend=StubBackend(terms))
    p.load_files([os.path.join(pipeline.ROOT, "samples", "sample_report.csv"),
                  os.path.join(pipeline.ROOT, "samples", "sample_followup.xlsx")])
    return p


def base_run(p):
    return p.run("看看张三最近的血糖和体重变化")


def main():
    p = fresh()
    r = base_run(p)
    print(f"基线：reject={r['verification']['counts']['reject']} "
          f"flag={r['verification']['counts']['flag']}  "
          f"passed={r['verification']['passed']}")
    assert r["verification"]["passed"], "基线就该通过，否则后面测不准"
    print()

    cases = []

    # ---- 1. 报告里编造数字 ----
    p = fresh(); base_run(p)
    sents = copy.deepcopy(p.store.sentences)
    sents.append({"resolved": "糖化血红蛋白为 9.9 %。", "raw": "糖化血红蛋白为 9.9 %。",
                  "claim_ids": ["c1"], "unresolved": []})
    v = verify.verify_all(p.store, sents)
    cases.append(("报告里编造数字", "数字有依据",
                  any(f["check"] == "数字有依据" for f in v["rejects"])))

    # ---- 2. 报告里出现临床评价 ----
    p = fresh(); base_run(p)
    sents = copy.deepcopy(p.store.sentences)
    sents.append({"resolved": "血糖控制不佳。", "raw": "血糖控制不佳。",
                  "claim_ids": ["c1"], "unresolved": []})
    v = verify.verify_all(p.store, sents)
    cases.append(("报告里出现临床评价", "临床越界",
                  any(f["check"] == "临床越界" for f in v["rejects"])))

    # ---- 3. 命题数值被篡改 ----
    p = fresh(); base_run(p)
    p.store.claims["c1"]["value"] = 3.3      # 真实值是 8.2
    v = verify.verify_all(p.store, p.store.sentences)
    cases.append(("命题数值被篡改", "重算",
                  any(f["check"] == "重算" for f in v["rejects"])))

    # ---- 4. 报告句没有引用 ----
    p = fresh(); base_run(p)
    sents = copy.deepcopy(p.store.sentences)
    sents.append({"resolved": "总体情况平稳。", "raw": "总体情况平稳。",
                  "claim_ids": [], "unresolved": []})
    v = verify.verify_all(p.store, sents)
    cases.append(("报告句没有引用", "引用完整性",
                  any(f["check"] == "引用完整性" for f in v["rejects"])))

    # ---- 5. 命题引用不存在的需求 ----
    p = fresh(); base_run(p)
    bad = copy.deepcopy(p.store.claims["c1"])
    bad["cid"] = "cX"; bad["rid"] = "r999"
    p.store.claims["cX"] = bad
    v = verify.verify_all(p.store, p.store.sentences)
    cases.append(("命题引用不存在的需求", "需求一致性",
                  any(f["check"] == "需求一致性" for f in v["rejects"])))

    # ---- 6. 图表引用不存在的命题 ----
    p = fresh(); base_run(p)
    sid = next(iter(p.store.charts))
    p.store.charts[sid]["claims"] = ["c404"]
    v = verify.verify_all(p.store, p.store.sentences)
    cases.append(("图表引用不存在的命题", "引用完整性",
                  any(f["check"] == "引用完整性" for f in v["rejects"])))

    # ---- 7. 单位不一致（写入时契约就该拦）----
    p = fresh(); base_run(p)
    bad = copy.deepcopy(p.store.claims["c1"])
    bad["cid"] = "cY"; bad["unit"] = "mg/dL"
    ok = p.store.put_claim(bad, schemas.validate_claim)
    cases.append(("单位与术语表不一致", "写入被拒", not ok))

    print(f"{'注入的错误':<22} {'应被谁抓到':<14} 结果")
    print("-" * 56)
    allok = True
    for name, who, caught in cases:
        print(f"{name:<22} {who:<14} {'✓ 抓到' if caught else '✗ 漏了'}")
        allok = allok and caught

    print("-" * 56)
    print("全部抓到" if allok else "有漏网 —— 校验不可信")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
