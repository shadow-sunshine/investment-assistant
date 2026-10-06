# R1 评测护栏状态契约复审

> 日期：2026-09-26
> 仓库：`D:\It\Test_Project\Investment_Assistant`
> 类型：只读代码审阅与验证；未修改业务代码、测试代码或实验产物。
> 本次新增：独立复审 Markdown。
> 本报告补充并修正 `docs/CODEX_REVIEW_R1_final.md` 对未知非 `ok` 状态风险的描述。

## 1. 结论

**F4 修复本身通过，但 R1 复渲染护栏仍有一个 P2 状态契约问题；整体建议“补一个小修复后再签最终通过”。**

上一份最终审阅报告把未知非 `ok` 状态列为 malformed payload 下可能 `KeyError` 的非阻塞风险。进一步复核发现，更重要的反例是：只要一个非 `ok` payload 具备正常字段形状、样本 SHA 和冻结字段均正确，`render_from_result()` 会把它作为正常结果渲染，而不是 BLOCKED。

## 2. 发现：只有明确 `status == "ok"` 才应进入正常渲染

**级别：P2。**

### 位置

`investment_assistant/candidate_pool_eval.py:772-774`，以及正常渲染路径 `:803-845`。

### 当前行为

函数仅对：

```python
payload.get("status") == "blocked"
```

做专门处理。除此之外的状态（包括 `pending`、缺失、拼错或未来新增状态）都会继续进入正常渲染路径。SHA 检查并不校验任务状态，因此状态字段并非 `ok` 的 payload 仍可能通过样本与冻结 SHA 护栏。

### 独立反例

用临时文件构造一个完整形状 payload：

```json
{
  "status": "pending",
  "eval_set_sha256": "<真实冻结 SHA>",
  "frozen_r0_eval_set_sha256": "<真实冻结 SHA>",
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

本轮实际结果：

```text
returned normal=True blocked=False
```

也就是 `status: pending` 被渲染成正常标题的 R1 报告。结果字段虽是测试桩，但已证明入口的状态判定没有 fail-closed。

### 影响

- 半写入、手工编辑或未来引入的中间态 JSON，若带有完整字段，可能被 `--rerender` 显示为完整正常实验报告。
- 这破坏了“未完成不返半成品”和“未知状态 fail-closed”的状态语义。
- 当前 `run_experiment()` 正常实现只产生 `ok` 或 `blocked`，所以不是现有标准产物的日常路径故障；这是输入边界/未来演进的可靠性缺口。

### 修复方向

建议在解析 JSON 后先做显式状态分流：

```python
status = payload.get("status")
if status == "blocked":
    ...
if status != "ok":
    写入明确的 RENDER BLOCKED（未知/未完成状态）
    return markdown_path
# 只有 status == "ok" 才继续 SHA 校验与正常渲染
```

并加测试：`status="pending"`、其余 SHA 与渲染字段完整时，必须输出 BLOCKED，不得出现正常报告标题。另建议覆盖缺失状态和未知状态。

## 3. F4 复审结果

上一轮发现的 R1-F4 已正确修复，代码位置 `candidate_pool_eval.py:817-832`：

- `stored_sha == frozen_sha`；
- `payload_frozen_sha == frozen_sha`；
- `matches_frozen_baseline is True`。

伪造 SHA、缺冻结 SHA、缺 matches 字段、matches 为 False 都会 BLOCKED；合法结果可以正常渲染。

判定：**F4 通过。**

## 4. 本轮实际验证

### 目标测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_bilingual_eval.py tests/test_candidate_pool_eval.py -q
```

实际：

```text
48 passed in 3.79s
```

### 全量测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

实际：

```text
154 passed, 1 warning in 18.03s
```

警告：Starlette/AnyIO 弃用警告。

### 未跟踪反例复现

在临时目录调用 `render_from_result()`，不改仓库 JSON：

```text
status=pending + SHA/冻结元数据正确 + 完整渲染字段
=> returned normal=True blocked=False
```

### 空白检查

```powershell
git diff --check
```

退出码 0；有 LF/CRLF 归一提示，没有 whitespace error。

### 冻结区

`git status` 对 `rag.py`、`workflow.py`、`llm_generation.py`、`safety.py` 无输出，未发现这些冻结文件有工作树改动。

## 5. 验收建议

1. 保留 F4 当前修复，不重跑昂贵检索。
2. 单独补 `status != "ok" and status != "blocked"` 的 fail-closed 分支与测试。
3. 复跑目标测试、全量测试和 `git diff --check`。
4. 通过后再把 R1 护栏整体标为最终通过。

## 6. 边界

- 未修改业务代码、测试、实验结果或已有审阅文档。
- 未执行 `git add`、`commit`、`stash`、`reset`、`clean`。
- 未清理或覆盖用户既有改动、未跟踪文件。
- 本轮仅新增本审阅报告。

**最终判定：R1-F4 通过；整体状态契约仍需补一处小修复，修复前暂不建议宣告 R1 护栏全部验收完成。**
