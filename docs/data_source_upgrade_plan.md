# 数据源接入升级方案：Baostock + AKShare + 官方年报证据

> 版本：P1 方案草案
> 日期：2026-10-05
> 目标：让用户询问未收录公司时，系统能够先识别公司、发现候选数据源，再决定“直接回答、进入资料接入、还是明确拒答”，不再把所有未知公司都归为同一种错误。

## 1. 先定边界：两个库不能直接替代官方年报证据

### Baostock

定位为 A 股结构化数据源：证券基础信息、行业、交易日历、历史 K 线、季频财务指标、分红和公司报告等。它适合做：

- A 股公司名称与代码候选解析；
- 行情、行业和财务指标候选数据；
- 对新公司是否存在可查询标的的快速预检。

它不作为年报原文、页码、币种和表格证据的唯一来源；会话登录、数据空值和字段语义必须记录。

### AKShare

定位为多市场、多站点的结构化数据接口集合，可覆盖 A 股、港股、美股、基本面、行情、新闻和宏观数据。它适合做：

- A/H/US 公司候选识别和市场代码补全；
- Baostock 不覆盖的港股、美股、市场摘要和候选财务字段；
- 资料发现线索和回答实时行情类问题的候选数据。

AKShare 官方说明明确提示：接口依赖上游公开站点，接口可能变化、数据新鲜度需要核验，数据仅供研究参考。因此输出必须带来源、抓取时间、统计期间、单位和质量状态。

### MCP

目前搜索到的 AKShare MCP/BaoStock MCP 主要是第三方项目，不应直接作为本项目的可信证据层。建议顺序是：

1. 先在本项目内部实现统一的 Python 数据源适配器；
2. 通过结构化结果和权限校验接入聊天路由；
3. 最后再用 MCP 暴露少量白名单工具，作为工具调用协议，不让 MCP 直接写入正式资料清单。

## 2. 目标架构

```text
用户问题
  ↓
意图路由：事实 / 年报摘要 / 行情 / 研究 / 敏感问题
  ↓
公司解析器：名称、别名、市场、证券代码、年度
  ↓
Source Registry
  ├─ 本地官方年报 manifest：可直接回答证据事实
  ├─ BaoStock：A 股身份与结构化数据候选
  ├─ AKShare：A/H/US 身份与结构化数据候选
  └─ 官方资料发现器：CNINFO / HKEX / SEC 固定接入流程
  ↓
Source Arbitration
  ├─ verified：绑定官方文件 SHA、页码、字段锚点
  ├─ candidate：数据源发现结果，需接入审核
  ├─ stale：存在但时间不满足问题期间
  ├─ conflict：多源数值冲突，拒绝自动合并
  └─ unavailable：超时、空结果、格式错误或上游不可用
  ↓
受控回答 / 资料接入任务 / 明确澄清
```

## 3. 关键数据契约

所有外部数据源必须转换成同一结构，禁止把 DataFrame 或任意 JSON 直接交给模型：

```json
{
  "source": "akshare",
  "operation": "resolve_company",
  "status": "candidate",
  "retrieved_at": "2026-10-05T00:00:00Z",
  "ticker": "9988.HK",
  "company": "阿里巴巴",
  "market": "HK",
  "data": {},
  "provenance": {
    "provider": "upstream-provider-name",
    "endpoint": "documented-function-name",
    "source_url": "https://...",
    "as_of": "2025-12-31",
    "raw_sha256": null
  },
  "warnings": ["candidate_not_official_annual_report"]
}
```

强制规则：

- `candidate` 不能被回答层当作 `verified`；
- 没有 ticker、期间、单位、来源和抓取时间时不能交付数字；
- 多源冲突必须返回 `conflict`，不做静默平均；
- 任何外部源都不能直接写 `materials_manifest.json` 或 `onboarded_materials.json`；
- 正式入库仍走现有 `company_onboarding.py` 的官方 URL、SHA、页数和 PDF 校验。

## 4. 用户问任意公司时的正确流程

### 已收录公司

```text
腾讯 2025 年收入怎么样
→ 本地 manifest
→ 中文/英文证据服务
→ 返回数字、单位、同比和官方页码
```

### 未收录但可解析公司

```text
阿里巴巴 2025 年收入
→ Baostock/AKShare 得到公司和市场候选
→ 状态 candidate
→ 检查官方年报接入能力
→ 未完成审核前：说明“已识别但资料尚未核验”
→ 不拿候选数值直接冒充年报事实
```

### 用户没有指定市场

```text
阿里巴巴收入怎么样
→ 如果只有一个高置信候选：展示候选并询问确认
→ 如果同时存在港股/美股候选：询问“港股还是美股”
→ 不让模型自行选择并混用两地数据
```

### 数据源失败

```text
AKShare 超时 / BaoStock 登录失败 / 返回空表
→ 结构化 source_unavailable
→ 尝试另一来源但保留失败原因
→ 全部失败时告诉用户“公司已识别，但数据源暂不可用”
```

## 5. 实施分期

### P1-A：适配器和错误契约

- 新建 `market_data_sources.py`；
- Baostock、AKShare 懒加载；
- 支持 fake client 离线测试；
- 覆盖异常、超时、空结果、格式错误和候选状态；
- 不联网、不改正式 manifest。

### P1-B：公司解析与候选接入

- 将适配器接到公司解析路由；
- 支持中文名、英文名、A/H/US 代码；
- 统一返回 `candidate / verified / unavailable`；
- 未收录公司改为“已识别但尚未核验”，不再泛化为“无法确认公司”。

### P1-C：官方年报接入

- A 股优先 CNINFO；
- 港股优先 HKEX；
- 美股优先 SEC；
- 适配器只发现线索，正式资料仍通过白名单和 SHA 校验；
- 阿里巴巴作为第一个真实 onboarding 样本。

### P1-D：实时/结构化问答

- 行情、估值、行业、财务候选数据和年报事实分开；
- 每类问题显示不同来源和时间；
- “实时行情”不再从年报回答；
- “年报收入”不再由行情接口替代。

### P1-E：MCP 暴露

只开放白名单工具：

- `resolve_company_candidates`
- `get_market_snapshot`
- `get_structured_financial_candidates`
- `get_source_health`
- `request_official_material_onboarding`

MCP 工具必须有参数 schema、超时、权限、审计记录和禁止写正式 manifest 的硬约束。

## 6. 验收标准

### 功能

- 阿里巴巴、腾讯、平安银行、宁德时代等公司名称都能进入公司解析；
- 代码、中文名、英文名和市场不混淆；
- 未收录公司能返回候选状态而不是模糊拒答；
- 已核验公司仍沿用现有证据回答链路。

### 安全

- 0 条跨 ticker 来源污染；
- candidate 数据不能直接生成 verified 回答；
- 外部数据源异常不能导致旧资料混答；
- 外部工具不能写正式资料清单；
- 超时、空表、字段漂移都能被审计。

### 可观测性

每次外部调用至少记录：

- source；
- operation；
- ticker/company；
- requested period 与 as_of；
- latency；
- status；
- error_code；
- provenance；
- 是否进入官方资料接入流程。

## 7. 本轮结论

现在不建议直接把第三方 AKShare MCP 或 BaoStock MCP 接到大模型自由调用。正确做法是：

- 先做本地适配器和统一数据契约；
- 再做公司候选解析；
- 再接官方年报接入；
- 最后把少数安全工具暴露成 MCP。

这样既能逐步覆盖“用户问哪家公司就找到哪家公司”，又不会为了扩大覆盖面牺牲证据可信度。

## 参考资料

- AKShare 官方仓库：https://github.com/akfamily/akshare
- AKShare 官方接口说明与数据风险说明：https://github.com/akfamily/akshare/tree/main/docs
- BaoStock 官方仓库：https://github.com/zxygithub/baostock
- BaoStock API 文档入口：https://www.baostock.com/
- 第三方 AKShare MCP 示例，仅作协议参考，不作为本项目可信来源：https://github.com/jadenmong/akshare-mcp-server
