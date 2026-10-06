# R1 评测护栏最终复审

> 日期：2026-09-26
> 仓库：`D:\It\Test_Project\Investment_Assistant`
> 审阅模式：只读业务审阅；未修改业务代码、测试代码、实验产物或冻结区。
> 本次新增：本审阅报告。

## 1. 最终结论

**R1 评测护栏：通过，可以进入下一阶段。**

WorkBuddy 针对上一轮 R1-F4 的修复已经生效：复渲染现在严格要求结果 JSON 同时满足：

1. `eval_set_sha256 == 当前评测集 SHA`；
2. `eval_set_sha256 == 冻结 R0 SHA`；
3. `frozen_r0_eval_set_sha256 == 冻结 R0 SHA`；
4. `matches_frozen_baseline is True`。

任一条件不满足都会输出 `RENDER BLOCKED`，不会生成正常可比报告。

## 2. 本轮独立核验

### 2.1 F4 伪造冻结 SHA

位置：`investment_assistant/candidate_pool_eval.py:817-832`

构造临时结果：

```json
{
  "status": "ok",
  "eval_set_sha256": "真实冻结 SHA",
  "frozen_r0_eval_set_sha256": "forged",
  "matches_frozen_baseline": true
}
```

结果：

```text
BLOCKED
```

判定：**通过**。

### 2.2 F4 缺少冻结元数据

分别测试：

- 缺少 `frozen_r0_eval_set_sha256`；
- 缺少 `matches_frozen_baseline`；
- `matches_frozen_baseline: false`。

三种情况均输出：

```text
BLOCKED
```

判定：**通过**。

### 2.3 合法结果复渲染

当以下字段均正确时：

```text
stored_sha == current_sha == frozen_sha
frozen_r0_eval_set_sha256 == frozen_sha
matches_frozen_baseline == True
```

结果正常生成 R1 Markdown 报告。

判定：**通过**。

### 2.4 R1 mismatch 时序

`run_experiment()` 在 `candidate_pool_eval.py:647-686` 先校验评测集 SHA，`run_mode` 调用位于 `:717` 之后。

已有测试验证 mismatch 时 `run_mode` 未被调用。

判定：**通过**。

### 2.5 BLOCKED 复渲染

`candidate_pool_eval.py:774-794` 对以下情况分别处理：

- `material_drift`；
- `eval_set_mismatch`；
- 未知 `blocked_reason`。

不再假设所有 BLOCKED payload 都存在 `material_verification`，也不会因为缺字段抛 `KeyError`。

判定：**通过**。

### 2.6 冻结样本 SHA

独立核验结果：

```text
冻结提交 c0f5782：8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5
当前文件：       8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5
代码常量：       8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5
```

判定：**通过**。

### 2.7 冻结区

以下文件未出现在当前工作树修改列表中：

- `investment_assistant/rag.py`
- `investment_assistant/workflow.py`
- `investment_assistant/llm_generation.py`
- `investment_assistant/safety.py`

判定：**通过**。

## 3. 测试结果

### 3.1 目标测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_bilingual_eval.py tests/test_candidate_pool_eval.py -q
```

实际结果：

```text
48 passed in 6.36s
```

### 3.2 全量测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

实际结果：

```text
154 passed, 1 warning in 67.56s
```

唯一警告是 Starlette/AnyIO 弃用警告，不是本轮失败。

### 3.3 空白检查

```powershell
git diff --check
```

实际结果：退出码 0；仅有 LF/CRLF 归一化提示，无 whitespace error。

## 4. 剩余风险（不阻塞本轮通过）

### 4.1 非 `ok`、非 `blocked` 状态的 malformed payload

`render_from_result()` 当前主要按 `status == "blocked"` 分支，否则进入正常结果路径。

如果 payload 是 `status: "pending"` 且同时带有一部分正常结果字段但结构不完整，仍可能因缺少 `results`、`material_verification` 或模式字段而抛 `KeyError`，而不是输出统一的 `RENDER BLOCKED`。

这不影响本轮 R1-F1/F2/F3/F4 的验收，因为 `--rerender` 的合法输入是已产出的 `ok` 或 `blocked` 结果；但建议后续补一个状态契约：

```python
if payload.get("status") != "ok":
    fail_closed_render_blocked()
```

建议级别：**P2 后续加固，不阻塞本轮通过**。

### 4.2 `render_report()` 是低层 helper

`render_report()` 本身不负责样本 SHA 校验。当前 CLI 入口先经过 `render_from_result()`，未发现现有调用绕过护栏；后续应继续保持 `render_report()` 为内部渲染函数，不直接暴露给外部输入。

建议级别：**设计约束，不阻塞本轮通过**。

### 4.3 工作树不可整体提交

当前工作树仍混有前序任务与用户既有改动，不能执行 `git add -A` 或整体提交。若要提交，应由用户明确选择 R1 相关文件，并逐文件检查 diff。

## 5. 验收建议

- R1 评测护栏可以标记为**已通过**。
- 可以开始下一阶段的检索优化设计/实验，但仍应保持 R0 样本、资料 SHA、冻结区和评测先验规则不变。
- 下一阶段建议先做最小、可证伪的中文召回实验，不要直接修改线上默认检索路径。
- 在提交前，应单独审阅并选择性暂存：`candidate_pool_eval.py`、`tests/test_candidate_pool_eval.py`、R1 结果产物及相关说明文件；不要整体提交当前工作树。

## 6. 本次边界确认

- 未执行 `git add`、`commit`、`stash`、`reset`、`clean`。
- 未清理、覆盖或恢复其他未提交修改。
- 未重跑昂贵的真实检索实验。
- 未修改冻结区。

**最终判定：通过；允许进入下一阶段，但不允许因此自动提交整个工作树。**
