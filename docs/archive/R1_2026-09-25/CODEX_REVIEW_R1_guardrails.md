# Codex 审阅请求：R1 评测护栏收尾

> 本文件是给 Codex（独立审阅者）的**自包含审阅简报**。Codex 不会看到原始对话，请据此独立完成代码审阅与复测，并给结论。
> 对应任务卡：`docs/R1_评测护栏收尾任务卡.md`（同一目录）。
> 仓库根目录：`D:\It\Test_Project\Investment_Assistant`

---

## 0. 一句话背景

R0/R1 双语检索评测已落地，本次收尾给评测加"样本不可变"护栏：
1. 目标页命中判定在 R0/R1 必须统一（不能把不同 ticker 的同页码当命中）；
2. R1 候选池实验只能对照**被冻结的 R0 样本集**，样本一变就在建索引/检索前 `BLOCKED`；
3. 结果复渲染也要校验样本集身份，禁止用新样本重写旧报告。

本任务**不做检索算法优化**，不动 `rag.py` / `workflow.py` / `llm_generation.py` / `safety.py`（"冻结区"）。

---

## 1. 本次改动范围（请重点审这些文件）

| 文件 | git 状态 | 关键行 | 改动性质 |
|---|---|---|---|
| `investment_assistant/bilingual_eval.py` | 已跟踪，已修改（M） | L48-56（常量+函数）、L131/141（`MaterialDriftError`）、L457-496（`reattribute_payload` 护栏） | 增量修改，未覆盖历史改动 |
| `investment_assistant/candidate_pool_eval.py` | **未跟踪新文件（??）** | L43/45（import）、L71-82（`_read_r0_eval_set_sha`）、L556-577（两份 BLOCKED 报告）、L586-689（`run_experiment` 护栏）、L692-717+（`render_from_result` 护栏） | 新文件（用户既有未跟踪文件，本次在其上增量实现） |
| `tests/test_bilingual_eval.py` | 已跟踪，已修改（M，+114 行） | 文件末尾新增 R0 护栏测试 | 新增/调整测试 |
| `tests/test_candidate_pool_eval.py` | **未跟踪新文件（??）** | 全文 | 新增/调整测试 |

⚠️ **重要**：`candidate_pool_eval.py` 与 `tests/test_candidate_pool_eval.py` 是**未跟踪文件**，`git diff` 看不到内容——请直接读取这两个文件审阅，不要只看 `git diff`。

**确认未改动（冻结区，请 Codex 复核 `git diff` 不应出现）**：
`rag.py`、`workflow.py`、`llm_generation.py`、`safety.py`。

**用户既有改动（本次未触碰，供你区分）**：
`.gitignore`、`KNOWN_ISSUES.md`、`data/evaluations/bilingual_r0.json`、`.md`、`docs/投研服务台升级总纲.md`。这些不是本次审阅对象，仅用于区分"哪些是本次新增"。

---

## 2. 运行与复测方法

### 环境现状
- 仓库**没有** `.venv`，任务卡验收命令 `.\.venv\Scripts\python.exe ...` 在本机不可用。
- 目标测试是 **mock/monkeypatch 驱动**的，运行时不建真实 Chroma 索引、不联网、不加载模型；但导入链需要 `rag` 可导入（间接依赖 `chromadb`/`numpy`/`pypdf`）。

### 复测命令（A/B 二选一）
**A. 轻量离线（原执行者所用，最快）**
```powershell
# 用 managed python 建 venv 并装最小依赖
& "C:\Users\shadow\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m venv C:\Users\shadow\.workbuddy\binaries\python\envs\default
& C:\Users\shadow\.workbuddy\binaries\python\envs\default\Scripts\pip.exe install pytest numpy pypdf
# chromadb 用 import 桩（真实 chromadb 不影响离线 mock 测试）
# 把 chromadb 桩放进 venv site-packages：新建 C:\Users\shadow\.workbuddy\binaries\python\envs\default\Lib\site-packages\chromadb\__init__.py，内容：
#     class PersistentClient:
#         def __init__(self, *a, **k): raise RuntimeError("chromadb stub")
& C:\Users\shadow\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m pytest tests/test_bilingual_eval.py tests/test_candidate_pool_eval.py -q -p no:cacheprovider
```

**B. 完整依赖（推荐 Codex 用，贴近任务卡原意）**
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest tests/test_bilingual_eval.py tests/test_candidate_pool_eval.py -q
.venv\Scripts\python.exe -m pytest -q
git diff --check
```

**必须复测并报告真实输出**：
1. `tests/test_bilingual_eval.py` + `tests/test_candidate_pool_eval.py` —— 目标是全部 pass。
2. `git diff --check` —— 应无 whitespace 级告警（仓库既有 CRLF 归一警告可忽略，非本次引入）。
3. 全量 `pytest -q` —— 若其他 7 个模块因缺 `fastapi`/`langchain` 收集失败，**如实说明**，不要算作本次改动的问题。

---

## 3. 验收标准对照（任务卡 §4 / §5 / §7）

| 任务卡要求 | 实现位置 | 是否满足（待你确认） |
|---|---|---|
| §4.A 统一目标来源判定（ticker+gold page 双校验，不比较纯页码） | `bilingual_eval.py` L75 `_is_target_source`、L88-94 `top_result_is_relevant`、L194、L216；`candidate_pool_eval.py` L52 import、L123、L150 | **核查项 5** |
| §4.B.2 冻结 SHA 来自 Git 历史 `c0f5782`，与提交内原始字节一致 | 基线 SHA `8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5`（见 §4） | **核查项 1** |
| §4.B.3 冻结 SHA 固定为代码常量，仅显式建新基线时更新 | `bilingual_eval.py` L48-56 `FROZEN_R0_EVAL_SET_SHA256` / `get_frozen_r0_eval_set_sha256()` | 已做 |
| §4.B.4 结果记录 `eval_set_sha256`；R1 另记冻结 R0 SHA | `candidate_pool_eval.py` L613-615/640-645/668-673（payload 各分支均记 `frozen_r0_eval_set_sha256`） | 已做 |
| §4.B.5 `--eval-set` SHA≠冻结基线 → 建索引/检索前 `BLOCKED`，不继续跑 | `candidate_pool_eval.py` L603-632（`matches_frozen_baseline` 为 False 即返回，先于 L663 `run_mode`） | **核查项 2** |
| §4.B.6 `render_from_result` 校验 sha 一致，不一致则阻断 | `candidate_pool_eval.py` L707-717+（stored_sha 与 current_sha 不一致 → BLOCKED 报告） | **核查项 3** |
| §4.B.7 旧结果无 SHA → fail-closed，不静默补写 | `candidate_pool_eval.py` L712-716 → `_render_eval_set_missing_sha_report`；`bilingual_eval.py` L469-475 | **核查项 3** |
| §4.B.1 `reattribute_payload` 不得用新样本重写旧报告 | `bilingual_eval.py` L468-480（无 SHA / 不匹配均 `MaterialDriftError`） | **核查项 4** |
| §5.1-5.4 命中判定反例测试 | `tests/test_bilingual_eval.py`（正确 ticker+gold page / 错误 ticker 同页 / 非 gold page） | **核查项 6** |
| §5.5 R1 冻结 SHA 匹配可继续、不匹配 BLOCKED 且 `run_mode` 未调用 | `tests/test_candidate_pool_eval.py` | **核查项 2/6** |
| §5.6 `render_from_result` 缺 SHA / 不一致 fail-closed | `tests/test_candidate_pool_eval.py` | **核查项 3/6** |
| §5.7 资料 SHA drift/missing 既有 fail-closed 仍成立 | 未破坏（drift 分支 L633-662 保留） | **核查项 7** |

---

## 4. 基线 SHA 证据（请 Codex 独立核验）

- 冻结提交：`c0f5782` —— "test(R0): 固化四象限双语评测样本与评测器（评测结果尚未产生）"
- 样本文件：`data/bilingual_eval_set.json`
- 冻结基线 SHA256（原始字节）：
  `8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5`
- 独立核验命令（应与原执行者结论一致）：
```powershell
git -C D:\It\Test_Project\Investment_Assistant show c0f5782:data/bilingual_eval_set.json | python -c "import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"
python -c "import hashlib;print(hashlib.sha256(open(r'D:\It\Test_Project\Investment_Assistant\data\bilingual_eval_set.json','rb').read()).hexdigest())"
```
两行输出应**逐字节相同**；且都应等于 `FROZEN_R0_EVAL_SET_SHA256` 常量值。

---

## 5. 已验证 / 未验证（原执行者自报，待你独立确认）

**已验证（原执行者）**
- 两目标测试模块 **40 passed**。
- `git diff --check` 干净（仅仓库既有 CRLF 归一警告）。
- 基线 SHA 与当前文件逐字节一致。

**未验证（原执行者诚实标注）**
- 全量 `pytest -q` 未完整通过收集：7 个其他模块（`test_api.py`/`test_fetch_materials.py`/`test_financial_data.py`/`test_golden_regression.py`/`test_llm_generation.py`/`test_qa.py`/`test_report_jobs.py`）因缺 `fastapi`/`langchain` 等重依赖收集失败，与本次改动无关。
- 真实 Chroma 索引端到端评测未跑（仅离线 mock 测试）。

---

## 6. 给 Codex 的具体审阅问题（重点）

请逐条回答，给出文件:行号 与 你的判断（通过 / 问题 / 建议）：

1. **基线常量正确性**：`FROZEN_R0_EVAL_SET_SHA256` 是否真等于 `git show c0f5782:data/bilingual_eval_set.json` 的 sha256？硬编码常量是否存在被悄悄改错的风险？
2. **fail-closed 时序**：`run_experiment` 的 mismatch 分支（L605-632）是否**确实早于** `run_mode` 调用（L663）返回，且分支内**未调用** `run_mode` / 任何建索引逻辑？有无从其它入口绕过的可能？
3. **复渲染 fail-closed**：`render_from_result` 在 `stored_sha is None`（L712-716）与 `stored_sha != current_sha`（L717+）两种情况下，是否都只写出 BLOCKED 报告、**绝不**写出"看似正常"的可比报告？
4. **旧报告防覆盖**：`reattribute_payload`（L468-480）是否彻底拒绝"无 SHA / SHA 不匹配"时用新样本重写？是否存在其它函数（如直接调 `render_report`）能绕过该护栏？
5. **§4.A 统一判定**：`_is_target_source` 是否同时校验 ticker（或样本目标资料文件身份）**和** gold page？`page_hit_at_k` / `keyword_verified_hit_at_k` / `top_result_is_relevant` / `unscoped_page_hit_at_k` 是否全部经它、无"只比页码"的旁路？R0 与 R1 逻辑是否一致？
6. **测试覆盖**：§5 列出的反例（错误 ticker+同页、正确 ticker+非 gold page、R1 匹配继续、R1 不匹配 BLOCKED 且 run_mode 未调用、render 缺 SHA、render 不一致、reattribute 缺/不一致）是否都有对应测试？有无遗漏的护栏边界未测？
7. **既有行为未被破坏**：§5.7 资料 SHA drift/missing 的原有 fail-closed 是否仍成立（L633-662 与 `bilingual_eval.py` 既有逻辑未被本次改动削弱）？
8. **冻结区未动**：`rag.py`/`workflow.py`/`llm_generation.py`/`safety.py` 是否确实无改动？
9. **可简化性**：硬编码 `FROZEN_R0_EVAL_SET_SHA256` 与"日后样本被正当更新但常量未同步会误 BLOCKED"的风险，是否有更稳妥的同步机制（如基线元数据文件 + 提交钩子）？是否值得在本任务外单独立项？
10. **其它**：代码风格、命名、异常处理、报告文案可读性，有无明显可改进点？

---

## 7. 附：当前 `git status --short --untracked-files=all`

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
?? data/evaluations/candidate_pool_sensitivity.json
?? data/evaluations/candidate_pool_sensitivity.md
?? "docs/R1_评测护栏收尾任务卡.md"
?? investment_assistant/candidate_pool_eval.py
?? tests/test_candidate_pool_eval.py
```
（无暂存、无提交；本任务未执行 `git add/commit/stash/reset/clean`。）

---

## 8. 红线（Codex 不要做的事）

- 不要为了"让测试通过"而改 gold page、删样本、放宽断言。
- 不要修改冻结区四个文件。
- 不要执行任何 git 写操作（add/commit/reset/clean）。
- 不要自动开始 R1 检索优化——本次仅收尾护栏，验收后停止。

请审阅后给出：**通过 / 需修改（列出具体点）** 的结论，并附复测输出。
