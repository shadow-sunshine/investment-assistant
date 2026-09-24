# R0 四象限双语检索评测（实验基线）

- 评测时间：2026-09-24T16:16:49.599284+00:00
- 样本：`data/bilingual_eval_set.json`（32 条，gold page 由资料原文关键词共现 + 逐页人工核对确定，先于检索结果固化）
- Top-K 冻结为 4，与既有 Apple 20 条基线保持一致
- 本报告为小样本实验基线，**不外推为业务准确率**

## 1. 资料快照校验

| 标的 | 资料 | sha256 校验 | 页码权威性 |
|---|---|---|---|
| AAPL | Apple_2025_Form_10-K.pdf | match | official |
| MSFT | MSFT_SEC_10-K_2026-06-30_official-html.pdf | match | generated |
| 600519.SS | 600519_2025_annual_report.pdf | match | official |
| 0700.HK | 0700HK_annual_report_2025.pdf | match | official |

> MSFT 的页码是本地 HTML→PDF 转换页码（`page_authority=generated`），不是 SEC 官方分页；其首页含 XBRL 标签噪音。

## 2. 总体指标（按 ticker 限定检索，对齐线上行为）

| 指标 | Hash | Semantic |
|---|---:|---:|
| Page Recall@4 | 0.0938 | 0.25 |
| 关键词/数值核验 Recall@4 | 0.0938 | 0.2188 |
| Top-1 页面相关性 | 0.0625 | 0.0625 |
| 引用页面相关率@4 | 0.0312 | 0.0703 |
| 跨标的来源/条（不限定 ticker 对照） | 2.0625 | 1.4688 |

## 3. 按象限拆分（Page Recall@4 / 关键词核验 Recall@4 / Top-1）

### hash
| 象限 | 样本数 | Page Recall@4 | 关键词核验 Recall@4 | Top-1 | 跨标的来源/条 |
|---|---:|---:|---:|---:|---:|
| 中→中（中文问 / 中文资料） | 8 | 0.0 | 0.0 | 0.0 | 1.25 |
| 英→英（英文问 / 英文资料） | 8 | 0.375 | 0.375 | 0.25 | 0.75 |
| 中→英（中文问 / 英文资料） | 8 | 0.0 | 0.0 | 0.0 | 2.25 |
| 英→中（英文问 / 中文资料） | 8 | 0.0 | 0.0 | 0.0 | 4.0 |

### semantic
| 象限 | 样本数 | Page Recall@4 | 关键词核验 Recall@4 | Top-1 | 跨标的来源/条 |
|---|---:|---:|---:|---:|---:|
| 中→中（中文问 / 中文资料） | 8 | 0.0 | 0.0 | 0.0 | 0.625 |
| 英→英（英文问 / 英文资料） | 8 | 0.625 | 0.5 | 0.25 | 0.0 |
| 中→英（中文问 / 英文资料） | 8 | 0.25 | 0.25 | 0.0 | 1.75 |
| 英→中（英文问 / 中文资料） | 8 | 0.125 | 0.125 | 0.0 | 3.5 |

## 4. 失败归因分布

| 归因 | Hash | Semantic |
|---|---:|---:|
| 命中（无失败） | 2 | 2 |
| 跨语言未召回 | 16 | 13 |
| 同语言候选未召回 | 13 | 11 |
| 页命中但数值不在块内 | 0 | 1 |
| 已召回但排序未进 Top-1 | 1 | 5 |

跨标的污染是**独立于主因**的对照指标（不限定 ticker 那一臂）：

| 口径 | Hash | Semantic |
|---|---:|---:|
| 出现跨标的来源的样本数（共 32 条） | 22 | 17 |
| Top-4 全部来自错误标的的样本数（共 32 条） | 12 | 8 |


## 5. 失败样本明细

### hash（30 条未完全命中）
- `zhzh_moutai_revenue` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 61]；召回页 [1, 5, 125, 46]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_total_operating_revenue` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [61]；召回页 [1, 5, 125, 46]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_net_profit` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 62]；召回页 [1, 5, 30, 46]；不限定 ticker 时跨标的来源 4 条
- `zhzh_moutai_operating_cash_flow` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 115]；召回页 [1, 5, 31, 30]；不限定 ticker 时跨标的来源 4 条
- `zhzh_moutai_eps` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 63]；召回页 [1, 5, 125, 46]；不限定 ticker 时跨标的来源 1 条
- `zhzh_moutai_roe` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6]；召回页 [1, 5, 125, 46]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_selling_expense` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [9, 61]；召回页 [1, 5, 125, 46]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_inventory` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [14, 57]；召回页 [2, 122, 72, 30]；不限定 ticker 时跨标的来源 1 条
- `enen_aapl_total_assets` [en-en/AAPL] 归因=candidate_miss；目标页 [34]；召回页 [2, 22, 80, 42]；不限定 ticker 时跨标的来源 3 条
- `enen_msft_total_revenue` [en-en/MSFT] 归因=candidate_miss；目标页 [62]；召回页 [127, 52, 125, 74]；不限定 ticker 时跨标的来源 0 条
- `enen_msft_operating_income` [en-en/MSFT] 归因=candidate_miss；目标页 [62]；召回页 [125, 52, 55, 54]；不限定 ticker 时跨标的来源 0 条
- `enen_msft_gross_margin` [en-en/MSFT] 归因=candidate_miss；目标页 [62]；召回页 [51, 127, 53, 115]；不限定 ticker 时跨标的来源 2 条
- `enen_tencent_revenue` [en-en/0700.HK] 归因=candidate_miss；目标页 [4, 8]；召回页 [5, 9, 200, 196]；不限定 ticker 时跨标的来源 0 条
- `enen_tencent_gross_profit` [en-en/0700.HK] 归因=ranking；目标页 [4, 8]；召回页 [5, 10, 8, 234]；不限定 ticker 时跨标的来源 0 条
- `zhen_msft_net_income` [zh-en/MSFT] 归因=cross_language_miss；目标页 [62]；召回页 [77, 55, 66, 78]；不限定 ticker 时跨标的来源 0 条
- `zhen_msft_rnd_expense` [zh-en/MSFT] 归因=cross_language_miss；目标页 [62]；召回页 [53, 73, 54, 33]；不限定 ticker 时跨标的来源 1 条
- `zhen_msft_total_assets` [zh-en/MSFT] 归因=cross_language_miss；目标页 [64]；召回页 [136, 123, 94, 115]；不限定 ticker 时跨标的来源 4 条
- `zhen_msft_total_liabilities` [zh-en/MSFT] 归因=cross_language_miss；目标页 [65]；召回页 [136, 123, 94, 115]；不限定 ticker 时跨标的来源 4 条
- `zhen_msft_operating_cash_flow` [zh-en/MSFT] 归因=cross_language_miss；目标页 [67]；召回页 [57, 113, 66, 107]；不限定 ticker 时跨标的来源 2 条
- `zhen_tencent_profit_for_the_year` [zh-en/0700.HK] 归因=cross_language_miss；目标页 [4, 8]；召回页 [52, 57, 53, 49]；不限定 ticker 时跨标的来源 2 条
- `zhen_tencent_profit_before_tax` [zh-en/0700.HK] 归因=cross_language_miss；目标页 [4, 8]；召回页 [52, 57, 53, 49]；不限定 ticker 时跨标的来源 2 条
- `zhen_tencent_selling_expense` [zh-en/0700.HK] 归因=cross_language_miss；目标页 [8]；召回页 [52, 53, 57, 49]；不限定 ticker 时跨标的来源 3 条
- `enzh_moutai_total_assets` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [6]；召回页 [31, 30, 61, 53]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_equity` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [6]；召回页 [31, 30, 129, 33]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_cash` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [14, 56]；召回页 [31, 30, 35, 53]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_contract_liability` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [58]；召回页 [30, 31, 129, 63]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_rnd_expense` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [9, 61]；召回页 [33, 30, 31, 97]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_admin_expense` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [9, 61]；召回页 [24, 31, 30, 53]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_cost_of_sales` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [9, 61]；召回页 [30, 53, 63, 31]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_liquor_gross_margin` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [10]；召回页 [30, 31, 53, 63]；不限定 ticker 时跨标的来源 4 条

### semantic（30 条未完全命中）
- `zhzh_moutai_revenue` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 61]；召回页 [54, 1, 26, 12]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_total_operating_revenue` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [61]；召回页 [54, 1, 26, 12]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_net_profit` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 62]；召回页 [70, 69, 54, 107]；不限定 ticker 时跨标的来源 1 条
- `zhzh_moutai_operating_cash_flow` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 115]；召回页 [10, 116, 67, 66]；不限定 ticker 时跨标的来源 4 条
- `zhzh_moutai_eps` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6, 63]；召回页 [70, 54, 107, 69]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_roe` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [6]；召回页 [70, 69, 68, 71]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_selling_expense` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [9, 61]；召回页 [40, 1, 128, 70]；不限定 ticker 时跨标的来源 0 条
- `zhzh_moutai_inventory` [zh-zh/600519.SS] 归因=candidate_miss；目标页 [14, 57]；召回页 [20, 122, 70, 68]；不限定 ticker 时跨标的来源 0 条
- `enen_aapl_basic_eps` [en-en/AAPL] 归因=chunk_or_value_boundary；目标页 [39]；召回页 [32, 24, 62, 39]；不限定 ticker 时跨标的来源 0 条
- `enen_msft_total_revenue` [en-en/MSFT] 归因=candidate_miss；目标页 [62]；召回页 [127, 126, 112, 52]；不限定 ticker 时跨标的来源 0 条
- `enen_msft_operating_income` [en-en/MSFT] 归因=candidate_miss；目标页 [62]；召回页 [52, 125, 127, 49]；不限定 ticker 时跨标的来源 0 条
- `enen_msft_gross_margin` [en-en/MSFT] 归因=candidate_miss；目标页 [62]；召回页 [53, 49, 127, 45]；不限定 ticker 时跨标的来源 0 条
- `enen_tencent_revenue` [en-en/0700.HK] 归因=ranking；目标页 [4, 8]；召回页 [196, 272, 5, 8]；不限定 ticker 时跨标的来源 0 条
- `enen_tencent_gross_profit` [en-en/0700.HK] 归因=ranking；目标页 [4, 8]；召回页 [5, 130, 272, 4]；不限定 ticker 时跨标的来源 0 条
- `zhen_msft_net_income` [zh-en/MSFT] 归因=cross_language_miss；目标页 [62]；召回页 [78, 118, 54, 110]；不限定 ticker 时跨标的来源 3 条
- `zhen_msft_rnd_expense` [zh-en/MSFT] 归因=cross_language_miss；目标页 [62]；召回页 [54, 53, 73, 33]；不限定 ticker 时跨标的来源 1 条
- `zhen_msft_total_assets` [zh-en/MSFT] 归因=cross_language_miss；目标页 [64]；召回页 [136, 123, 118, 127]；不限定 ticker 时跨标的来源 2 条
- `zhen_msft_total_liabilities` [zh-en/MSFT] 归因=cross_language_miss；目标页 [65]；召回页 [127, 118, 48, 136]；不限定 ticker 时跨标的来源 2 条
- `zhen_msft_operating_cash_flow` [zh-en/MSFT] 归因=cross_language_miss；目标页 [67]；召回页 [57, 66, 60, 125]；不限定 ticker 时跨标的来源 2 条
- `zhen_tencent_profit_for_the_year` [zh-en/0700.HK] 归因=ranking；目标页 [4, 8]；召回页 [14, 12, 8, 16]；不限定 ticker 时跨标的来源 1 条
- `zhen_tencent_profit_before_tax` [zh-en/0700.HK] 归因=cross_language_miss；目标页 [4, 8]；召回页 [205, 11, 131, 237]；不限定 ticker 时跨标的来源 2 条
- `zhen_tencent_selling_expense` [zh-en/0700.HK] 归因=ranking；目标页 [8]；召回页 [12, 8, 16, 15]；不限定 ticker 时跨标的来源 1 条
- `enzh_moutai_total_assets` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [6]；召回页 [68, 70, 14, 69]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_equity` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [6]；召回页 [68, 69, 70, 47]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_cash` [en-zh/600519.SS] 归因=ranking；目标页 [14, 56]；召回页 [116, 114, 56, 85]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_contract_liability` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [58]；召回页 [68, 70, 138, 108]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_rnd_expense` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [9, 61]；召回页 [20, 13, 73, 106]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_admin_expense` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [9, 61]；召回页 [106, 14, 26, 108]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_cost_of_sales` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [9, 61]；召回页 [118, 12, 68, 106]；不限定 ticker 时跨标的来源 4 条
- `enzh_moutai_liquor_gross_margin` [en-zh/600519.SS] 归因=cross_language_miss；目标页 [10]；召回页 [16, 120, 1, 15]；不限定 ticker 时跨标的来源 0 条

## 6. 已知限制

- 四象限各 8 条，属小样本实验基线，不做统计显著性外推。
- 术语映射 `data/financial_term_map.json` 只覆盖 中文→英文，英→中/英→英 象限不触发扩展。
- 既有 Apple 25 条评测（含 5 条 holdout）全部是 中→英 单象限；holdout 的 gold keyword 与术语映射表存在 1:1 重合，其提升幅度不能直接作为泛化证据。
- pypdf 未安装 fontTools，CFF 字体编码解析受限，可能影响部分页面抽取质量，归为「原始资料」类风险。
- **语料规模与既有 Apple 基线不可直接横比**：Apple 基线只索引单份 80 页 PDF（AAPL 406 个块），R0 同时索引四份资料共 644 页（2029 个块）。
  语料扩大 5 倍而候选池仍为 48 条，是本次 Recall 显著低于 Apple 基线的已知混杂因素，不能解读为纯粹的能力退化。

## 7. 下一步最小改进假设（待 R1 验证）

1. **中文侧检索近乎失效**：中→中 象限 Hash / Semantic 均为 0/8，且不限定 ticker 时仍召不回中文页。
   假设：中文查询与中文表格文本之间缺少可用的字面锚点（术语 + 数值），现有向量与字面覆盖率都不够。
   最小验证：加一路**不依赖 embedding 的术语/数值锚点召回**，与向量候选融合，只测中→中 象限是否脱离 0。
2. **跨语言查询扩展只覆盖中→英**：`financial_term_map.json` 单向且条目与 Apple 历史 gold 关键词高度重合；
   在 MSFT / 0700.HK 这类新语料上，hash 下跨语言样本 16 条全部未召回。
   最小验证：把术语表拆成「通用财务术语」与「标的专属条目」两组，分别测泛化贡献。
3. **候选池 48 条对 2029 个块偏小**：先做候选池规模敏感性实验（48 / 100 / 200），确认是否为硬瓶颈，再决定是否改检索结构。

> 以上三条都是「先做最小实验证伪」级别的假设，不构成 R1 的实施方案；R1 需另立对照评测与切流计划。