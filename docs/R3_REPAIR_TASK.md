# R3 修复任务：真正独立留出集 + 安全临时 Chroma

> 状态：BLOCKED，必须修复后才能宣称 R3 通过或进入线上 A/B。
> 执行方式：WorkBuddy 一次性完成本文件全部要求；完成后停止，不自动进入 R4 或修改线上默认检索。
> 入口：本文件是对 R3 当前工作树的修复任务，不是新实验方向。

## 1. 已确认的问题（不可争辩）

1. 当前 `data/r3_holdout_eval_set.json` 的 32 条样本中，有 **10/32** 条与 R0 `data/bilingual_eval_set.json` 在 `(ticker, question)` 上重复；10 条 gold keywords 全部相同，9 条 evidence snippet 相同。
2. 因此当前 R3 只能作为探索性结果，不能被描述为“独立留出集泛化验证”，不能据此进入线上 A/B，不能作为简历量化成果。
3. `investment_assistant/r3_real_chroma_eval.py` 当前在 `:242-243` 对传入的 `temp_chroma_dir` 直接执行 `shutil.rmtree()`；固定目录或含用户文件的目录不能被评测自动递归删除。
4. 当前报告对“是否进入线上 A/B”只给条件式规则，没有对这次实际结果做明确决策；而本次 treatment 只多命中 1/32，同时跨标的污染恶化（21/32→23/32，Top-4 全错标的 11/32→14/32）。

## 2. 必须做的修复

### A. 重建真正独立的 R3 holdout

- 不要只删除重复的 10 条后继续使用旧指标；样本集变了，必须重新逐页标注并从头跑两臂。
- 保持四象限各 8 条、总计 32 条（若确实无法保持，必须在报告中明确说明，不能静默改变样本数）。
- 新增的 10 条替换样本必须来自 R0 未使用的事实/页面，不能只是同一事实的英文改写或中文改写；优先选择同一资料中不同指标、不同目标页，或已冻结资料中的未使用事实。
- 每条仍需包含：`id`、`quadrant`、`question_lang`、`doc_lang`、`ticker`、`question`、`target_pages`、`keywords`、`evidence_snippet`、`forbidden_tickers`。
- 先完成 gold page 和 evidence 的逐页核验，保存新样本；再计算并冻结 holdout SHA；最后才能运行检索。
- 新增独立性校验脚本/测试，至少做到：
  - 与 R0 按规范化 `(ticker, question)` 比对，重复即 fail；
  - 与 R0 按 `(ticker, target_page, normalized keywords)` 比对，复用同一事实即 fail 或显式人工豁免并记录理由；
  - 样本数量、四象限数量、资料 SHA、holdout SHA 均在建索引前校验；
  - 资料缺失或 SHA 漂移时 fail-closed，不运行检索、不产出可比指标。
- 新 holdout 必须有新的冻结常量；旧 `84d0...` 只能作为历史 R3 结果的证据，不能继续当新实验基线。
- 保留旧的 `r3_real_chroma.json/md`，将其明确标记为 `exploratory_blocked_non_independent`，不得覆盖为新结论。

### B. 修复临时 Chroma 目录生命周期

- 禁止对用户传入路径无条件 `shutil.rmtree()`。
- 默认运行必须使用每次唯一的临时目录（建议 `tempfile.TemporaryDirectory` 或 `tempfile.mkdtemp`），运行结束自动清理；不要把持久化索引作为默认交付物留在 `data/evaluations/r3_chroma_tmp/`。
- 若保留 `temp_chroma_dir` 参数供测试，必须：
  - 路径解析后严格限制在项目 `data/evaluations/` 下的专用 R3 前缀目录；
  - 目录已存在且非空时拒绝运行，而不是删除；
  - 测试用哨兵文件证明不会被删除；
  - 禁止任何路径穿越、项目根目录、`data/chroma` 或其他共享索引目录作为清理目标。
- 测试必须覆盖：唯一临时目录、正常清理、已有非空目录 fail-closed、默认共享 Chroma 不受影响。

### C. 重新运行真实 Chroma 对照

- 保持唯一变量：baseline = 原始 question；treatment = question + 已冻结 query-only 扩展。
- 不能修改 `rag.py`、`workflow.py`、`llm_generation.py`、`safety.py` 或线上默认检索路径。
- 使用真实 Chroma，记录实际 `embedding_mode`、`reranker_mode` 和降级原因；不得把 hash 结果写成 semantic 结果。
- 生成新的机器可读结果和 Markdown 报告，推荐新文件名：
  - `data/evaluations/r3_real_chroma_v2.json`
  - `data/evaluations/r3_real_chroma_v2.md`
- 报告必须明确写出：
  - `status=ok` 仅表示实验链路完成，不表示策略有效；
  - 新 holdout 与 R0 无重复且已冻结；
  - 本次是否允许线上 A/B。若仍没有明确、可接受的增益且污染不恶化，结论必须是“不进入线上 A/B”。
- 对当前旧结果明确写：**仅探索性、独立性验收失败，不可用于泛化或线上决策**。

## 3. 测试与验收（必须实际运行）

先运行针对性测试，再运行全量：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_r3_real_chroma_eval.py -q
.\.venv\Scripts\python.exe -m pytest -q
 git diff --check
```

如新增测试文件，命令中必须包含。真实 Chroma 集成测试不得全部 mock；报告必须区分离线测试与真实语料 E2E。

最终汇报必须包含：

1. 改了什么 / 没改什么；
2. 新 holdout 与 R0 的独立性校验结果（重复数必须为 0）；
3. 新 holdout、资料、策略 SHA；
4. 临时目录安全测试结果；
5. 实际运行命令与原始结果；
6. R3 v2 指标、失败归因和是否进入线上 A/B；
7. 未验证项与剩余风险；
8. 完成后停止，不进入下一阶段。

## 4. 严格边界

- 不执行 `git add -A`、`commit`、`stash`、`reset`、`clean`。
- 不清理、覆盖、回滚用户已有未提交修改或未跟踪文件。
- 不删除 `data/evaluations/r3_chroma_tmp/`；如需处理旧残留，只能在用户明确授权后单独提出。
- 不把“测试全绿”当成独立性验收通过；必须报告样本重叠检查的实际数字。
