# Verifiable Clinical Report

> **让临床 LLM 报告里的数字不可能被编造。**

一个面向慢性病随访的多源数据分析系统。它把散落的检验、监测数据整理成医生可读的趋势报告——**但不做任何诊断，也不做任何临床评价**。

它的核心不是"用 LLM 生成报告"，而是：

> **把 LLM 限制在流水线两端，中间全部确定性化；数值与其语义绑定分离；校验发生在产物写入时。**
> 结果：报告里**每一个数字都能追溯到源数据**，且这个性质可验证、不随模型强弱变化。

**English**: A chronic-disease follow-up analysis pipeline where the report layer never sees a single number. Values and their semantic bindings (metric, unit) travel separately through a deterministic middle; the LLM only parses the question and organises verified claims into prose. Hallucinated numbers are therefore **impossible by construction**, not merely unlikely.

---

## 核心主张（可证伪）

| # | 主张 | 怎么证伪 |
|---|---|---|
| 1 | 报告层**物理上不可能**编造数字 | 试着让它编一个 —— 做不到，它从未接收过数字 |
| 2 | 8 条边界中 **7 条零成本校验** | 数 token；注入错误看检出率 |
| 3 | 正确性**不随模型强弱变化** | 换 `stub` 与 `Qwen3-4B`，报告共有句逐字相同 |
| 4 | 分析口径**可见** | 撤掉框架层，看能否发现"365 天窗口把 4 年历史截成 1 个点" |

---

## 为什么这么设计

| 常见做法 | 本系统 | 换来了什么 |
|---|---|---|
| 全流程交给 LLM | LLM 只在**两端**（自然语言进、自然语言出） | 上游幻觉**没有通道**传到下游 |
| 数据流里传 `"体重80kg"` 字符串 | 传 `80` + 模式声明 `体重:kg` | 数字接地从**语义判断**退化为**精确比对** |
| 产出后通读找错 | 在**产物写入那一刻**校验 | 上游上下文还在，能查到传递后查不到的东西 |
| 提示工程 / LLM 评判 / 自纠 | 7/8 条边是确定性校验 | 成本降一个数量级，且 sound 而非 heuristic |

**六个环节里只有三个用 LLM：**

```
医生的话
  │
  ├─[① 意图解析]        🧠 LLM     自然语言 → 结构化需求
  │
  ├─[② 可满足性预检]     ⚙️ 代码     只看元数据，不碰数据值
  ├─[③ 清洗]            ⚙️ 代码     单位统一，之后只传裸数字
  ├─[④ 分析]            🧠 规划 + ⚙️ 计算    数字全部由代码算出
  ├─[⑤ 选图]            ⚙️ 规则     需求 + 术语表 → 图型
  ├─[⑥ 画图]            ⚙️ 代码     纯映射，不平滑/不插值/不聚合
  │
  └─[⑦ 报告]            🧠 LLM     命题 → 自然语言（数字用占位符，一个都不生成）
```

**中段零模型依赖、零 ML 库依赖** —— 斜率、均值、选图、刻度、SVG 全部是纯 Python。

---

## 快速开始

**核心部分不需要任何第三方依赖**（纯标准库）：

```bash
git clone <this-repo>
cd verifiable-clinical-report

# 1. 用自带的样本跑一次（不加载模型，秒出）
python run.py --demo --backend stub

# 2. 校验自测：注入 7 类错误，看是否都能抓到
python tests/test_verification.py

# 3. 意图解析测试
python tests/test_intent.py
```

输出长这样：

```
【结论】
  糖化血红蛋白最近一次为 6.8 %。
  糖化血红蛋白窗口内呈下降趋势（-0.18 %/月）。
  收缩压最近一次为 130 mmHg。
  收缩压窗口内呈上升趋势（0.04 mmHg/月）。

【校验】reject=0 flag=1 → 通过
```

### 用真实模型

```bash
pip install torch transformers accelerate
# 把 Qwen3-4B 权重放到 models/Qwen3-4B/（或设 MODELS_DIR）
python run.py --demo --backend local
```

或指向任何 OpenAI 兼容服务（vLLM / llama.cpp / LM Studio / Ollama）：

```bash
LLM_BACKEND=openai LLM_BASE_URL=http://127.0.0.1:8000/v1 \
  LLM_MODEL=Qwen3-4B python run.py --demo
```

### Web 界面

```bash
python web/server.py --port 8100 --backend stub --open
# →  http://127.0.0.1:8100
```

Windows 下也可以双击 `start_web.bat`。

---

## 能验证什么

8 条边界，7 条零成本：

| 边界 | 校验什么 | 方法 | 成本 |
|---|---|---|---|
| 提问 → 需求 | schema、指标在术语表内、时间可解析 | 查表 | ~0 |
| 需求 → 预检 | 数据存在性、覆盖度 | 查表 | ~0 |
| 预检 → 清洗 | 每个值能追溯到原始字段 | 映射可追溯 | ~0 |
| **清洗 → 分析** | **统计量能重算出来** | **重算比对** | ~0 |
| 分析 → 选图 | 引用完整性、图型合规、误导性 lint | 规则表 | ~0 |
| 选图 → 画图 | 重渲染逐像素比对 | 重渲染 | ~0 |
| 画图 → 报告 | 每句有引用；**数字与命题一致** | 精确比对 | ~0 |
| 出口 | 句子是否忠实于引用的命题 | LLM（局部） | 低 |

**7 类注入错误的检出结果**：

```
报告里编造数字          数字有依据      ✓
报告里出现临床评价      临床越界        ✓
命题数值被篡改          重算            ✓
报告句没有引用          引用完整性      ✓
命题引用不存在的需求     需求一致性      ✓
图表引用不存在的命题     引用完整性      ✓
单位与术语表不一致      写入被拒        ✓
```

> ⚠️ **重算的边界**：它验证"命题声明的口径下值算得对"，**不能**验证"口径选对了"
> —— 因为窗口是分析层写的、重算又用它，这是循环的。后者由「需求一致性」负责。

---

## 框架层：把系统的取舍摊开

系统的分析口径不是中立的——**换一段窗口、换一个基线，结论可能整体反转**。所以系统必须把这些决定显式列出：

```
使用了      hba1c、sbp、dbp
该患者共有  12 个指标
未使用      bmi、creatinine、hdl、ldl、tc、wbc …
窗口        hba1c: 547 天   sbp: 1461 天
基线        首次记录日期
数据完整度  每项指标的 n / 缺失率 / 来源
```

**这不是装饰。** 实测中曾出现：默认 365 天窗口把某患者 4 年的血压历史**截成 1 个数据点**（n=1，缺失率 88%），报告退化成"仅 1 个数据点，未计算趋势"。修好后 n: 1 → 8，趋势才出来。

**框架层是唯一让这件事可见的机制。** 没有它，医生看到的是一份"看起来正常"的残缺报告。

---

## 设计取舍（诚实列出）

| 取舍 | 说明 |
|---|---|
| **范围窄** | 只适用于输出可限定为**可重算的描述性命题**的任务。诊断性推理不在此列。 |
| **不做诊断** | 不是规避，是**前提**——正因为输出被限定在可重算命题上，校验才能 sound |
| **抽取不是 100%** | 基于规则/别名的抽取在定性描述（"within normal limits"）上无能为力 |
| **不保证临床价值** | 报告是否被医生读、净收益是否为正，本文不回答 |
| **不声称更准** | 贡献不依赖"我们更准"，而依赖"某类错误不可能发生" |

---

## 术语表（`terms.json`）

系统的契约基础。定义每个指标的：标准名、单位、别名、时间语义、单位换算规则，以及**可合并到同一张图的指标组**。

```json
"hba1c": {
  "name": "糖化血红蛋白", "unit": "%",
  "temporal": "window_mean", "window_days": 90,
  "aliases": ["糖化血红蛋白", "HbA1c", "A1C", "GHb"]
}
```

**三个必须守住的约束**（都踩过坑）：

1. **别名之间不允许互为子串** —— `"糖化血红蛋白"` 含 `"血红蛋白"`，子串匹配会把 hba1c 和 hemoglobin 同时命中，系统于是分析了医生没问的指标。解法：长别名优先占位，短别名与已占区间重叠就丢弃。
2. **单位量纲不能靠别名归一** —— `/µL` 和 `10³/µL` 差 1000 倍。必须走带因子的换算，否则 `12,000/µL` 会被当成 `12000×10³/µL`（看着完全合理）。
3. **缺失必须是 `None`，绝不能是 `0`** —— 血糖缺失被当 0，图上会出现一个不存在的致命低血糖点。

---

## 输入数据

支持 Excel / CSV 检验报告（`.xlsx` / `.csv`），以及 **Synthetic Hospital** 数据集适配器。

> **Synthetic Hospital** — Park, Chen & Dettmers. *An Open, Verifiable, Physician-Validated Longitudinal EHR Benchmark.* arXiv:2609.30027, 2026.
> 需自行下载并放到 `data/synthetic_hospital/`（或设 `SYNTHETIC_HOSPITAL_ROOT`）：
> <https://github.com/sparkcpark/synthetic_hospital>

该数据集含 1,268 名纵向患者 / 5,602 次就诊。本系统的规则抽取在其中 **1,015 名患者上有效**（80%），**466 名患者至少有一条指标可算出趋势**。

---

## 目录

```
app/
  schemas.py     产物 schema + 契约校验 + 越界词表
  llm.py         LLM 后端（stub / local / openai，可插拔）
  store.py       共享事实库（版本戳 + 写入拒绝记录）
  agents.py      六个智能体
  verify.py      八类校验
  pipeline.py    编排 + 框架层输出
  adapters/      数据源适配器
ingest.py        Excel/CSV 识别与长表化（纯标准库）
terms.json       术语表
run.py           CLI
web/             Web 前端（FastAPI + 单文件页面）
tests/           错误注入测试 + 意图解析测试
samples/         样本生成器
```

---

## 依赖

| 用途 | 依赖 |
|---|---|
| 核心（stub 后端、清洗、分析、校验） | **无**（纯标准库） |
| Web 界面 | `fastapi` `uvicorn` `pydantic` |
| 本地模型 | `torch` `transformers` `accelerate` |
| CSV 编码兜底（可选） | `charset_normalizer` |

---

## License

MIT。见 [LICENSE](LICENSE)。
