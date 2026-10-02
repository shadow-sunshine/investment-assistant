# Hybrid hash 双语检索隔离对照（2026-09-28）

**状态：离线实验完成；不切线上。** 代码与数据仅新增 `hybrid_retrieval.py`、`hybrid_eval.py`、`test_hybrid_retrieval.py` 和本目录 `hybrid_*` 结果。原有未提交工作树保持；未 reset/stash/clean/commit。完整逐题结果、冻结 SHA 和资料核验见 `hybrid_hash_v1.json`。

## 事先固定方案与对照口径

- 相同四份 PDF 原材料、一次性隔离 Chroma(hash) 索引、同一轮运行的 R0（32 题）和 R3 v3（32 题）；索引块 AAPL 406、MSFT 533、0700.HK 827、600519.SS 263。两臂均使用现有冻结 `financial_term_map.json` 的**默认中→英查询扩展**。不使用 gold keywords、目标页、答案值或 holdout 构造检索词表。
- `baseline_default` 直接调用 `LocalResearchRAG.search(question, limit=4, ticker=...)`。它**相当于历史 R3 的 treatment（扩展 query）口径，不是历史 R3 的 raw-query baseline**。本次 R3 baseline_default Page Recall@4=0.0938，与历史 treatment=0.0938 相符；历史 raw baseline=0.0625，不能把两者混为一谈。
- `hybrid` 的向量分支使用同一扩展 query 和原 hash 向量的前 48 块；字面分支仅扫描同 ticker 的现有块，以中文 2/3 字符 ngram、英文单词/数字和冻结扩展词的 BM25 打分，各取前 48 **页**。以固定 k=60 的 reciprocal-rank 融合；按 `file_name/source_id/page` 去重，展示块由查询字面锚点选取。规则在首次正式评测前写定，评测后未修改词表、权重或排序。不存在语义模型/cross-encoder 静默 fallback。
- Candidate Recall@48 是诊断量：baseline 检查前 48 个向量**块**（通常不足 48 个独立页），hybrid 检查融合排序前 48 个**页**（来自 48 向量块 + 48 字面页）。**两者候选预算不同，不能把增益完全归因于 ngram 或等价算力优势**。页级 Top-4 指标是本轮主要对照。`number_check_at_k` 是 gold 中带数字关键词在命中页展示块出现的比例（32/32 有数值关键词），不是数值推理准确率。

## 冻结与阻断

建索引之前逐字节校验 R0 题集、R3 v3 题集、冻结术语表及两份历史结果 JSON 的固定 SHA；复验两套题集的资料文件名/SHA 相符，四份 PDF 的实际 SHA 与两份历史报告的资料快照一致。任一失败返回 `BLOCKED`、`results={}`；测试覆盖题集/结果 SHA 漂移与资料漂移。实验不读取或清理 `data/chroma` / 既有 `r3_chroma_tmp`，仅使用自己创建的一次性临时目录，按既有 R3 Windows Chroma 句柄释放/重试清理策略处置。结果 JSON 记录全部实际 SHA 和逐 ticker 校验状态。

## 总体结果（baseline_default → hybrid；每组 32 题）

| 题集 | Page Recall@4 | Top-1 | keyword check@4 | number check@4 | Candidate Recall@48* | scoped ticker 污染 | 检索耗时，秒** |
|---|---:|---:|---:|---:|---:|---:|---:|
| R0 | 0.0938 → 0.2812 | 0.0625 → 0.1875 | 0.0938 → 0.2500 | 0.0938 → 0.2500 | 0.5312 → 0.7812 | 0 → 0 | 0.6434 → 0.8331 |
| R3 v3 | 0.0938 → 0.1562 | 0 → 0.0312 | 0.0938 → 0.1562 | 0.0938 → 0.1562 | 0.3125 → 0.6875 | 0 → 0 | 0.6330 → 0.5791 |

\* 非等预算，见口径说明。\*\* 每题计时之和，baseline 含额外候选诊断 query，hybrid 含首次 ticker 字面缓存构建；不含 PDF 索引/加载。样本量、缓存顺序和计时范围均不足以判断生产时延收益。ticker 污染仅指 **scoped 返回**，未进行 unscoped 臂，不能推断开放查询的污染风险。

| 题集 / 象限（各 8 题） | Page Recall@4 | Candidate Recall@48* | Top-1 | keyword/number check@4 |
|---|---:|---:|---:|---:|
| R0 zh-zh | 0 → 0.625 | 0.625 → 1.000 | 0 → 0.500 | 0 → 0.500 |
| R0 en-en | 0.375 → 0.500 | 0.750 → 1.000 | 0.250 → 0.250 | 0.375 → 0.500 |
| R0 zh-en | 0 → 0 | 0.125 → 0.250 | 0 → 0 | 0 → 0 |
| R0 en-zh | 0 → 0 | 0.625 → 0.875 | 0 → 0 | 0 → 0 |
| R3 zh-zh | 0 → 0 | 0 → 1.000 | 0 → 0 | 0 → 0 |
| R3 en-en | 0.250 → 0.500 | 0.625 → 1.000 | 0 → 0.125 | 0.250 → 0.500 |
| R3 zh-en | 0.125 → 0.125 | 0.250 → 0.375 | 0 → 0 | 0.125 → 0.125 |
| R3 en-zh | 0 → 0 | 0.375 → 0.375 | 0 → 0 | 0 → 0 |

R3 zh-zh 的 8/8 目标页已进入融合候选前 48，但 0/8 进入最终前 4：**召回改进不等于可用引用排序改进**；不根据该结果反复调整权重。en-zh 的 Page Recall@4 仍为 0；本轮没有实现英→中反向规范化，也不宣称全面双语有效。R0 与 R3 v3 非同一题集；R3 更可用于独立留出观察，均仅为四份静态 PDF 的小样本。

## 执行与异常

原始命令（项目根目录，`.venv`）：

```powershell
.\.venv\Scripts\python.exe -m compileall -q investment_assistant/hybrid_retrieval.py investment_assistant/hybrid_eval.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_hybrid_retrieval.py tests/test_bilingual_eval.py tests/test_r3_real_chroma_eval.py
Get-FileHash data/evaluations/bilingual_r0.json,data/evaluations/r3_real_chroma_v3.json
.\.venv\Scripts\python.exe -c "import logging; logging.getLogger('pypdf').setLevel(logging.ERROR); from investment_assistant.hybrid_eval import main; raise SystemExit(main())"
git diff --check
```

定向测试 `43 passed, 1 warning`（Starlette/anyio 弃用警告，269.20s），最终离线评测退出码 0。第一次未屏蔽 PDF 日志的运行出现大量 pypdf `fontTools` 缺失/CFF 字体告警；没有安装依赖，最终运行仅抑制日志，不改变索引抽取方法。`git diff --check` 无空白错误，但已有工作树文件有 LF→CRLF 提示。无 semantic 评测、无线上 A/B；上线前须以新鲜独立样本、排序/数值核验与污染/时延门槛重新验证。

## 独立验收补充（2026-09-28）

审阅者在 coder agent 交付后复跑了隔离评测，四臂 Page Recall@4 / Top-1 与上表一致；逐题核对 R0 净增 6 题且无丢失，R3 v3 新增 3 题但丢失 `enen_aapl_total_net_sales` 一题（净增 2 题），不能只报净收益。R3 的中→中仍是最终 0/8，虽候选 8/8，下一瓶颈是进入 Top-4 的排序，而不是宣称中文问题已可交付。

独立攻击测试先证明：若底层集合错误地返回其他 ticker 的元数据，原实验包装会把它混入候选。已在本实验包装层增加双重后置核验（`get` 和向量 `query` 的元数据归属与数组长度），异常时拒绝返回部分结果，补两条回归测试。还将逐题 JSON 的污染字段从错误的 `unscoped` 命名改为 `scoped`；本实验没有运行 unscoped 臂。将冻结题集或历史结果的**原始字节**各篡改一次、保留旧期望指纹，均在索引创建前得到 `BLOCKED` 和空结果。默认 `rag.py` 与工作流没有改动。

**验收结论：接受隔离实验，不接受线上切流。** 后续应先在新的预先冻结样本上检验页级 Top-4 排序、英→中、数值片段与回归损失，再考虑默认检索改动；R0/R3 已用于本轮观察，不能再冒充全新留出集调参。

独立修正后最终全量回归：`.\.venv\Scripts\python.exe -m pytest -q`，**330 passed, 1 warning**（290.07s）；隔离评测 `python -m investment_assistant.hybrid_eval` 退出码 0。告警为已存在的 Starlette/anyio 弃用项及评测时 pypdf 的可选 fontTools 字体解析提示，未据此声称 OCR/表格完整支持。
