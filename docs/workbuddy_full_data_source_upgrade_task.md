# WorkBuddy 总任务卡：投研助手全量数据源接入闭环

> 项目：Investment_Assistant
> 工作目录：`D:\It\Test_Project\Investment_Assistant`
> 日期：2026-10-05
> 交付目标：一次完成“任意公司识别与资料接入”的完整业务闭环，不把任务停在 P1-B。
> 前置成果：P1-A 数据源适配层已经存在并通过 658 passed / 1 skipped。

---

## 0. 最终业务目标

用户输入：

```text
阿里巴巴2025年的收入怎么样
```

系统应当完成：

```text
识别公司与市场
  ↓
检查本地已收录资料
  ↓
未收录时调用 Baostock/AKShare 做候选发现
  ↓
候选结果不能冒充官方年报证据
  ↓
进入受控官方资料接入流程
  ↓
下载、校验、登记、建立索引和字段锚点
  ↓
基于已核验证据回答
```

如果某一步失败，用户必须看到具体状态：

- 公司未识别；
- 市场不明确；
- 公司已识别但官方资料未接入；
- 数据源暂不可用；
- 官方文件校验失败；
- 字段尚未建立证据锚点。

禁止把所有失败都显示成“无法确认公司”。

---

## 1. 必须先阅读的文件

```text
bug.md
docs/data_source_upgrade_plan.md
docs/company_qa_onboarding.md
docs/workbuddy_p1b_company_discovery_task.md
investment_assistant/market_data_sources.py
investment_assistant/company_onboarding.py
investment_assistant/company_qa.py
investment_assistant/chat_session.py
investment_assistant/web_app.py
investment_assistant/api.py
investment_assistant/source_governance.py
investment_assistant/market_data.py
tests/test_market_data_sources.py
tests/test_chat_session.py
tests/test_company_qa.py
```

不要重复实现已有能力，特别是：

- `SourceObservation`；
- `MarketDataSourceRegistry`；
- `company_qa.catalog()`；
- `company_qa.references()`；
- `company_onboarding.provision_approved()`；
- `source_governance` 的错误码和审计结构。

---

## 2. 禁止触碰范围

除非有明确变更依据、对照评测和失败回归，不得修改：

```text
investment_assistant/rag.py
investment_assistant/workflow.py
investment_assistant/llm_generation.py
investment_assistant/safety.py
```

不得：

- `git add -A`；
- `git reset --hard`；
- `git clean`；
- 删除工作区已有改动；
- 在默认测试中强制联网；
- 自动安装 Baostock、AKShare 或 MCP 依赖；
- 让外部数据源直接写 `materials_manifest.json`；
- 让外部数据源直接写 `onboarded_materials.json`；
- 把候选数据标记为 verified；
- 用其他公司的数据填充当前公司缺口。

---

## 3. 全量交付范围

### 3.1 公司候选解析

新建或复用统一服务，例如：

```text
investment_assistant/company_discovery.py
```

提供：

```python
resolve_company_candidates(
    query: str,
    *,
    market: str | None = None,
    registry: MarketDataSourceRegistry | None = None,
) -> DiscoveryResult
```

覆盖：

- 阿里巴巴 / Alibaba；
- 腾讯 / Tencent / 0700.HK；
- 平安银行 / 000001.SZ；
- 宁德时代 / 300750.SZ；
- 贵州茅台 / 600519.SS；
- 未收录公司；
- 港股/美股/A股市场歧义。

结果必须包括：

```json
{
  "status": "matched | ambiguous | unresolved | source_unavailable | onboarded",
  "query": "阿里巴巴",
  "selected_ticker": null,
  "candidates": [],
  "source_observations": [],
  "warnings": [],
  "requested_market": "HK"
}
```

候选必须带：

- ticker；
- 公司名；
- 市场；
- source；
- confidence 或匹配理由；
- `verified`；
- provenance。

本地 manifest 已收录公司优先，不调用外部源。

---

### 3.2 Baostock/AKShare 真实适配

复用现有：

```text
investment_assistant/market_data_sources.py
```

要求：

- 可选依赖懒加载；
- 没安装依赖时项目仍可启动；
- 超时、登录失败、空响应、字段漂移和格式错误结构化返回；
- 所有调用记录 source、operation、ticker、耗时、错误码和 provenance；
- 默认测试不联网；
- 真实联网必须由显式配置开启；
- 不允许通过任意 URL 抓取未知网页。

Baostock主要承担 A 股候选、行情和结构化财务数据；AKShare补充多市场候选和结构化数据。两者都不能直接替代官方年报证据。

---

### 3.3 官方资料接入闭环

复用现有：

```text
investment_assistant/company_onboarding.py
```

完善为：

```text
候选公司
  ↓
管理员/受控策略确认市场和年度
  ↓
官方来源白名单
  ↓
官方 PDF/HTML 下载
  ↓
SHA256、页数、文件格式、文本层校验
  ↓
onboarded_materials.json
  ↓
建立中文/英文索引
  ↓
字段锚点登记
  ↓
状态 onboarded
```

必须实现：

- 阿里巴巴至少有一条可审计的 onboarding 路径；
- 港股优先 HKEX；美股优先 SEC；A股优先 CNINFO；
- 不接受用户直接传入任意 URL；
- 资料下载失败不改变正式 manifest；
- 下载成功但字段未核验时，状态只能是 `onboarded_material_only`，不能回答财务数字；
- 只有字段锚点通过后，状态才允许进入 `verified_field_available`。

如果无法在当前环境安全完成真实下载，必须实现完整流程、fixture 和明确的人工审批待办，不得伪造“阿里巴巴已完成接入”。

---

### 3.4 聊天路由升级

修改：

```text
investment_assistant/chat_session.py
investment_assistant/company_qa.py
```

复用现有路由，不在前端复制公司识别。

预期：

```text
给我一份2025年的腾讯财报
→ 本地已收录
→ 年报摘要任务
→ 不调用外部候选源
```

```text
给我一份2025年的阿里巴巴财报
→ 识别阿里巴巴和市场候选
→ 未完成官方证据接入时
→ 返回“已识别但官方年报尚未核验”
→ 不生成数字答案
```

```text
阿里巴巴2025年收入怎么样
→ 若字段已核验：回答数值、单位、比较期、来源
→ 若资料已接入但字段未核验：说明字段缺口
→ 若市场不明确：询问港股/美股
```

错误码至少区分：

- `COMPANY_NOT_FOUND`
- `COMPANY_NOT_ONBOARDED`
- `COMPANY_AMBIGUOUS_MARKET`
- `OFFICIAL_MATERIAL_REQUIRED`
- `FIELD_NOT_VERIFIED`
- `SOURCE_UNAVAILABLE`
- `CROSS_TICKER_QUERY`
- `REPORT_PERIOD_UNSUPPORTED`

---

### 3.5 API 和前端闭环

优先新增或完善：

```text
POST /api/company-discovery
POST /api/company-material-requests
GET  /api/company-coverage
GET  /api/source-health
```

要求：

- 复用现有 Bearer 身份认证；
- 普通用户可查询候选；
- 资料接入只能由 analyst/admin 或受控服务调用；
- 前端显示“已识别 / 已核验 / 待接入 / 来源不可用”；
- 不显示内部堆栈；
- 查看依据中显示来源、期间、抓取时间、文件 SHA 或未核验说明；
- 不让用户手动选择底层语料作为正常使用前置条件。

聊天框应能正确展示：

```text
意图：年报事实 / 年报摘要 / 公司发现
来源：AKShare / BaoStock / CNINFO / HKEX / SEC
状态：候选 / 已核验 / 待接入 / 受控拒答
```

---

### 3.6 MCP 工具边界

如果当前环境没有 MCP SDK，不要安装并强耦合。先实现内部工具函数和稳定 schema：

```text
resolve_company_candidates
get_market_snapshot
get_structured_financial_candidates
get_source_health
request_official_material_onboarding
```

每个工具必须：

- 有输入 schema；
- 有输出 schema；
- 有权限检查；
- 有超时；
- 有结构化错误；
- 有审计记录；
- 不允许写正式 manifest；
- 不允许执行任意 URL；
- 不允许把 candidate 提升为 verified。

若确实能在不增加不必要依赖的情况下暴露 MCP server，再提供可选入口；否则完成内部 tool boundary 即可，并写明 MCP 暴露延后原因。

---

## 4. 必须测试的矩阵

### 正向

- 本地已收录腾讯：不调用外部源；
- 阿里巴巴 fake candidate：返回 candidate，不是 verified；
- 阿里巴巴官方 fixture onboarding：成功写入独立 onboarded 文件；
- 资料已接入但字段未锚定：拒绝数字回答并说明缺口；
- 字段锚点通过后：返回数值、单位、期间和来源；
- 港股/美股/A股代码规范化；
- API 返回结构和 UI 可展示字段完整。

### 负向

- 两市场同名或多 ticker：ambiguous；
- 阿里巴巴和腾讯混问：cross ticker 拒绝；
- 外部源超时、空结果、依赖缺失、字段改变：结构化错误；
- 任意 URL 注入：拒绝；
- SHA 漂移、页数漂移、报告年度漂移：fail-closed；
- 候选数据不能直接调用年报事实服务返回数字；
- 旧报告不能被新公司问题复用；
- 外部源失败不能混用其他公司缓存；
- 未安装 AKShare/Baostock 时项目仍可导入和启动。

---

## 5. 测试命令

先跑新增和关键路径：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_market_data_sources.py `
  tests/test_company_discovery.py `
  tests/test_chat_session.py `
  tests/test_company_qa.py `
  tests/test_company_onboarding.py
```

再跑全量：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

必须报告：

- 通过数量；
- 跳过数量；
- 失败测试；
- 是否联网；
- 是否安装依赖；
- 是否修改冻结区；
- 是否写入正式资料清单；
- 阿里巴巴是否真的完成官方证据接入。

---

## 6. 交付限制

- 不自动提交 Git；
- 不执行 `git add -A`；
- 不删除现有工作区修改；
- 不声称“已支持任意公司”，除非每一层都有测试证据；
- 不能用“测试全绿”掩盖未运行的真实外部源或 UI smoke；
- 如果真实联网条件不足，要明确标记“代码完成、实源验证待授权/待配置”。

---

## 7. 审阅重点

WorkBuddy 完成后，Codex 独立审阅：

1. 对照本任务卡逐项检查，不接受只完成候选解析；
2. 复现阿里巴巴、腾讯、平安银行和多公司混问；
3. 重放旧证明、篡改 SHA、空源、超时和跨 ticker 攻击路径；
4. 检查正式 manifest 是否被非授权写入；
5. 检查未安装可选依赖时的导入和启动；
6. 运行全量测试；
7. 必要时直接修复，不要求 WorkBuddy 返工；
8. 最终只汇报当前真实完成范围，不夸大为“任意公司可查”。
