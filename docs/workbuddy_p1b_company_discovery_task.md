# WorkBuddy 任务卡：P1-B 公司候选解析与受控资料接入入口

> 项目：Investment_Assistant
> 工作目录：`D:\It\Test_Project\Investment_Assistant`
> 日期：2026-10-05
> 前置阶段：P1-A 数据源适配层已完成
> 目标：让用户询问未收录公司时，系统能够识别公司候选和市场状态，而不是直接返回模糊的“公司未收录”；但候选数据不能冒充官方年报证据。

## 一、开始前必须阅读

1. `bug.md`
2. `docs/data_source_upgrade_plan.md`
3. `docs/company_qa_onboarding.md`
4. `investment_assistant/market_data_sources.py`
5. `investment_assistant/company_onboarding.py`
6. `investment_assistant/company_qa.py`
7. `investment_assistant/chat_session.py`
8. `investment_assistant/source_governance.py`
9. `tests/test_market_data_sources.py`
10. `tests/test_chat_session.py`、`tests/test_company_qa.py`

## 二、禁止触碰范围

除非有明确变更说明和对照测试，不得修改：

- `investment_assistant/rag.py`
- `investment_assistant/workflow.py`
- `investment_assistant/llm_generation.py`
- `investment_assistant/safety.py`

不得执行：

- `git add -A`
- `git reset --hard`
- `git clean`
- 删除或覆盖用户已有未提交改动
- 自动安装 Baostock、AKShare 或其他依赖
- 未经授权的真实联网抓取
- 直接写入 `data/materials_manifest.json`
- 直接写入 `data/onboarded_materials.json`

## 三、当前已经存在的能力

P1-A 已提供：

- `BaoStockAdapter`
- `AKShareAdapter`
- `MarketDataSourceRegistry`
- `SourceObservation`
- 可选依赖懒加载
- `candidate / success / empty / unavailable` 状态
- 结构化错误和来源 provenance
- 离线 fake client 测试

不要重新定义一套平行的数据源结果结构。

## 四、本次编码目标

### 目标 1：新增统一的公司候选解析服务

建议新建：

```text
investment_assistant/company_discovery.py
```

如果现有代码已经存在等价模块，必须复用而不是重复创建。

服务至少提供：

```python
resolve_company_candidates(
    query: str,
    *,
    market: str | None = None,
    registry: MarketDataSourceRegistry | None = None,
) -> DiscoveryResult
```

结果必须包含：

- `status`：`matched` / `ambiguous` / `unresolved` / `source_unavailable`
- `query`
- `candidates`
- `selected_ticker`（没有唯一高置信候选时必须为 `None`）
- `source_observations`
- `warnings`
- `requested_market`

候选至少包含：

- `ticker`
- `company`
- `market`
- `source`
- `confidence` 或明确的匹配理由
- `verified`: 必须为 `False`，除非来自现有官方 manifest

### 目标 2：公司解析规则

必须覆盖：

- `阿里巴巴`
- `Alibaba`
- `腾讯`
- `Tencent`
- `0700.HK`
- `平安银行`
- `000001.SZ`

规则：

1. 现有 `company_qa.catalog()` 中的公司优先使用本地 manifest，不调用外部源。
2. 未收录公司才允许调用注入的 registry。
3. 多个来源得到相同 ticker 可以合并，但必须保留每个来源的 provenance。
4. 多个 ticker 或多个市场候选不能自动选一个，必须返回 `ambiguous`。
5. 不能因为 AKShare/Baostock 返回数据就把公司状态改成 `verified`。
6. 不能跨 ticker 合并财务数字。

### 目标 3：接入聊天路由，但保持证据边界

修改聊天入口时复用现有：

```text
route_message()
dispatch_message()
company_qa.catalog()
company_qa.references()
```

预期行为：

```text
给我一份2025年的腾讯财报
→ 现有已收录公司路径
→ 不调用外部候选源
→ 进入年报摘要任务
```

```text
给我一份2025年的阿里巴巴财报
→ 识别为 Alibaba 候选
→ 若没有官方资料：返回“已识别但资料尚未核验”
→ 不生成数字答案
→ 不混用腾讯或其他公司资料
```

```text
阿里巴巴收入怎么样
→ 如果市场不明确且存在多个候选：请求确认市场
→ 如果只有一个候选：返回 candidate 状态并说明需要官方年报接入
```

不得把所有失败继续统一显示为“无法确认公司”。至少区分：

- `COMPANY_NOT_ONBOARDED`
- `COMPANY_AMBIGUOUS_MARKET`
- `SOURCE_UNAVAILABLE`
- `OFFICIAL_MATERIAL_REQUIRED`

### 目标 4：提供受控 API 或内部服务入口

优先新增一个最小 API：

```text
POST /api/company-discovery
```

请求示例：

```json
{
  "query": "阿里巴巴",
  "market": "HK"
}
```

要求：

- 复用现有身份认证；
- 不允许客户端指定任意 URL；
- 不写正式资料清单；
- 返回结构化候选和来源状态；
- 外部源异常返回稳定错误码；
- 没有安装外部依赖时服务仍可启动。

如果现有 API 分层不适合新增端点，可以先实现内部服务和测试，并在交付说明中列出 API 延后理由；不得在前端复制一套解析逻辑。

## 五、测试要求

必须新增或补充离线测试，不能依赖真实 Baostock/AKShare 网络：

### 成功路径

- 本地已收录腾讯：直接命中 manifest，不调用外部源；
- fake AKShare 返回阿里巴巴候选：状态为 `matched` 或 `ambiguous`，但 `verified=False`；
- fake Baostock 返回平安银行：保留来源 provenance；
- 证券代码可规范化但不改变市场含义。

### 负面路径

- 两个市场返回多个 ticker：`ambiguous`；
- 外部源空结果：`unresolved` 或 `source_unavailable`；
- 外部源超时、格式错误、依赖缺失：结构化错误，不抛未处理异常；
- `阿里巴巴和腾讯收入`：拒绝跨公司混答；
- 候选状态不能调用现有年报回答服务返回财务数字；
- 尝试任意 URL 或写 manifest：必须被阻断；
- 已收录公司不能被外部源返回的别名覆盖或改名。

至少运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_market_data_sources.py tests/test_company_discovery.py tests/test_chat_session.py tests/test_company_qa.py
```

交付前再运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 六、交付格式

完成后必须汇报：

1. 实际修改文件的绝对路径；
2. 是否安装依赖；
3. 是否联网；
4. 是否修改冻结文件；
5. 定向测试结果；
6. 全量测试结果；
7. 仍未覆盖的边界；
8. 不要声称“已支持任意公司”，除非真实跑过候选解析、证据接入和负面测试。

不要自动提交 Git，保留当前工作区其他未提交修改。

## 七、审阅人验收重点

WorkBuddy 完成后，审阅时重点检查：

- 是否复用了 `SourceObservation`，有没有重复定义来源结果；
- 是否把 candidate 错当 verified；
- 是否发生跨 ticker 证据混合；
- 未安装外部库时项目能否导入和启动；
- 空结果和超时是否保留真实失败原因；
- `company_qa.catalog()` 是否仍然是已收录公司的身份权威；
- 阿里巴巴请求是否从“模糊拒答”变成“已识别但资料未核验”；
- 旧的 658 条回归是否全部保持通过。
