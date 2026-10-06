# R1 评测护栏复审（WorkBuddy 修复后的独立复核）

> 日期：2026-09-25
> 仓库：`D:\It\Test_Project\Investment_Assistant`
> 审阅方式：只读审阅；未修改业务代码、测试代码、实验结果或冻结区；仅新增本审阅报告。
> 审阅对象：`investment_assistant/candidate_pool_eval.py`、`tests/test_candidate_pool_eval.py`、R1 实验 JSON/Markdown 产物及 WorkBuddy 修复回应。

## 1. 结论

**结论：需修改后再通过；暂不建议提交，也不要进入 R1 检索优化。**

WorkBuddy 声称的 R1-F1、R1-F2、R1-F3 主路径基本已实现，目标测试和当前环境下的全量测试均通过；但 `render_from_result()` 对冻结基线元数据的校验仍不完整：它校验了 `payload.eval_set_sha256` 与当前文件/代码常量，却没有严格校验 payload 中已有的 `frozen_r0_eval_set_sha256` 字段，也没有要求 `matches_frozen_baseline` 必须明确为 `True`。因此，带有伪造或缺失冻结元数据的旧结果仍可能被渲染为正常报告。

这不是检索指标本身被改写的证据，但会削弱“报告元数据可审计、复渲染不接受不完整/伪造基线证明”的护栏。建议修复后重新跑目标测试与全量测试，再验收。

## 2. 已独立验证并通过的项目

### 2.1 R0 冻结样本 SHA 正确

实际核验命令同时比较：

- `git show c0f5782:data/bilingual_eval_set.json` 的原始字节 SHA256；
- 当前 `data/bilingual_eval_set.json` 的 SHA256；
- `bilingual_eval.FROZEN_R0_EVAL_SET_SHA256` 常量。

三者均为：

```text
8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5
```

判定：**通过**。

### 2.2 R1 mismatch 在检索前阻断

`investment_assistant/candidate_pool_eval.py:647-686` 先计算评测集 SHA 并检查冻结基线，`run_mode` 调用位于 `:717`，因此 mismatch 分支不会建索引或调用检索。

目标测试 `test_r1_blocks_before_retrieval_when_eval_set_sha_mismatch` 也用 monkeypatch 证明 `run_mode` 未被调用。

判定：**通过**。

### 2.3 BLOCKED 复渲染主路径

`investment_assistant/candidate_pool_eval.py:765-785` 已按 `blocked_reason` 分派：

- `eval_set_mismatch` 不再读取不存在的 `material_verification`；
- `material_drift` 使用 `.get()`，不因缺字段抛 `KeyError`；
- 未知原因进入 fail-closed 报告，不生成正常报告。

对应测试：`tests/test_candidate_pool_eval.py:378-426`。

判定：**通过**。

### 2.4 当前 R1 产物元数据完整且结构自洽

`data/evaluations/candidate_pool_sensitivity.json` 当前包含：

- `eval_set_sha256`；
- `frozen_r0_eval_set_sha256`；
- `matches_r0_eval_set: true`；
- `matches_frozen_baseline: true`；
- `material_drift: []`；
- `pools_actual: {48: 48, 100: 108, 200: 204}`；
- `online_path_modified: false`。

当前 JSON 的两个模式都保留了 48 / 100 / 200 三个池子的指标，且 `candidate_pool_sensitivity.md` 已出现冻结基线一致性说明。仅从当前产物内容看，未发现指标字段被清空或被改成 BLOCKED 的迹象。

判定：**通过当前产物结构核验**。但由于迁移前 JSON 未作为独立快照保留，本次无法仅靠工作树证明迁移前所有数值与迁移后逐字节一致；这部分只能接受 WorkBuddy 回应中的迁移断言，不能算独立强证据。

### 2.5 冻结区未出现工作树改动

当前 `git status --short` 中没有：

- `investment_assistant/rag.py`；
- `investment_assistant/workflow.py`；
- `investment_assistant/llm_generation.py`；
- `investment_assistant/safety.py`。

判定：**通过**。

## 3. 发现的问题

### R1-F4：复渲染没有严格校验 payload 的冻结 SHA 元数据

**级别：P2，需修复后通过。**

**文件与行号：**

- `investment_assistant/candidate_pool_eval.py:806-812`
- `investment_assistant/candidate_pool_eval.py:815-824`

当前逻辑的核心判断是：

```python
frozen_sha = get_frozen_r0_eval_set_sha256()
if payload.get("matches_frozen_baseline") is False or stored_sha != frozen_sha:
    # BLOCKED
```

问题在于：

1. `payload["frozen_r0_eval_set_sha256"]` 的值没有被读取并与 `frozen_sha` 比较；
2. `matches_frozen_baseline` 缺失时为 `None`，`None is False` 为假，因此仍可继续正常渲染；
3. 正常渲染时 `:821-823` 又把 payload 中的冻结 SHA 和判定字段透传给 `render_report()`，会造成“冻结证明已校验”的报告文案与实际元数据不一致。

#### 独立反例

用临时 JSON（不触碰仓库文件）构造：

```json
{
  "status": "ok",
  "eval_set_sha256": "<真实冻结 SHA>",
  "frozen_r0_eval_set_sha256": "forged",
  "matches_frozen_baseline": true,
  "matches_r0_eval_set": true,
  "pools_requested": [48, 100, 200],
  "material_verification": {},
  "results": {
    "hash": {"index_seconds": 0.0},
    "semantic": {"error": "x"}
  }
}
```

实际结果：`render_from_result()` 返回正常报告，不含 `BLOCKED`。报告仍显示冻结一致性已校验，但透传的冻结 SHA 是伪造值。

同样，删除 `frozen_r0_eval_set_sha256` 和 `matches_frozen_baseline` 两字段时，仍会生成正常报告，只是正文显示“未记录 frozen_r0_eval_set_sha256”。这不符合本轮新增冻结元数据的 fail-closed 目标。

#### 修复方向

建议在 `stored_sha == current_sha` 之后，要求以下条件全部成立，否则输出 RENDER BLOCKED：

```python
payload_frozen_sha = payload.get("frozen_r0_eval_set_sha256")
if (
    stored_sha != frozen_sha
    or payload_frozen_sha != frozen_sha
    or payload.get("matches_frozen_baseline") is not True
):
    # fail-closed
```

同时新增至少两个回归测试：

1. `frozen_r0_eval_set_sha256` 错误但 `eval_set_sha256` 正确 → BLOCKED；
2. 缺少 `frozen_r0_eval_set_sha256` 或 `matches_frozen_baseline` → BLOCKED。

## 4. 非阻断但建议补强的风险

### 4.1 非 BLOCKED 的未知/残缺状态仍可能抛 `KeyError`

`render_from_result()` 只对 `status == "blocked"` 做专门分派；对于 `status` 缺失、`pending` 或其他未知状态，会直接进入正常路径，并在 `:817-823` 读取 `payload["results"]`、`payload["material_verification"]` 等字段。

独立构造最小 payload 后，实际得到：

```text
KeyError: 'results'
```

这不影响本次已覆盖的 BLOCKED 三类路径，但如果 `--rerender` 处理的是损坏或半写入 JSON，用户会看到未处理异常而不是明确的 RENDER BLOCKED。建议后续把“只有 `status == ok` 才允许进入正常渲染”写成显式判断，其他状态统一 fail-closed。

### 4.2 `render_report()` 本身仍是无护栏的纯渲染函数

`render_report()` 在 `:252-260` 接受任意结果与 SHA 元数据，不自行验证样本身份。当前 CLI 的 `--rerender` 入口走的是 `render_from_result()`（`:835-839`），因此现有入口没有绕过复渲染护栏的证据；但未来若新增调用方直接调用 `render_report()`，可以绕过这些检查。

这暂不作为本轮阻塞项，建议保持 `render_report()` 为内部 helper，或后续改名为 `_render_report()` 并只通过已校验的 wrapper 调用。

## 5. 实际验证命令与结果

### 5.1 目标测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_bilingual_eval.py tests/test_candidate_pool_eval.py -q
```

实际结果：

```text
46 passed in 4.54s
```

### 5.2 全量测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

实际结果：

```text
152 passed, 1 warning in 26.92s
```

警告为已安装依赖中的 Starlette/AnyIO 弃用警告，不是本轮失败。

### 5.3 空白检查

```powershell
git diff --check
```

实际结果：退出码 0；仅有 Git 关于 LF/CRLF 的归一化提示，没有 whitespace error。

### 5.4 冻结 SHA

```text
frozen  = 8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5
current = 8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5
constant= 8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5
```

## 6. 工作树与边界

- 本次未执行 `git add`、`commit`、`stash`、`reset`、`clean`。
- 未清理、覆盖或恢复用户既有修改和未跟踪文件。
- 未修改冻结区。
- 当前工作树仍包含用户/前序任务的既有修改，不能据此直接提交全部文件。

## 7. 验收建议

1. WorkBuddy 只修 R1-F4，不要重跑昂贵检索，不要修改 `rag.py`、`workflow.py`、`llm_generation.py`、`safety.py`。
2. 新增上述两个冻结元数据反例测试，并补一个未知非 `ok` 状态的 fail-closed 测试（可作为加分项）。
3. 重新运行目标测试、全量测试、`git diff --check`。
4. 修复通过后，再决定是否提交 R1 护栏；在此之前不要开始中文锚点召回或其他检索优化。

**最终判定：需修改（R1-F4 修复完成并复测通过后再验收）。**
