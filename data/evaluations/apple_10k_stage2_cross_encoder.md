# Apple 2025 Form 10-K retrieval comparison

| Metric | Hash | Semantic |
|---|---:|---:|
| Page Recall@4 | 0.95 | 0.8 |
| Keyword-verified Recall@4 | 0.8 | 0.55 |
| Citation page relevance@4 | 0.3375 | 0.3125 |
| Top-1 page relevance | 0.4 | 0.45 |

## Retrieval configuration

- Hash: embedding=hash; reranker requested=cross_encoder; reranker actual=hybrid_fallback; reranker fallback=Cross-Encoder ????????????????????????????????????????OSError: We couldn't connect to 'https://huggingface.co' to load the files, and couldn't find them in the cached files.
Check your internet connection or see how to run the library in offline mode at 'https://huggingface.co/docs/transformers/installation#offline-mode'..
- Semantic: embedding=semantic; reranker requested=cross_encoder; reranker actual=hybrid_fallback; reranker fallback=Cross-Encoder ????????????????????????????????????????OSError: We couldn't connect to 'https://huggingface.co' to load the files, and couldn't find them in the cached files.
Check your internet connection or see how to run the library in offline mode at 'https://huggingface.co/docs/transformers/installation#offline-mode'..

## Failures (page miss or keyword verification miss)

### Hash
- operating_cash_flow: targets [36]; retrieved [36, 41, 27, 28].
- geographic_segments: targets [50]; retrieved [5, 50, 20, 39].
- effective_tax_rate: targets [28, 44]; retrieved [77, 61, 24, 34].
- services_revenue_recognition: targets [38]; retrieved [25, 39, 38, 26].

### Semantic
- services_growth: targets [26]; retrieved [27, 25, 13, 12].
- services_margin: targets [27]; retrieved [27, 18, 5, 10].
- operating_cash_flow: targets [36]; retrieved [36, 52, 41, 46].
- capex: targets [36]; retrieved [42, 34, 33, 28].
- geographic_segments: targets [50]; retrieved [20, 5, 50, 39].
- us_sales: targets [51]; retrieved [32, 34, 33, 5].
- effective_tax_rate: targets [28, 44]; retrieved [33, 52, 24, 50].
- services_revenue_recognition: targets [38]; retrieved [32, 26, 38, 25].
- services_gross_profit: targets [27]; retrieved [32, 10, 27, 52].
