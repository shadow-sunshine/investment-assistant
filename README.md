# 智能投研助手

一个面向学习和作品集展示的、可追溯投研工作流项目。项目从老师提供的“感知 → 建模 → 推理 → 决策 → 报告”思路迁移而来，但重新实现为独立工程，并补充了真实数据、RAG、风险控制和离线评估。

- **真实市场与基本面数据**：通过 `yfinance` 获取历史 OHLCV、收益、波动率、最大回撤、近期新闻，以及年度营收、净利润、自由现金流、滚动 PE / PB 与各指标数据日期；
- **本地 RAG**：使用 Chroma 持久化文本、Markdown 和 PDF 资料；PDF 采用逐页解析，检索引用保留文件名和页码；
- **混合检索**：默认使用无需下载模型的确定性哈希向量，并以字面 token 覆盖率做轻量重排；可选安装 SentenceTransformers 并设置 `RAG_EMBEDDING_MODE=semantic` 使用本地多语言语义嵌入；
- **风险控制**：行情与财报/估值的可用性和时效检查、财报期末日期检查、引用/页码完整性检查、收益承诺拦截、免责声明；
- **离线评估**：检查报告结构、引用、财报日期、PDF 页码覆盖率；对人工标注的预期证据片段可计算召回率。

> 仅用于学习和研究，不构成投资建议。Yahoo Finance / yfinance 数据可能延迟、缺失或被修订，所有结果必须自行复核。

## 安装

```powershell
cd D:\It\Test_Project\Investment_Assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 可选：启用本地语义嵌入

默认 `hash` 模式不下载模型，适合稳定的离线演示。若希望使用多语言语义嵌入：

```powershell
pip install -r requirements-semantic.txt
$env:RAG_EMBEDDING_MODE="semantic"
# 可选：覆盖默认模型名
$env:RAG_SEMANTIC_MODEL="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
```

首次加载模型会下载权重。若依赖或模型不可用，系统会自动降级为哈希向量，并在审计记录的 `retrieval_status.fallback_reason` 中说明原因。

## 建立知识库

将 `.txt`、`.md` 或可提取文本的 `.pdf` 放进：

```text
data\knowledge_base
```

然后执行：

```powershell
python -m investment_assistant.cli index
```

PDF 页会逐页索引；扫描版 PDF 若没有文字层会被跳过而不会生成伪造内容。报告的来源列表会显示 PDF 文件名和页码。

## 生成研究简报

```powershell
python -m investment_assistant.cli research --ticker AAPL --topic "AI 基础设施" --horizon 中期
```

报告和机器可读审计 JSON 会写入 `data\reports`。JSON 同时保存行情、财报、估值、RAG 来源、检索配置和两套评估结果。

## 离线质量检查

```powershell
python -m investment_assistant.cli evaluate
```

## 工作流

```text
真实数据采集（行情、财报、估值、新闻）
  → PDF/文本 RAG 检索与重排
  → 市场建模
  → 情景推理
  → 带来源和页码的研究简报
  → 风险验证、报告评估、检索质量评估
```

## 当前边界

- 默认哈希向量保证离线可运行，但语义质量有限；启用可选的 SentenceTransformers 后，应使用人工标注集比较 `expected_source_recall`、页码覆盖率和人工相关性。
- `pypdf` 仅能解析含文字层 PDF；扫描件需后续接入 OCR，不能被当作“已解析”。
- 当前不输出交易指令、目标价或收益承诺；所有结论都应回到原始公告、年报或新闻链接复核。

## Apple 2025 Form 10-K 检索评测基线

评测集位于 `data\apple_10k_eval_set.json`，共 **20** 条人工标注问题，覆盖服务业务收入/增长/毛利率、研发费用、总销售额、净利润、经营现金流、资本开支、股份回购、现金余额、递延收入、地域分部、中美销售额、iPhone 收入、固定资产、长期债务、实际税率和收入确认。每条问题都标注了应命中的 PDF 页码和应出现的关键证据词。

评测在 **2026-09-07** 对同一份 `Apple_2025_Form_10-K.pdf` 单独建库运行，Top-K 为 4；没有混入新闻或 Markdown 文档。`semantic` 模式使用默认 Hugging Face 缓存中的 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，以 `local_files_only=True` 离线加载；未设置 `HF_HOME` 或 `HF_ENDPOINT`，不会触发下载。

| 指标 | Hash | Semantic |
|---|---:|---:|
| 页面 Recall@4 | 0.20 | **0.60** |
| 关键词核验 Recall@4 | 0.20 | **0.30** |
| 引用页面相关性@4 | 0.05 | **0.175** |
| Top-1 页面相关性 | 0.00 | **0.30** |

指标定义：

- **页面 Recall@4**：Top-4 中至少有一个引用页命中人工标注目标页的比例。
- **关键词核验 Recall@4**：命中目标页，且该页被召回的文本块同时包含该题预标注关键证据词的比例。此指标能发现“页码碰巧命中、内容却不支持问题”的情况。
- **引用页面相关性@4**：每题 Top-4 中相关目标页所占比例的平均值。
- **Top-1 页面相关性**：第一条引用页属于人工目标页的比例。

结论：semantic 在四项指标上均优于 hash，但绝对值仍不够高，特别是关键词核验 Recall@4 仅为 0.30。这是当前项目明确暴露的检索质量缺陷，不能把“引用格式正确”当成“证据内容相关”。对比结果和每题召回页保存在：

- `data\evaluations\apple_10k_retrieval_comparison.json`
- `data\evaluations\apple_10k_retrieval_comparison.md`

运行命令：

```powershell
python -m investment_assistant.evaluation --top-k 4
```

该命令会清空并重建 `data\evaluation_chroma\hash` 与 `data\evaluation_chroma\semantic`，保证两种模式都只评测 Apple 10-K。若默认缓存中不存在语义模型，semantic 会返回明确错误并停止，不会偷偷降级后伪装成语义结果。

## Controlled LLM Slot-Template Acceptance (2026-09-08, Frozen)

The `auto` mode uses a slot-template control to prevent the model from inventing or mixing numeric facts. The model may only return a narrative framework and whitelisted `{slot}` placeholders. The program injects prices, dates, and amounts from structured market and financial snapshots.

The following controls remain mandatory:

- A template cannot contain bare numbers, dates, unknown slots, or unknown `[Sx]` citations.
- A template may use one or more question-relevant allowed slots, or a qualitative narrative with no numbers, but it must include valid citations.
- After rendering, every numeric token in the controlled narrative must come from a program-injected slot value or original evidence text.
- The total-revenue slot cannot be described as services revenue, and the free-cash-flow slot cannot be described as operating cash flow.
- One API retry is allowed for a connection failure; after that, the report explicitly falls back to the rule-based version.

The final run on **2026-09-08** used the frozen `semantic` retrieval configuration, the Apple 2025 Form 10-K, and the same five end-to-end cases:

| Case | Auto result | Safety |
|---|---|---:|
| services_revenue | Controlled LLM | Passed |
| r_and_d_expense | Controlled LLM | Passed |
| operating_cash_flow | Controlled LLM | Passed |
| china_sales | Controlled LLM | Passed |
| effective_tax_rate | Controlled LLM | Passed |

The controlled LLM success rate is **5/5**, meeting the acceptance threshold of **3/5**. Therefore, the **LLM version is accepted**. `auto` may emit a controlled LLM narrative only after slot, citation, numeric-provenance, and full-report safety checks pass. Any failed check must still show an explicit rule-based fallback. The rule-based report remains the stable fallback and is not removed.

This feature is now frozen: no more prompt, slot, retrieval, or reranking changes will be made. Known presentation boundary: when RAG evidence does not directly cover a question, the controlled LLM may only state that evidence is insufficient and recommend verification. The frontend should prominently display the data snapshot, original evidence, and the `Controlled LLM` or `Rule-based fallback` mode label, rather than presenting the narrative as a complete factual answer by itself.

Final evaluation artifacts:

- `data\reports\llm_rule_comparison_20260908_110530\summary.md`
- `data\reports\llm_rule_comparison_20260908_110530\summary.json`

The project now moves to frontend development, centered on real-data snapshots, traceable evidence, risk disclosures, report mode, and audit results.

## Web Demo

The frozen research, retrieval, LLM, and safety modules are exposed through a minimal FastAPI and Streamlit demo. The web layer only calls `run_research` and reads saved report artifacts; it does not duplicate or alter research logic.

On Windows, double-click `start_demo.bat`. It starts the local API and opens `http://127.0.0.1:8501` in a browser. In the page, enter a ticker, topic, and horizon, then click the generate button. The page shows the full report, mode and fallback reason, market and financial snapshots, evidence file/page/link details, risk flags, and saved historical reports.

For manual startup:

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn investment_assistant.api:app --host 127.0.0.1 --port 8000
python -m streamlit run investment_assistant/web_app.py
```

## Golden Sample and Offline Regression

The approved golden sample is stored in `data/golden/` and was copied from the post-fix AAPL report for topic `?????????????`. It is an offline baseline: no market, network, embedding, or LLM call is needed to validate it.

Run:

```powershell
python -m investment_assistant.golden_regression
```

The regression verifies report safety, controlled-LLM mode, section 2 label `??????`, structured-snapshot source attribution, RAG citation attribution, and availability of the five required financial snapshot fields. The complete accepted-boundary register is maintained in `KNOWN_ISSUES.md`.
