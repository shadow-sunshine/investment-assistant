# R5 任务：证据与交付门禁（Evidence / Delivery Gates）

> 前置：R4 已完成独立验收。执行本任务前先读取 `docs/CURRENT_TASK.md`、`docs/REVIEW_LOG.md`、`docs/DECISIONS.md`、`KNOWN_ISSUES.md` 与本项目现有 `qa.py`、`report_jobs.py`、`api.py`、报告 JSON/Markdown 结构。
> 执行方式：WorkBuddy 一次性完成本文件全部要求，完成后停止，不自动进入 R6。
> 目标：让每条对外关键结论都能回到同一标的、同一报告版本、同一资料来源中的原文/字段/期间/单位/页面；无法证明时 fail-closed，不把“有引用”冒充“已被证据支持”。

## 1. 范围与冻结边界

本阶段只做“证据绑定、交付门禁、可解释拒答/待审核”，不做检索算法升级、不做真实权限、不做通用 Agent/MCP。

不得修改：

- `investment_assistant/rag.py`
- `investment_assistant/workflow.py`
- `investment_assistant/llm_generation.py`
- `investment_assistant/safety.py`

不得修改线上默认检索路径；不得把 `requested_by` 当认证或授权；不得安装新依赖；不得访问真实网络生成评测结果；不得清理现有未提交或未跟踪文件；不得执行 `git add -A`、commit、stash、reset、clean。

优先复用已有报告审计 JSON、`qa.py` 的 evidence-bound answer 结构、`report_jobs.py` 的任务状态和 R4 的来源健康/降级契约。先检查现有能力，避免重复造一套报告格式。

## 2. 必须实现的能力

### A. 统一 Claim / Evidence 数据模型

在非冻结区新增一个小模块（名称可为 `evidence_gates.py`），定义可序列化模型，至少包含：

- `claim_id`
- `claim_text`
- `ticker`
- `report_id` 或报告版本标识
- `source_id` / 资料文件身份（不能只存页码）
- `page` 或结构化字段路径
- `period`
- `unit`
- `evidence_excerpt` 或可验证的证据锚点
- `support_status`
- `validation_errors[]`

支持状态至少包括：`supported`、`partial`、`unsupported`、`conflict`、`needs_review`。

### B. 证据验证必须 fail-closed

实现纯函数/服务入口，对 Claim-Evidence 逐条校验：

1. claim 的 ticker 必须与报告 ticker 一致；
2. `report_id`/资料版本必须存在且可解析；
3. source_id 必须对应当前报告的真实来源，不得只用相同 page 号判定命中；
4. page/字段路径必须在来源证据范围内；
5. period、unit、数值/字段锚点必须与证据一致（不能只看 citation 是否存在）；
6. evidence_excerpt 在允许的证据文本/字段中确实存在，或有明确的结构化字段匹配；
7. 来源冲突、资料缺失、版本漂移、无法确认时不得判为 `supported`，应为 `conflict` / `needs_review` / `unsupported`。

验证失败时返回结构化原因，不直接把内部异常堆栈暴露给用户。

### C. 交付门禁

提供一个明确的 `evaluate_delivery()` / `render_delivery()` 入口，输入报告及其 claims/evidence，输出：

- `release_status`: `released` / `blocked` / `needs_review` / `partial`
- `claim_results[]`
- `blocking_reasons[]`
- `degradation_reasons[]`（可复用 R4）
- 报告/资料版本标识

门禁规则：

- 存在 `unsupported`、`conflict` 或无法验证的关键 claim 时，不能输出“已验证完成”的正常交付状态；
- 只有 citation 存在、但 ticker/source/page/期间/单位/片段不匹配时，必须阻断；
- `partial` 只能显式带缺口交付，不能静默补全；
- `needs_review` 必须可供上层 API/任务状态消费，不能被转换成 200/正常成功；
- 不允许用旧缓存、另一标的或未经校验的网络文本替代缺失证据。

### D. 接入现有外层，不侵入冻结工作流

在 `report_jobs.py` 或 `api.py` 外层接入最小门禁状态，使报告任务/问答响应至少能区分：

- 证据已验证可交付；
- 部分证据、显式降级；
- 证据不足/冲突、阻断或待人工审核。

不要重写现有报告生成逻辑。若现有报告 JSON 暂时无法生成完整 claims，先提供确定的离线 claim fixture 和外层门禁接口，并在报告中明确“尚未接入自动 claim 抽取”，不得伪造已完成全链路绑定。

## 3. 测试要求

只做离线、可重复测试，不访问真实网络：

1. 通过一条同 ticker、同 source_id、同 page、同 period、同 unit、同 excerpt 的 supported 样例；
2. ticker 不一致必须阻断；
3. 不同 PDF 相同 page 号必须阻断；
4. report/source 版本或 SHA 不匹配必须阻断；
5. excerpt/字段不存在必须阻断；
6. period/unit/数值不一致必须阻断；
7. conflict / needs_review 的交付状态不能误报为 released；
8. partial 交付必须带 `blocking_reasons[]` 或 `degradation_reasons[]`；
9. API/任务序列化回归；
10. 原冻结测试、R0/R1/R3 评测回归。

要求先运行相关测试，再运行全量测试；只报告实际运行过的命令与结果。未运行的真实网络、浏览器 E2E、自动 claim 抽取和人工审核路径必须明确写“未验证”。

## 4. 交付报告格式

最终报告必须包含：

1. 改了什么 / 没改什么；
2. Claim/Evidence 模型字段与 fail-closed 规则；
3. 交付门禁状态与 API/任务接入；
4. 实际测试命令及结果；
5. 未验证项；
6. 剩余风险；
7. 停止，不进入 R6。
