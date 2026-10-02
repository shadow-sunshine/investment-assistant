> **DEPRECATED / exploratory_blocked_non_independent**：v2 holdout 内部复用了同一事实的双语改写；请勿用于独立泛化、线上 A/B 或简历量化。新结果见 `r3_real_chroma_v3.md`。

# R3 独立留出集 · 真实 Chroma(hash) 召回对照

- 评测时间：2026-09-26T15:28:17.183798+00:00
- holdout：`r3_holdout_eval_set_v2.json`（SHA `8c0e3f174930…`，与冻结基线 一致；共 32 条，四象限各 8 条）
- 术语扩展策略：`data/financial_term_map.json`（SHA `66a0ce85220d…`，与冻结策略 一致）
- 实际 embedding 模式：`hash`；reranker 模式：`disabled`
- Top-K 冻结为 4；两臂 = baseline(原始问题) vs treatment(问题+冻结词面扩展)；唯一变量为冻结术语扩展，检索不读取任何 gold 标签
- 含数值/年份的查询：32/32（数值锚点仅来自问题文本，绝不取答案值）
- 索引块数：{'AAPL': 406, 'MSFT': 533, '0700.HK': 827, '600519.SS': 263}

## 1. 总体四象限指标（Page Recall@4 / 关键词-数值核验 Recall@4 / Top-1）

| 指标 | baseline | treatment |
|---|---:|---:|
| Page Recall@4 | 0.0938 | 0.125 |
| 关键词/数值核验 Recall@4 | 0.0625 | 0.0938 |
| Top-1 页面相关性 | 0.0 | 0.0312 |
| 引用页面相关率@4 | 0.0234 | 0.0312 |
| 跨标的来源/条（unscoped） | 2.0 | 2.125 |

## 2. 按象限拆分（Page Recall@4 / 核验 Recall@4 / Top-1）

### 中→中（中文问 / 中文资料）
| 指标 | baseline | treatment |
|---|---:|---:|
| Page Recall@4 | 0.0 | 0.0 |
| 核验 Recall@4 | 0.0 | 0.0 |
| Top-1 | 0.0 | 0.0 |
| 跨标的来源/条 | 0.125 | 0.625 |

### 英→英（英文问 / 英文资料）
| 指标 | baseline | treatment |
|---|---:|---:|
| Page Recall@4 | 0.25 | 0.25 |
| 核验 Recall@4 | 0.25 | 0.25 |
| Top-1 | 0.0 | 0.0 |
| 跨标的来源/条 | 1.125 | 1.125 |

### 中→英（中文问 / 英文资料）
| 指标 | baseline | treatment |
|---|---:|---:|
| Page Recall@4 | 0.125 | 0.25 |
| 核验 Recall@4 | 0.0 | 0.125 |
| Top-1 | 0.0 | 0.125 |
| 跨标的来源/条 | 2.75 | 2.75 |

### 英→中（英文问 / 中文资料）
| 指标 | baseline | treatment |
|---|---:|---:|
| Page Recall@4 | 0.0 | 0.0 |
| 核验 Recall@4 | 0.0 | 0.0 |
| Top-1 | 0.0 | 0.0 |
| 跨标的来源/条 | 4.0 | 4.0 |


## 3. 失败归因分布

| 归因 | baseline | treatment |
|---|---:|---:|
| 命中（无失败） | 0 | 1 |
| 跨语言未召回 | 15 | 14 |
| 同语言候选未召回 | 14 | 14 |
| 页命中但数值不在块内 | 1 | 1 |
| 已召回但排序未进 Top-1 | 2 | 2 |

## 4. 跨标的污染（unscoped 臂）

| 出现跨标的来源的样本数（共 32 条） | 23 | 24 |
| Top-K 全来自错误标的的样本数（共 32 条） | 11 | 12 |

## 5. 耗时（秒，索引 + 两臂检索）

- baseline: 0.6155
- treatment: 0.6902

## 6. 适用范围、未验证项与是否进入线上 A/B

- **适用范围**：本实验在真实 Chroma(hash) 索引上，用冻结 query-only 词面扩展对照 baseline，量化扩展对四象限「金标准页进入 Top-4/Top-1」的增量；gold page 先于检索固化，检索不读取任何 gold 标签。
- **与 R0 独立性验收**：level1(ticker,question) 重复 0 条，level2(ticker,page,keyword) 重复 0 条 —— 均为 0，独立留出集验收通过；历史 v1（r3_real_chroma.json/.md）已标记为 `exploratory_blocked_non_independent`（独立性验收失败），本 v2 不覆盖其为新结论。
- **未验证项**：① semantic / cross-encoder 需本地模型缓存，本次不作为独立实验臂（不静默 fallback）；② 仅 32 条小样本，不做统计显著性外推；③ 仅覆盖四份冻结 PDF，未含新闻/网页等动态语料。
- **剩余风险**：pypdf 对 CFF 字体 PDF 抽取存在已知噪音（fontTools 缺失告警），可能影响个别英文页字面分；中文页抽取经验证完整。冻结术语扩展为单向（中→英），en-zh 象限无对应扩展方向，属已知结构限制。
- **是否进入线上 A/B**：见第 7 节结论。

## 7. 结论（R2 → R3 对照）

- **线上 A/B 决策：`no_online_ab`** —— treatment 相对 baseline 的 Page Recall@4 增益为 0.0312（无明确正向增益），且跨标的 Top-4 全错污染 baseline=11/treatment=12；不满足「有明确增益且污染不恶化」的上线条件，结论为不进入线上 A/B。
- 若 treatment 仅在 zh-en 象限相对 baseline 提升、且其它象限两臂持平（en-en/en-zh 无扩展方向、zh-zh 同语言已工作），则增益被**限定在中文→英文扩展方向**，不能直接外推为全局线上改动；
- 若任一象限出现 treatment < baseline（扩展引入噪音），应保守，不进入线上；
- 任何结论均不构成直接修改 `rag.py` 的依据；线上改动须另立切流计划与更大样本验证。
