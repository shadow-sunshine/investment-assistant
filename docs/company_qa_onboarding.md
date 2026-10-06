# 公司问答与资料接入（当前边界）

聊天框会从 `data/materials_manifest.json`、`data/issuer_materials.json` 和
`data/onboarded_materials.json` 识别公司与年度；用户无须选择语料。API `POST
/api/company-answers` 仅接受 `ticker` 和 `question`，身份从 Bearer token 推导。
`GET /api/company-coverage` 返回已登记资料、年度及问答覆盖范围。

当前可核验的路径：四家中文年报沿用中文证据服务；网易 2025 年收入、股数、证券代码沿用 SEC 固定快照；腾讯 2025 年收入锁定披露易官方年报 PDF 第 130 页的全年合并利润表（人民币百万元，2025 列 751,766，2024 对比列 660,257），不能把第 12/13 页的 Q4 数值用于全年回答。其他已登记公司或字段未必有证据锚点，缺口时必须拒答；实时行情、季度与买卖建议不从年报推断。中文原始 PDF 的部分字符提取损坏，宁德时代收入单位使用人工核对的固定 PDF 页及 SHA 标注。

未收录公司不能由用户传任意网址自动联网。管理员先核对公司身份、报告年度、官方 PDF、全文 SHA256 和总页数，再把 `data/company_onboarding_allowlist.json` 中的 `{ticker}:{year}` 项写成：

```json
{
  "1234.HK:2025": {
    "approved": true,
    "market": "HKEX",
    "company": "经预审的公司全名（需出现在 PDF 前三页）",
    "report_date": "2025",
    "source_url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/预审的文件.pdf",
    "sha256": "官方 PDF 的 64 位小写十六进制摘要",
    "page_count": 100
  }
}
```

`POST /api/company-material-requests` 请求体 `{ "ticker": "1234.HK", "year": "2025" }`，只允许预审清单的 CNINFO（`.SZ/.SS`）或 HKEX（`.HK`）固定路径；下载禁止重定向、限制 25 MB、验 PDF 格式/页数/摘要。清单未预审返回 `not_approved`，失败不更新资料清单。成功只写入独立的 `data/onboarded_materials.json`，不改变四家中文语料的冻结版本；**入库不代表字段可回答**，须再核对指标、期间、币种、单位及页码并建立证据锚点。美国 SEC 新公司、任意网页与实时搜索当前没有受控接入，须另行设计审核流程。

预审清单由服务端文件权限保护，不向聊天客户端开放编辑；新公司 PDF 在 `data/knowledge_base/`，该目录 PDF 不提交 Git。要在新环境展示问答，需按原清单复原且通过 SHA/页数校验。运行 `\.venv\Scripts\python.exe -m pytest tests/test_company_qa.py -q` 验证已覆盖证据与负面路径。

## 公司发现与接入状态机（2026-10-05 补充）

聊天框现在会区分"已识别 / 待接入 / 待确认市场 / 来源不可用"，不再把所有未知公司统一显示为"无法确认公司"。
解析逻辑集中在 `investment_assistant/company_discovery.py`，候选状态与错误码见 `docs/full_data_source_delivery.md`。

接入流程分三步，每一步的状态都由本地清单与字段锚点决定，不由调用方声明：

```text
request_onboarding(ticker, year)        # 只写申请台账，不联网、不写正式清单
  → POST /api/company-onboarding-requests（仅 admin）
provision_approved(ticker, year)        # 白名单 → 下载 → SHA/页数/格式/文本层/公司名 → onboarded_materials.json
  → POST /api/company-material-requests（analyst/admin）
  → 状态 onboarded_material_only：资料已入库，但财务问答继续拒答
register_field_anchor(ticker, field, spec, approved_by, expected_sha256)
  → 状态 verified_field_available：此时才允许回答该字段数字
```

官方来源按市场强绑定，错配直接拒绝：港股 `HKEX`（`www1.hkexnews.hk/listedco/listconews/sehk/...`）、
A 股 `CNINFO`（`static.cninfo.com.cn/finalpage/...`）、美股 `SEC`（`www.sec.gov/Archives/edgar/data/...`）。

`POST /api/company-discovery` 供普通 analyst 查询候选，`use_external_sources` 默认 `false`（不联网）。
**阿里巴巴当前尚未接入**：`onboarded_materials.json` 与 `company_onboarding_allowlist.json` 均为空，
系统对阿里只返回候选（`9988.HK` / `BABA`，`verified=false`），不提供任何收入数字。
