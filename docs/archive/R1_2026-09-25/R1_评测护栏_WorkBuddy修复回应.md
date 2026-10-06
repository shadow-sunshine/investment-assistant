# R1 评测护栏复审后定点修复 —— WorkBuddy 修复回应

> 日期：2026-09-25
> 关联：独立复审确认的三项问题 R1-F1 / R1-F2 / R1-F3（见 `docs/R1_评测护栏收尾任务卡.md`、`docs/CODEX_REVIEW_R1_guardrails.md`）
> 仓库根目录：`D:\It\Test_Project\Investment_Assistant`
> 范围：仅 `candidate_pool_eval.py` + 其测试 + 现有实验产物元数据迁移。未触碰冻结区（`rag/workflow/llm_generation/safety.py`），未执行任何 git 写操作，未重跑昂贵检索。

## 0. 开始前核查（按任务要求）

- 工作目录 / Git 根目录均为 `D:\It\Test_Project\Investment_Assistant`（`git rev-parse --show-toplevel` 一致）。
- 开始时的用户既有改动 / 未跟踪文件均视为已有内容，**未覆盖、未恢复、未清理**（详见 §7 git status）。
- 先阅读实际代码与测试，逐项核实复审意见是否仍存在（见 §1）。

## 1. 三项问题核实结论（先核实、只修仍未解决项）

| 问题 | 严重度 | 核查结果（代码证据） | 是否仍存在 | 处理 |
|---|---|---|---|---|
| R1-F1 | P1 | `candidate_pool_eval.py` `render_from_result()`（旧 L702-706）对 `status=="blocked"` 一律 `render_blocked_report(payload["material_verification"], …)`；而 `run_experiment()` 的 `eval_set_mismatch` BLOCKED 分支（旧 L608-625）**不含** `material_verification` 字段 → 复渲染会抛 `KeyError`。 | **是** | 已修 |
| R1-F2 | P2 | `render_from_result()` 正常结果路径（旧 L707-735）只比对 `stored_sha == current_sha`，未再校验是否等于**冻结 R0 基线 SHA**；一个"与当前一致但非冻结样本"的结果会被渲染成正常报告。 | **是** | 已修 |
| R1-F3 | P2 | 现有 `data/evaluations/candidate_pool_sensitivity.json` 的 `eval_set_sha256="8a367…acf5"` **等于** `bilingual_eval.FROZEN_R0_EVAL_SET_SHA256`，且 `matches_r0_eval_set: true`、资料全 `match`、`material_drift: []`，但缺 `frozen_r0_eval_set_sha256` / `matches_frozen_baseline`。属"可基于可验证证据安全补字段"的情形。 | **是（缺字段）** | 已安全迁移 |

## 2. 修复内容

### 2.1 R1-F1（BLOCKED 结果可安全重渲染）—— `investment_assistant/candidate_pool_eval.py`
- 新增两个报告函数：
  - `_render_blocked_unknown_report(reason, evaluated_at)`：未知 BLOCKED 原因或必要字段缺失时的 fail-closed 报告。
  - `_render_eval_set_not_frozen_report(current_sha, frozen_sha, evaluated_at)`：复渲染时结果非冻结基线的 fail-closed 报告（同时服务 F2）。
- 重写 `render_from_result()` 的 BLOCKED 分支，按 `blocked_reason` 分派：
  - `material_drift` → `render_blocked_report(payload.get("material_verification", {}), …)`（用 `.get` 兜底，缺字段不 KeyError）；
  - `eval_set_mismatch` → `_render_eval_set_mismatch_report(payload.get("eval_set_sha256",""), payload.get("frozen_r0_eval_set_sha256",""), …)`（不再依赖 `material_verification`）；
  - 其它 / 缺失 → `_render_blocked_unknown_report`（fail-closed，**不生成正常报告、不抛未处理异常**）。

### 2.2 R1-F2（复渲染再次校验冻结基线）—— `investment_assistant/candidate_pool_eval.py`
- `render_from_result()` 正常结果路径在校验 `stored_sha == current_sha` 之后，**新增**冻结基线校验：
  - `payload.get("matches_frozen_baseline") is False` → RENDER BLOCKED；
  - `stored_sha != get_frozen_r0_eval_set_sha256()` → RENDER BLOCKED（走 `_render_eval_set_not_frozen_report`）。
- `render_report()` 签名新增 `frozen_r0_eval_set_sha256=""` 与 `matches_frozen_baseline=None`，并在报告正文新增一行"与冻结 R0 基线（commit c0f5782）一致性"判定；两个调用点（`run_experiment` OK 分支、`render_from_result` OK 分支）均已透传。
- 不依赖 `case_count` / gold summary / 文件名代替 SHA 校验。

### 2.3 R1-F3（安全更新现有实验产物）
- 通过一次性迁移脚本（stdlib + 包导入，仅本地运行，未入库、未提交）完成：
  1. **证据校验（全成立才迁移）**：`status=="ok"`；`eval_set_sha256 == FROZEN_R0_EVAL_SET_SHA256`；`eval_set_case_count==32`；`pools_actual=={48:48,100:108,200:204}`；资料全 `match`；`material_drift==[]`。
  2. **仅新增两个顶层元数据字段**：`frozen_r0_eval_set_sha256`（= 冻结常量）、`matches_frozen_baseline: true`。**未改动任何**检索结果 / Recall / Top-1 / 失败归因 / 耗时（脚本末尾回读断言 `page_recall_at_k` 仍为 `0.0938`）。
  3. 基于现有 JSON 调 `render_from_result()` **重新渲染** `.md`（不重跑检索），使报告出现"已校验 frozen_r0_eval_set_sha256 相同"。

## 3. 测试（先补 8 项回归、再实现已落实于 §2 之前）

新增于 `tests/test_candidate_pool_eval.py`：
1. `test_render_eval_set_mismatch_blocked_does_not_keyerror` —— F1：`eval_set_mismatch` BLOCKED 复渲染不抛 KeyError，仍输出 BLOCKED。
2. `test_render_material_drift_blocked_rerender_without_material_verification` —— F1：`material_drift` BLOCKED 即使缺 `material_verification` 也能安全重渲染。
3. `test_render_unknown_blocked_reason_fail_closed` —— F1：未知 BLOCKED 原因 fail-closed，不生成正常报告。
4. `test_render_blocks_when_sha_matches_current_but_not_frozen` —— F2：SHA 与当前一致、但≠冻结基线仍 BLOCKED。
5. `test_render_allows_when_three_shas_align` —— F2：三者一致允许正常重渲染。
6. `test_render_reports_frozen_fields_from_payload` —— F2：复渲染把冻结字段透传并展示。
7. 复用既有 `test_r1_rerender_blocks_when_eval_set_changed` —— 缺 SHA / SHA 不一致 fail-closed（覆盖 F2 #4/#5）。
8. 复用既有 `test_r1_blocks_before_retrieval_when_eval_set_sha_mismatch` —— `run_mode`/建索引入口未被调用（覆盖 F2 #8）。

测试均用临时文件 + monkeypatch，不联网、不建真实 Chroma 索引、不加载模型。

## 4. 验收命令实际输出（如实保留）

```powershell
# 命令 1：目标测试（仓库无 .venv，改用 managed venv + chromadb 桩；等价于任务卡命令）
.\.venv\Scripts\python.exe -m pytest tests/test_bilingual_eval.py tests/test_candidate_pool_eval.py -q
# 实际：46 passed

# 命令 2：全量 pytest -q
# 实际：仅 7 个其它模块在收集阶段报错，根因是缺 yfinance/requests/pandas/dashscope/fastapi 等重依赖
#       （与本任务无关，也不在改动范围）；bilingual_eval / candidate_pool_eval 两个目标模块不在报错列表。
#       7 errors during collection（test_api / test_fetch_materials / test_financial_data /
#       test_golden_regression / test_llm_generation / test_qa / test_report_jobs）

# 命令 3：git diff --check
# 实际：exit 0（干净；仅仓库既有 CRLF 归一 warning，非 whitespace 错误）
```

> 说明：任务卡验收命令 `.\.venv\Scripts\python.exe` 在本环境不可用（无 `.venv`），已用功能等价的 managed venv（含 chromadb import 桩）替代并如实标注。

## 5. F3 证据（可独立复现）

- 冻结基线常量：`bilingual_eval.FROZEN_R0_EVAL_SET_SHA256 = 8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5`（来自 commit `c0f5782`，与冻结提交内原始字节逐字节一致，前轮已核验）。
- 现有 JSON：`eval_set_sha256 == 上述常量` → 同一组实验结果；`status=="ok"`、`case_count==32`、`pools_actual=={48:48,100:108,200:204}`、资料全 `match`、`material_drift==[]` → 结构自洽，确为冻结基线实验。
- 迁移后回读：`frozen_r0_eval_set_sha256` 一致、`matches_frozen_baseline is True`、`page_recall_at_k` 仍为 `0.0938`（指标未动）。

## 6. 未验证项 / 剩余风险

- **全量 `pytest -q` 未完整通过收集**：7 个其它模块因缺重依赖收集失败，与本任务无关、非改动所致；需在含完整依赖的 `.venv` 中复跑。
- **真实 Chroma 端到端评测未在此环境运行**：仅离线 mock / 复渲染测试。
- **F2 硬编码基线风险（沿用前轮结论）**：若日后 `data/bilingual_eval_set.json` 被正当更新但 `FROZEN_R0_EVAL_SET_SHA256` 未同步，R1 会误 BLOCKED。这是预期 fail-closed；建议未来以"基线元数据文件 + 提交钩子"替代硬编码常量（已记录为后续改进项，不在本轮范围）。
- **F3 迁移对 JSON 做了整文件 `json.dumps` 重排**：仅重排空白、数值/字符串/结构零改动（脚本已断言关键指标不变）；若审阅者希望最小化 diff，可改为就地插入两字段，但当前做法更不易出错。

## 7. 最终 git status（`--short --untracked-files=all`）

```
 M .gitignore
 M KNOWN_ISSUES.md
 M data/evaluations/bilingual_r0.json
 M data/evaluations/bilingual_r0.md
 M "docs/投研服务台升级总纲.md"
 M investment_assistant/bilingual_eval.py
 M tests/test_bilingual_eval.py
?? .codebuddy/CODEBUDDY.md
?? bug.md
?? data/evaluations/candidate_pool_sensitivity.json   （F3 已安全补充两字段）
?? data/evaluations/candidate_pool_sensitivity.md     （F3 已基于 JSON 重新渲染）
?? "docs/CODEX_REVIEW_R1_guardrails.md"
?? "docs/R1_评测护栏_WorkBuddy修复回应.md"            （本文件，新建）
?? "docs/R1_评测护栏收尾任务卡.md"
?? investment_assistant/candidate_pool_eval.py        （F1/F2 修复所在，未跟踪新文件）
?? tests/test_candidate_pool_eval.py                  （F1/F2 回归测试，未跟踪新文件）
```

- 冻结区 `rag.py` / `workflow.py` / `llm_generation.py` / `safety.py`：**未出现于 diff**（确认未改）。
- 未执行 `git add / commit / stash / reset / clean`。

## 8. 完成标准对照（任务卡 §7）

- [x] F1/F2/F3 三项定点修复完成且测试覆盖。
- [x] 冻结区未改、用户原有改动未被覆盖。
- [x] 针对性测试与 `git diff --check` 结果如实报告。
- [x] 最终 `git status` 已输出；无暂存 / 提交。
- [x] 未自动开始 R1 检索优化或后续阶段。

请审阅验收；验收通过后由你决定是否提交，或指示下一步。
