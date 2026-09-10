# Apple 2025 Form 10-K retrieval comparison

| Metric | Hash | Semantic |
|---|---:|---:|
| Page Recall@4 | 0.8 | 1.0 |
| Keyword-verified Recall@4 | 0.4 | 0.8 |
| Citation page relevance@4 | 0.2 | 0.3 |
| Top-1 page relevance | 0.6 | 0.6 |

## Retrieval configuration

- Hash: embedding=hash; reranker requested=cross_encoder; reranker actual=hybrid_fallback; reranker fallback=Cross-Encoder 重排器不可用，已显式降级为既有混合重排（仅允许默认缓存离线加载，不会下载模型）：OSError: We couldn't connect to 'https://huggingface.co' to load the files, and couldn't find them in the cached files.
Check your internet connection or see how to run the library in offline mode at 'https://huggingface.co/docs/transformers/installation#offline-mode'..
- Semantic: embedding=semantic; reranker requested=cross_encoder; reranker actual=hybrid_fallback; reranker fallback=Cross-Encoder 重排器不可用，已显式降级为既有混合重排（仅允许默认缓存离线加载，不会下载模型）：OSError: We couldn't connect to 'https://huggingface.co' to load the files, and couldn't find them in the cached files.
Check your internet connection or see how to run the library in offline mode at 'https://huggingface.co/docs/transformers/installation#offline-mode'..

## Failures (page miss or keyword verification miss)

### Hash
- total_gross_margin: targets [27]; retrieved [27, 32, 42, 51].
- inventory_balance: targets [34]; retrieved [36, 77, 50, 43].
- dividends_declared: targets [35]; retrieved [35, 29, 36, 4].

### Semantic
- total_gross_margin: targets [27]; retrieved [32, 33, 27, 34].
