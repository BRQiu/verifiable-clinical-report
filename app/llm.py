# -*- coding: utf-8 -*-
"""LLM 后端 —— 可插拔。

系统里只有三处用 LLM（意图解析、报告生成、可选的语义校验），全部通过这个接口，
所以换模型 / 换推理服务不用动任何业务代码。

后端：
    stub        确定性桩，不加载模型 —— 用来跑通全流程、做回归测试
    local       transformers 直接加载 models/ 下的权重
    openai      任何 OpenAI 兼容的 HTTP 服务（vLLM / llama.cpp / LM Studio / Ollama）

选择优先级（环境变量 LLM_BACKEND 可强制指定）：
    显式指定 > local（torch 可用且模型存在） > stub
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 模型目录：默认用仓库下的 models/，可用 MODELS_DIR 环境变量覆盖
MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(_ROOT, "models"))

# 12GB 显存：4B 的 fp16 约 8GB，能跑；14B 的 fp16 约 28GB，跑不了
DEFAULT_LOCAL_MODEL = "Qwen3-4B"


class LLMError(RuntimeError):
    pass


class LLMBackend:
    name = "base"

    def generate(self, system: str, user: str, max_tokens: int = 1024,
                 json_only: bool = False) -> str:
        raise NotImplementedError

    def generate_json(self, system: str, user: str, max_tokens: int = 1024) -> dict:
        raw = self.generate(system, user, max_tokens=max_tokens, json_only=True)
        return extract_json(raw)


def extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象。模型常在 JSON 外面裹一层解释。"""
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    raise LLMError(f"输出里没有合法 JSON：{text[:200]}")


def strip_thinking(text: str) -> str:
    """去掉 Qwen3 泄漏出来的思考段。

    关闭 thinking 之后多数情况不带，但不同版本的模板行为不一致，
    所以这里再兜一次：只保留 </think> 之后的内容。
    """
    if not text:
        return text
    for tag in ("</think>", "</thinking>", "<｜end▁of▁thinking｜>"):
        if tag in text:
            text = text.rsplit(tag, 1)[-1]
    return text.strip()


# ==========================================================================
# 桩后端：确定性规则，不加载模型
# ==========================================================================
class StubBackend(LLMBackend):
    """确定性桩。

    不是"随便返回点东西"——它按规则真的把中文问句解析成需求、
    真的按模板生成带占位符的报告。这样整条流水线可以被端到端测试。
    """
    name = "stub"

    def __init__(self, terms: dict):
        self.terms = terms

    def generate(self, system, user, max_tokens=1024, json_only=False) -> str:
        if "意图解析" in system:
            return json.dumps(self._intent(user), ensure_ascii=False)
        if "报告" in system:
            return self._report(user)
        return "{}"

    # ---- 意图解析：关键词匹配 ----
    def _intent(self, q: str) -> dict:
        """从问题里挑出指标。必须处理"别名互相包含"的撞车：

            「糖化血红蛋白」里含「血红蛋白」
            -> 若按子串逐个匹配，会同时命中 hba1c 和 hemoglobin，
               于是系统去分析了一个医生根本没问的指标（跑偏）。

        做法：把命中按别名**长度从长到短**排序，长别名先占位并屏蔽其区间，
        短别名若与已占区间重叠就丢弃。
        """
        hits: list[tuple[int, int, str]] = []      # (start, end, metric)
        cands: list[tuple[int, str, str]] = []
        for mid, meta in self.terms["metrics"].items():
            for a in [meta["name"]] + meta.get("aliases", []):
                if a and a in q:
                    cands.append((len(a), a, mid))
        cands.sort(key=lambda x: -x[0])            # 长别名优先

        metrics: list[str] = []
        for ln, alias, mid in cands:
            for m in re.finditer(re.escape(alias), q):
                s, e = m.start(), m.end()
                if any(not (e <= us or s >= ue) for us, ue in hits):
                    continue                        # 与更长的别名重叠 -> 丢弃
                hits.append((s, e))
                if mid not in metrics:
                    metrics.append(mid)
                break

        # 笼统说法（"血糖""血压"）展开成全部候选，但同样要避开已被长别名占用的区间
        for term, mids in self.terms.get("generic_terms", {}).items():
            if term.startswith("_"):
                continue
            for m in re.finditer(re.escape(term), q):
                s, e = m.start(), m.end()
                if any(not (e <= us or s >= ue) for us, ue in hits):
                    continue
                hits.append((s, e))
                metrics += [x for x in mids if x not in metrics]
                break

        seen, ordered = set(), []
        for m in metrics:
            if m not in seen:
                seen.add(m)
                ordered.append(m)
        metrics = ordered

        m = re.search(r"(近|最近|过去)\s*([一二三四五六七八九十\d]+)\s*(个?月|年|周|天)", q)
        days = 365
        explicit = False
        if m:
            explicit = True
            num = m.group(2)
            num = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                   "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(num, None) \
                or (int(num) if num.isdigit() else 1)
            unit = m.group(3)
            days = num * {"月": 30, "个?月": 30, "年": 365, "周": 7, "天": 1}.get(
                unit.replace("个", ""), 30)

        aspect = "trend"
        if any(k in q for k in ("变化", "趋势", "走势", "升", "降")):
            aspect = "trend"
        elif any(k in q for k in ("最新", "现在", "目前")):
            aspect = "latest"
        elif any(k in q for k in ("对比", "比较", "比")):
            aspect = "compare"
        elif any(k in q for k in ("分布", "波动")):
            aspect = "distribution"

        return {"requirements": [
            {"rid": f"r{i+1}", "metric": mid, "window_days": days, "aspect": aspect,
             "explicit_window": explicit}
            for i, mid in enumerate(metrics)
        ]}

    # ---- 报告：模板 + 占位符，一个数字都不生成 ----
    def _report(self, payload: str) -> str:
        data = json.loads(payload)
        claims = data["claims"]
        lines = []
        for c in claims:
            unit = c["unit"]
            if c["statistic"] == "latest":
                lines.append(f"{c['label']}最近一次为 {{{c['cid']}}}。")
            elif c["statistic"] == "mean":
                lines.append(f"{c['label']}窗口内均值为 {{{c['cid']}}}。")
            elif c["statistic"] == "slope":
                if c["value"] is None:
                    # 说清"为什么没有趋势"，并且挂在命题上 —— 不是无依据的空话
                    lines.append(f"{c['label']}仅 {{{c['cid']}.n}} 个数据点，未计算趋势。")
                elif abs(c["value"]) < 1e-9:
                    lines.append(f"{c['label']}窗口内近似持平（{{{c['cid']}}}）。")
                else:
                    d = "上升" if c["value"] > 0 else "下降"
                    lines.append(f"{c['label']}窗口内呈{d}趋势（{{{c['cid']}}}）。")
            elif c["statistic"] == "delta":
                d = "增加" if (c["value"] or 0) > 0 else "减少"
                lines.append(f"{c['label']}较上一次{d} {{{c['cid']}}}。")
            elif c["statistic"] in ("min", "max"):
                zh = "最低" if c["statistic"] == "min" else "最高"
                lines.append(f"{c['label']}窗口内{zh}值为 {{{c['cid']}}}。")
        return "\n".join(lines)


# ==========================================================================
# 本地 transformers 后端
# ==========================================================================
class LocalHFBackend(LLMBackend):
    name = "local"

    def __init__(self, model_dir: str | None = None, dtype: str | None = None):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as e:
            raise LLMError(
                "本地后端需要 torch 与 transformers。"
                "可改用 LLM_BACKEND=openai 指向推理服务，或用 LLM_BACKEND=stub 跑通流程。"
            ) from e

        path = model_dir or os.path.join(MODELS_DIR, DEFAULT_LOCAL_MODEL)
        if not os.path.isdir(path):
            raise LLMError(f"模型目录不存在：{path}")

        self.torch = torch
        # bf16 比 fp16 数值范围更大，12GB 显存跑 4B 更稳；显存不足时回退 fp16
        if dtype is None:
            dtype = "bfloat16" if torch.cuda.is_available() else "float32"
        torch_dtype = getattr(torch, dtype)

        self.tok = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            path,
            dtype=torch_dtype,          # transformers 5.x：torch_dtype 已弃用
            device_map="auto" if torch.cuda.is_available() else None,
            low_cpu_mem_usage=True,     # 分块加载，降低峰值内存
            attn_implementation="sdpa",  # 比默认 eager 快不少
            trust_remote_code=True,
        )
        if not torch.cuda.is_available():
            self.model = self.model.to("cpu")
        self.model.eval()
        self.path = path
        self.device = next(self.model.parameters()).device

    def generate(self, system, user, max_tokens=1024, json_only=False) -> str:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        # Qwen3 默认先输出思考过程。正确做法是用模板自己的开关关掉，
        # 手工拼  thinking 标签在部分版本上不生效（会漏出 </think>）。
        kwargs = {}
        if "qwen3" in self.path.lower():
            kwargs["enable_thinking"] = False
        try:
            text = self.tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, **kwargs)
        except TypeError:
            text = self.tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)

        inputs = self.tok(text, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=max_tokens,
                                      do_sample=False)
        gen = out[0][inputs["input_ids"].shape[1]:]
        raw = self.tok.decode(gen, skip_special_tokens=True)
        return strip_thinking(raw)


# ==========================================================================
# OpenAI 兼容后端
# ==========================================================================
class OpenAICompatBackend(LLMBackend):
    name = "openai"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None):
        self.base = (base_url or os.environ.get("LLM_BASE_URL")
                     or "http://127.0.0.1:8000/v1").rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_LOCAL_MODEL)
        self.key = api_key or os.environ.get("LLM_API_KEY", "not-needed")

    def generate(self, system, user, max_tokens=1024, json_only=False) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if json_only:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"},
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]


# ==========================================================================
# 选择后端
# ==========================================================================
def get_backend(terms: dict, prefer: str | None = None) -> LLMBackend:
    want = (prefer or os.environ.get("LLM_BACKEND") or "").lower()

    if want == "stub":
        return StubBackend(terms)
    if want == "openai":
        return OpenAICompatBackend()
    if want == "local":
        return LocalHFBackend()

    # 自动：能用本地就用本地，否则退到桩
    try:
        return LocalHFBackend()
    except LLMError as e:
        print(f"[llm] 本地模型不可用（{e}）—— 回退到 stub", file=sys.stderr)
        return StubBackend(terms)
