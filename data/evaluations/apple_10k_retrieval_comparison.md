# Apple 2025 Form 10-K 检索模式对比

| 指标 | Hash | Semantic |
|---|---:|---:|
| 页面 Recall@4 | 0.2 | 0.6 |
| 关键词核验 Recall@4 | 0.2 | 0.3 |
| 引用页面相关性@4 | 0.05 | 0.175 |
| Top-1 页面相关性 | 0.0 | 0.3 |

## 失败样本（页面未命中或关键词未核验）

### Hash
- services_revenue：目标页 [26, 39]，召回页 [77, 61, 34, 51]。
- services_growth：目标页 [26]，召回页 [22, 39, 61, 51]。
- services_margin：目标页 [27]，召回页 [65, 68, 64, 43]。
- r_and_d_expense：目标页 [32]，召回页 [77, 61, 51, 34]。
- net_income：目标页 [32, 35, 36, 39]，召回页 [77, 61, 51, 34]。
- operating_cash_flow：目标页 [36]，召回页 [77, 61, 34, 51]。
- capex：目标页 [36]，召回页 [77, 61, 34, 51]。
- share_repurchase：目标页 [29, 35, 36]，召回页 [77, 61, 34, 51]。
- cash_balance：目标页 [34, 36, 40]，召回页 [77, 50, 43, 59]。
- deferred_revenue：目标页 [34, 39]，召回页 [77, 59, 43, 50]。
- geographic_segments：目标页 [50]，召回页 [76, 5, 77, 4]。
- ppe_balance：目标页 [34, 42]，召回页 [77, 50, 43, 59]。
- term_debt：目标页 [47]，召回页 [77, 50, 43, 59]。
- effective_tax_rate：目标页 [28, 44]，召回页 [77, 61, 24, 34]。
- services_revenue_recognition：目标页 [38]，召回页 [76, 5, 77, 80]。
- services_gross_profit：目标页 [27]，召回页 [77, 61, 34, 51]。

### Semantic
- services_revenue：目标页 [26, 39]，召回页 [32, 50, 33, 48]。
- services_growth：目标页 [26]，召回页 [27, 46, 28, 44]。
- services_margin：目标页 [27]，召回页 [27, 46, 38, 18]。
- net_income：目标页 [32, 35, 36, 39]，召回页 [33, 50, 32, 5]。
- operating_cash_flow：目标页 [36]，召回页 [36, 34, 33, 50]。
- capex：目标页 [36]，召回页 [34, 36, 33, 35]。
- share_repurchase：目标页 [29, 35, 36]，召回页 [34, 35, 32, 62]。
- deferred_revenue：目标页 [34, 39]，召回页 [36, 50, 33, 34]。
- geographic_segments：目标页 [50]，召回页 [20, 76, 52, 5]。
- us_sales：目标页 [51]，召回页 [32, 34, 33, 5]。
- term_debt：目标页 [47]，召回页 [34, 36, 59, 50]。
- effective_tax_rate：目标页 [28, 44]，召回页 [33, 52, 24, 50]。
- services_revenue_recognition：目标页 [38]，召回页 [32, 36, 33, 52]。
- services_gross_profit：目标页 [27]，召回页 [32, 50, 48, 33]。
