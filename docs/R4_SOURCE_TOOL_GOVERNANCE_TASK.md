# R4 任务：数据源与工具治理（Source / Tool Governance）

> 前置：R3 v3 必须先通过独立验收；本任务只在 R3 目标测试、真实 Chroma 复跑和全量测试通过后启动。
> 执行方式：WorkBuddy 一次性完成本文件全部要求，完成后停止，不进入 R5。
> 目标：让投研助手面对数据源/工具失败时可观测、可解释、可控降级，而不是把异常吞掉或继续生成貌似完整的结论。

## 1. 先读并核对

先读：`docs/CURRENT_TASK.md`、`docs/投研服务台升级总纲.md`、`KNOWN_ISSUES.md`、`README.md`，再核对当前工作树。保留所有已有未提交修改；不得执行 `git add -A`、commit、stash、reset、clean。

现有主要来源/工具至少包括：

- `investment_assistant/fetch_materials.py`：SEC EDGAR、巨潮资讯、港交所披露易；
- `investment_assistant/market_data.py`：市场数据来源；
- `investment_assistant/news_filter.py`：新闻/动态资料相关路径；
- `investment_assistant/report_jobs.py`：任务级幂等、失败与状态落盘；
- `investment_assistant/api.py`：对外 API 边界。

不得把 `requested_by` 或 ticker 参数当成认证/授权；R4 不实现真实多租户权限，权限留给 R6。

## 2. R4 交付目标

### A. 统一工具错误契约

新增一个不侵入冻结区的治理模块（名称自行选择，例如 `source_governance.py`），定义稳定、可序列化的错误/状态模型。至少覆盖：

- `invalid_input`
- `unsupported_ticker`
- `timeout`
- `rate_limited`
- `http_error`
- `anti_bot_or_blocked`
- `empty_response`
- `parse_error`
- `validation_error`
- `dependency_error`
- `unknown`

每条失败记录至少包含：`source`、`operation`、`error_code`、`retryable`、`attempts`、`elapsed_ms`、`checked_at`、安全的用户可见 message，以及不泄露 URL 参数/凭证的内部 detail。

不要把任意异常字符串直接返回给用户；不要吞掉异常后继续伪造成功。

### B. 超时、重试和速率边界

对来源调用建立显式策略：

- 连接/读取超时与总预算可配置；
- 只对明确的瞬时错误重试（网络错误、429、部分 5xx）；
- 不对 4xx、解析错误、资料校验失败、反爬拦截无限重试；
- 记录实际 attempts、退避等待和最终错误；
- 达到总预算后 fail-closed，不能继续联网；
- 保持现有来源的礼貌间隔，不新增绕过反爬的行为。

如果现有 `fetch_materials.py` 已有部分重试逻辑，优先抽象/复用，避免并行实现两套互相矛盾的策略。

### C. 源健康与新鲜度

为每个来源输出可查询的健康快照：

- `available` / `degraded` / `unavailable`；
- 最近检查时间、延迟、最近成功/失败；
- 错误码、重试次数、`retry_after`（如有）；
- 资料版本/抓取时间/年龄（能判断时）；
- 本次报告是否受该来源失败影响。

提供一个服务层/纯函数入口，必要时增加只读 API（例如 `GET /api/source-health`），但不要为了展示而伪造“健康”。没有真实检查时状态应为 `unknown`，不能默认 available。

### D. 失败影响范围和可解释降级

将来源故障映射为明确的业务影响：

- `source_unavailable`：没有该来源证据，不能回答依赖它的事实；
- `partial_evidence`：已有资料仍可支撑部分回答，必须显式说明缺口；
- `stale_data`：资料过期或新鲜度未知，回答带限制；
- `needs_review`：校验或来源冲突，需要人工复核；
- 不能用旧缓存/另一标的/未经校验的网络内容静默替代。

报告任务或问答响应至少要有稳定状态与 `degradation_reasons[]`，不要求本阶段改冻结工作流，但必须能在外层记录失败影响。

### E. 幂等与审计字段

对工具调用定义可测试的请求 fingerprint（source + operation + 标的 + 资料期间 + 关键输入规范化后计算），避免同一任务边界内重复调用。保留：`job_id`、`requested_by` 标签、source、operation、attempts、started_at、finished_at、result_status。

注意：`requested_by` 仍只是标签，不是权限凭证；不要声称实现了真实授权。

## 3. 测试要求

只做离线可重复测试，默认不访问真实网络：

1. 错误映射：timeout / 429 / 5xx / 4xx / anti-bot / empty / parse / validation；
2. 重试边界：可重试错误的最大次数、不可重试错误不重试、总预算 fail-closed；
3. 脱敏：用户响应不包含凭证、完整 URL 查询参数或内部堆栈；
4. 健康状态转换和 `retry_after`；
5. 新鲜度与 `stale_data`；
6. fingerprint 幂等；
7. API 错误状态与现有报告任务回归；
8. 原冻结测试与黄金回归。

如果增加 API 或前端，补对应 API/前端状态测试；真实网络、真实来源、浏览器 E2E 与离线 mock 必须分开报告，未运行的不能写已验证。

## 4. 不能做的事

- 不修改 `rag.py`、`workflow.py`、`llm_generation.py`、`safety.py`；
- 不把 R4 变成通用 Agent、MCP、多租户权限或自动交易建议；
- 不修改线上检索默认路径；
- 不把所有异常统一成 200/空列表；
- 不安装新依赖，除非先说明必要性并确认项目已有依赖无法满足；
- 不清理用户已有文件/目录；不动 `data/chroma`。

## 5. 验收

先跑新增/相关测试，再跑全量：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_source_governance.py tests/test_fetch_materials.py tests/test_api.py -q
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

如果文件名不同，汇报中列出实际命令。最终报告必须包含：

1. 改了什么 / 没改什么；
2. 错误枚举、超时重试和健康状态契约；
3. 幂等与审计字段；
4. 实际测试命令及结果；
5. 未验证的真实网络/浏览器路径；
6. 剩余风险；
7. 停止，不进入 R5。
