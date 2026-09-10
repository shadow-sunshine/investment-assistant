# Retrieval optimization staged evaluation

Evaluation date: 2026-09-09. The main evaluation set remains frozen at 20 questions, original target pages, original keywords, and Top-K=4.

## Main 20-question results

| Stage | Mode | Page Recall@4 | Keyword-verified Recall@4 | Top-1 relevance | Reranker |
|---|---|---:|---:|---:|---|
| Frozen prior baseline | hash | 0.20 | 0.20 | 0.00 | disabled |
| Frozen prior baseline | semantic | 0.60 | 0.30 | 0.30 | disabled |
| Stage 1: term map + same-page chunk display | hash | 0.95 | 0.80 | 0.40 | disabled |
| Stage 1: term map + same-page chunk display | semantic | 0.80 | 0.55 | 0.45 | disabled |
| Stage 2: Cross-Encoder requested | hash | 0.95 | 0.80 | 0.40 | hybrid_fallback |
| Stage 2: Cross-Encoder requested | semantic | 0.80 | 0.55 | 0.45 | hybrid_fallback |

## Stage 1 versus frozen baseline: newly keyword-verified cases

- hash: capex, cash_balance, deferred_revenue, net_income, ppe_balance, r_and_d_expense, services_gross_profit, services_growth, services_margin, services_revenue, share_repurchase, term_debt
- semantic: deferred_revenue, net_income, services_revenue, share_repurchase, term_debt

## Cross-Encoder result

The requested local model was `BAAI/bge-reranker-v2-m3`. It is absent from the default Hugging Face cache. Loading used `local_files_only=True`; no download was attempted. Both hash and semantic runs recorded `reranker_mode=hybrid_fallback` with an explicit fallback reason. The fallback preserves Stage 1 candidate selection and metrics; it is not presented as a successful Cross-Encoder rerank.

## Five-question disjoint holdout

| Mode | Page Recall@4 | Keyword-verified Recall@4 | Top-1 relevance | Reranker |
|---|---:|---:|---:|---|
| hash | 0.80 | 0.40 | 0.60 | hybrid_fallback |
| semantic | 1.00 | 0.80 | 0.60 | hybrid_fallback |

Holdout IDs: `total_gross_margin`, `sga_expense`, `accounts_payable`, `inventory_balance`, `dividends_declared`.

## Artifacts

- `data/evaluations/apple_10k_retrieval_comparison.json`
- `data/evaluations/apple_10k_stage1_termmap_samepage.json`
- `data/evaluations/apple_10k_stage2_cross_encoder.json`
- `data/evaluations/apple_10k_holdout_stage2_cross_encoder.json`
- `data/financial_term_map.json`
