# Apple 2025 Form 10-K retrieval comparison

| Metric | Hash | Semantic |
|---|---:|---:|
| Page Recall@4 | 0.8 | 1.0 |
| Keyword-verified Recall@4 | 0.4 | 0.8 |
| Citation page relevance@4 | 0.2 | 0.3 |
| Top-1 page relevance | 0.6 | 0.6 |

## Retrieval configuration

- Hash: embedding=hash; reranker requested=disabled; reranker actual=disabled; reranker fallback=none.
- Semantic: embedding=semantic; reranker requested=disabled; reranker actual=disabled; reranker fallback=none.

## Failures (page miss or keyword verification miss)

### Hash
- total_gross_margin: targets [27]; retrieved [27, 32, 42, 51].
- inventory_balance: targets [34]; retrieved [36, 77, 50, 43].
- dividends_declared: targets [35]; retrieved [35, 29, 36, 4].

### Semantic
- total_gross_margin: targets [27]; retrieved [32, 33, 27, 34].
