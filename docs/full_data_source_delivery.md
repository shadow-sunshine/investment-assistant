# 全量数据源接入闭环：交付说明与真实边界

> 项目：Investment_Assistant
> 日期：2026-10-05
> 对应任务卡：`docs/workbuddy_full_data_source_upgrade_task.md`
> 前置基线：P1-A 数据源适配层，658 passed / 1 skipped

## 一句话结论

代码层的"任意公司识别 → 候选发现 → 受控接入 → 字段锚点 → 受控回答"闭环**已经全部实现并有测试覆盖**；
但**阿里巴巴的真实官方年报证据尚未接入**，系统当前对阿里一律不给数字。

---

## 1. 交付范围对照

| 任务卡章节 | 状态 | 落点 |
|---|---|---|
| 3.1 公司候选解析 | 完成 | `investment_assistant/company_discovery.py` |
| 3.2 Baostock/AKShare 真实适配 | 完成（懒加载、默认不联网） | `investment_assistant/market_data_sources.py` |
| 3.3 官方资料接入闭环 | 完成（HKEX / CNINFO / SEC 三通道 + 状态机） | `investment_assistant/company_onboarding.py` |
| 3.4 聊天路由升级 | 完成 | `investment_assistant/chat_session.py` |
| 3.5 API 与前端闭环 | 完成 | `investment_assistant/api.py`、`investment_assistant/web_app.py` |
| 3.6 MCP 工具边界 | 完成内部边界，MCP 暴露延后 | `investment_assistant/research_tools.py` |

### 3.1 公司候选解析

```python
resolve_company_candidates(query, *, market=None, registry=None, allow_external=True) -> DiscoveryResult
```

状态：`matched` / `ambiguous` / `unresolved` / `source_unavailable` / `onboarded`
错误码：`COMPANY_NOT_FOUND` / `COMPANY_NOT_ONBOARDED` / `COMPANY_AMBIGUOUS_MARKET` /
`OFFICIAL_MATERIAL_REQUIRED` / `FIELD_NOT_VERIFIED` / `SOURCE_UNAVAILABLE` / `CROSS_TICKER_QUERY` /
`REPORT_PERIOD_UNSUPPORTED` / `COMPANY_CATALOG_UNAVAILABLE` / `COMPANY_INPUT_INVALID`

不变量：

- 本地 `company_qa.catalog()` 命中即 `onboarded`，**不调用任何外部源**（有测试用"会记录调用的 fake adapter"证明）；
- 除官方清单命中外，所有候选 `verified=False`，`evidence_level="candidate"`；
- 多 ticker 或多市场一律 `ambiguous`，`selected_ticker=None`；
- 裸 6 位代码（如 `000001`）不猜交易所：先查本地清单，查不到再问路由提示，仍不确定就保持歧义；
- 入参只接受公司名/代码 + 市场枚举，**含 `://` 的输入直接拒绝**。

### 3.2 数据源适配

新增 `fetch_market_snapshot` 与 `fetch_structured_financials`（BaoStock / AKShare），
以及 `MarketDataSourceRegistry._dispatch` 统一登记健康与审计。
`record_observation()` 把每次调用写入 `SourceHealthRegistry` 与 `ToolCallLedger`。

`source_health_snapshot()` 合并"已配置"与"真实调用健康"——**从未真实检查过的来源保持 `unknown`，不伪造 available**。

### 3.3 官方资料接入闭环

三通道白名单（市场与官方渠道强绑定，错配直接拒）：

| 市场 | 渠道 | URL 形态 |
|---|---|---|
| A 股 | CNINFO | `https://static.cninfo.com.cn/finalpage/YYYY-MM-DD/NNNNNNN.PDF` |
| 港股 | HKEX | `https://www1.hkexnews.hk/listedco/listconews/sehk/YYYY/MMDD/NNNNN.pdf` |
| 美股 | SEC | `https://www.sec.gov/Archives/edgar/data/.../*.htm` |

闭环步骤：

```text
request_onboarding()          # 登记申请台账，不联网、不写正式清单
  ↓
（人工把官方 URL + SHA256 + 页数写入 company_onboarding_allowlist.json）
  ↓
provision_approved()          # 白名单校验 → 下载 → SHA/页数/格式/文本层/公司名 → 写 onboarded_materials.json
  ↓  状态 = onboarded_material_only（不能回答财务数字）
register_field_anchor()       # 字段锚点登记（重读真实字节防替换）
  ↓  状态 = verified_field_available
```

新增拒绝项（都有测试）：市场与渠道错配、URL 带 query/凭证、非 https、主机非白名单、
后缀不符、PDF 损坏（`pdf_unreadable`）、公司名不在前三页、年份漂移、重定向、审批中途被撤销。

### 3.4 聊天路由

未收录公司不再统一显示"无法确认公司"，改为按真实状态区分：

| 场景 | 系统回答 |
|---|---|
| `给我一份2025年的腾讯财报` | 走已收录年报摘要任务，不调外部源 |
| `阿里巴巴2025年的收入怎么样` | 「已识别公司「阿里巴巴」，但存在多个市场候选：9988.HK（HK）、BABA（US）…不会自行选择」 |
| `阿里巴巴` + 指定 HK | 「已识别候选标的 9988.HK，但官方年报证据尚未接入」 |
| 贵州茅台（资料已入库、字段未锚定） | 「官方资料已入库，但该字段尚未建立证据锚点」 |
| 外部源不可用 | 「数据源暂时不可用…不会用其他公司的资料替代」 |

跨 ticker 仍然拒绝；不沿用旧报告作答（`CROSS_TICKER_QUERY`）。

### 3.5 API 与前端

新增端点（全部复用现有 Bearer 认证，`analyst`/`admin` 可用）：

```text
POST /api/company-discovery              # 候选解析（默认不联网）
POST /api/company-market-snapshot        # 结构化行情
POST /api/company-financial-candidates   # 结构化财务候选（verified 恒为 false）
GET/POST /api/company-onboarding-requests# 接入申请台账（写操作仅 admin）
GET  /api/company-evidence-state         # 覆盖 + 问句 → 稳定错误码
GET  /api/research-tools                 # 白名单工具目录
GET  /api/source-health                  # （原有，admin）
```

前端：侧栏新增「公司发现」面板；`_show_chat_answer` 展示意图/来源/状态/错误码与候选列表；
`_show_source_health` 展示来源健康与工具边界；覆盖清单改用证据状态标签。

### 3.6 MCP 边界

当前环境**未安装 MCP SDK**（`import mcp` → `ModuleNotFoundError`），任务卡禁止自动安装依赖，
因此只交付与传输层无关的内部工具边界。将来接 MCP server 时这些函数可直接作为 handler。

5 个白名单工具，每个都有输入 schema、输出 schema、权限检查、超时预算、审计记录：

```text
resolve_company_candidates           analyst/admin
get_market_snapshot                  analyst/admin
get_structured_financial_candidates  analyst/admin
get_source_health                    analyst/admin
request_official_material_onboarding admin only
```

硬约束（`TOOL_SPECS` 中 `writes_formal_manifest` 恒为 `False`）：
不写正式清单、不执行任意 URL（`FORBIDDEN_INPUT_KEYS` + `://` 检测）、不把 candidate 提升为 verified。

---

## 2. 阿里巴巴：真实状态（如实记录）

**未接入。** 三项可核查证据：

1. `data/materials_manifest.json` 无 `9988.HK` / `BABA`；
2. `data/onboarded_materials.json` 为 `{}`；
3. `data/company_onboarding_allowlist.json` 为 `{}`（无任何阿里的预审 URL/SHA/页数）。

当前系统对阿里能做到的事：

```text
POST /api/company-discovery {"query": "阿里巴巴"}
→ status=ambiguous, error_code=COMPANY_AMBIGUOUS_MARKET,
  candidates=[9988.HK(HK, verified=false), BABA(US, verified=false)],
  answerable=false
```

系统**不会**做的事：给出任何阿里收入数字；用腾讯数据替代；把候选标记为已核验。

要把阿里真正接入，需要人工完成（无法由代码代劳）：

1. 确认主体与年度：港股 `9988.HK`（HKEX 20-F/年度业绩公告）或美股 `BABA`（SEC 20-F）；
2. 从 HKEX / SEC 取得官方文件，落地后计算 `sha256` 与总页数；
3. 确认公司全称（必须出现在港股 PDF 前三页文本层内）；
4. 写入 `data/company_onboarding_allowlist.json` 的 `9988.HK:2025` 或 `BABA:2025`；
5. 调用 `POST /api/company-material-requests` 入库 → 状态 `onboarded_material_only`；
6. 人工核对收入所在表、单位、期间、页码后调用 `register_field_anchor()` → `verified_field_available`。

以上 1–4 步**未执行**，因此第 6 步无法进行。

---

## 3. 测试

```powershell
# 定向（任务卡第 5 节 + 本轮新增）
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_market_data_sources.py `
  tests/test_company_discovery.py `
  tests/test_chat_session.py `
  tests/test_company_qa.py `
  tests/test_company_onboarding.py

# 全量
.\.venv\Scripts\python.exe -m pytest -q
```

新增测试文件：

| 文件 | 覆盖 |
|---|---|
| `tests/test_company_discovery.py` | 候选解析、代码规范化、歧义、失败结构化、工具权限、API |
| `tests/test_company_onboarding.py` | 白名单、SHA/页数/年份漂移、损坏文件、状态机、锚点、权限 |
| `tests/test_market_data_sources_extended.py` | 行情/财务适配、健康与审计登记、空结果语义、依赖缺失 |
| `tests/test_alibaba_end_to_end.py` | 阿里全链路 + 明确断言"当前未接入且不给数字" |

全部为**离线测试**：使用本地 fixture 与 fake client，不发起真实网络请求。

---

## 4. 未验证 / 已知缺口

| 项 | 说明 |
|---|---|
| 真实 Baostock/AKShare 联网 | **未验证**。两个包均未安装，且任务卡禁止自动安装。适配层只经 fake client 验证。 |
| MCP server 暴露 | **延后**。无 SDK；只交付内部工具边界。 |
| UI smoke | **未做**。Streamlit 端到端点击未实际运行；前端改动为静态代码审阅 + 逻辑对齐。 |
| 阿里巴巴官方证据 | **未接入**，见第 2 节。 |
| 阿里字段锚点 | 依赖上一步，当前不存在。 |
| 中文年报语料索引 | 本轮未改动 `rag.py` / 冻结区，索引能力沿用现状。 |

## 5. 冻结区

未修改：`rag.py`、`workflow.py`、`llm_generation.py`、`safety.py`。
未执行：`git add -A` / `git reset --hard` / `git clean`；未提交 Git。
