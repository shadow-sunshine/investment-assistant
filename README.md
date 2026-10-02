# 智能投研助手

**输入一个股票代码,几分钟内得到一份基于真实数据、证据可溯源、风险已披露的研究简报。**

一个面向学习与作品集展示的可追溯投研工作流项目。核心理念:AI 生成投研内容最大的问题不是"写不出来",而是**不可信**——数字可能是编的、引用可能是假的、失败可能被掩盖。本项目用工程手段而非 prompt 约束来解决这个问题。

## 面试演示：两条受控路径

- **中文官方年报问答**：侧栏绑定单一标的（贵州茅台、宁德时代、平安银行、五粮液），在聊天框提问；后端从对应 2025 年年报中召回证据，校验字段、数值、期间和单位，返回回答或明确拒答，并展示 `ticker + 文件名 + SHA256 + 页码`。这不是开放域聊天，也不做投资建议。
- **研究报告工作流**：异步任务显示七步进度；报告有数据来源、风险与降级原因。未审核发布的报告不可对外问答，审核、发布与撤回由不同角色控制（单机演示级）。

### 从零运行中文问答（Windows PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-semantic.txt
.\.venv\Scripts\python.exe scripts\prepare_chinese_corpus.py
.\.venv\Scripts\python.exe -m investment_assistant.cli fetch --ticker 600519.SS
# 首次联网缓存固定版本的多语模型；问答服务运行时只读本地模型。
.\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', revision='e8f8c211226b894fcb81acc59f3b34ba3efd5f42')"
$env:IA_AUTH_TOKENS = '{"local-demo-token-change-me":{"actor":"analyst","tenant":"demo","roles":["analyst"]}}'
.\start_demo.bat
```

浏览器打开 `http://127.0.0.1:8501`，用本地凭证登录，侧栏选“中文年报问答”，例如问“宁德时代2025年营业收入是多少？”。示例 token **仅限本机演示，不得用于公网服务**。若缺 PDF、模型或 SHA256 不匹配，接口会拒绝交付而非猜测；官方 PDF 不随仓库重发。完整报告演示还依赖实时数据、检索索引和人工审核发布，不能用未发布报告冒充可问答内容。

### 可复核的边界与指标

- 中文召回**开发集** 26 题：Page Recall@4 为 **88.46%**、Top-1 为 **50%**、跨标的污染 0；不是跨期间/跨资料的生产指标。
- 17 题问答集修复后**回归**：13/13 数值与引用身份核验、4/4 拒答；该集曾用于调试，**不是独立盲测，也不能声称“幻觉率为零”**。
- 早期四象限双语 32 题基线很低，英语/中文跨语泛化仍是未解决问题；中文官方年报受控问答与默认报告 RAG 是两条路径，不能把开发集指标套到后者。
- 冷启动需加载多份 PDF 与本地模型，字段覆盖有限；跨页表头、复杂列语义会保守拒答。详见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。


| | |
|---|---|
| 工作流编排 | LangGraph 七节点状态机,全程输出机器可读审计记录 |
| 真实数据 | yfinance 实时行情 / 财报 / 估值 / 新闻,记录来源与抓取时间 |
| 证据溯源 | 本地 RAG(Chroma),PDF 页码级引用,ticker 隔离检索 |
| 防幻觉 | 受控 LLM 三道防线:槽位模板 → 引用校验 → 安全校验回退 |
| 质量评估 | 离线评测集、自动化回归与黄金样本；开发集/回归集指标明确区分 |

> 仅用于学习和研究,不构成投资建议。数据来自非官方接口,可能延迟、缺失或被修订,所有结果必须回到原始来源复核。

---

## 为什么做这个项目

通用大模型直接生成投研内容,有三个绕不开的信任问题:

1. **数字幻觉**——模型会把"净利润"的数值说成"经营现金流"(本项目实测拦截过的真实案例:10-K 第 36 页,净利润 112,010 与经营现金流 111,482 仅差零头,引用页和格式全对,事实映射却是错的);
2. **引用不可溯**——"据说营收增长 X%"无法翻回原文核对;
3. **失败被掩盖**——数据抓不到时,系统编一个数字出来,比"承认查不到"危险得多。

本项目的回答是:**把"找资料"(RAG)、"用资料"(受控生成)、"兜底"(规则校验)分成三段,各司其职,谁也不能替代谁。** 任何一段失守,内容都进不了最终报告。

## 架构:七节点可审计流水线

```text
用户输入 ticker
      │
      ▼
① collect_real_data ── 实时抓取行情/财报/估值/新闻(yfinance),记录来源与时间
      ▼                                   ┌─ 本地知识库(Chroma)
② retrieve_evidence ── RAG 检索 + 混合重排 ┘  PDF 页码级引用,ticker 隔离
      ▼
③ model_market ────── 市场建模:波动率/最大回撤 → 趋势与风险等级初判
      ▼
④ reason_scenarios ── 情景推理:积极/基准/压力(纯规则,确定可复现)
      ▼
⑤ controlled_generation ─ 受控 LLM 叙述(槽位模板,数字由程序注入)
      ▼
⑥ generate_report ─── 组装 Markdown 报告 + JSON 审计文件
      ▼
⑦ validate_risks ──── 风险终审:不过则回退规则版,并披露原因
```

两种数据源分层呈现、互不竞争:实时行情陈述**事实**(进数据快照),本地研报提供**观点背景**(进证据区,带页码与时间戳),系统不替用户做买卖决策。

### 受控 LLM 三道防线

1. **槽位模板**:LLM 只输出叙述框架,如"经营活动现金流为 {operating_cash_flow} 百万美元"——数字、日期由程序从结构化快照注入,模型笔下没有数字;
2. **引用存在性校验**:叙述中每个 `[Sx]` 必须真实存在于本次检索结果,编造引用直接拒绝;
3. **生成后安全校验**:全文检查数字溯源、口径混淆(总营收≠服务收入、自由现金流≠经营现金流)、免责声明、违规表述;任何一项不过,整段 LLM 内容作废,回退规则版并向用户披露原因。

### 失败显式披露原则

- 数据抓取失败:重试后仍失败 → 报告标注"数据不可用 + 原因",其余环节照常走完,**绝不伪造数字**;
- 该标的无本地资料:报告明示"该标的无本地研究资料,证据仅来自实时新闻",**绝不用其他公司的资料凑数**(ticker 隔离);
- 扫描版 PDF 无文字层:跳过并说明,**不把"解析失败"伪装成"已解析"**。

## 量化结果

### 检索质量(Apple 2025 Form 10-K,20 题人工标注评测集)

| 指标 | Hash 基线 | Semantic | Hash+术语映射(优化后) |
|---|---:|---:|---:|
| 页面 Recall@4 | 0.20 | **0.60** | 0.80 |
| 关键词核验 Recall@4 | 0.20 | 0.30 | **0.80** |
| Top-1 页面相关性 | 0.00 | 0.30 | 0.60 |

评测集覆盖服务收入、研发费用、经营现金流、中美销售额、实际税率等 20 个问题,每题标注应命中页码与关键证据词。**"关键词核验"指标专门捕捉"页码碰巧命中、内容却不支持问题"的情况。**

诚实记录的未决问题:5 题留出集上 Semantic 的关键词核验(0.80)反超 Hash+术语映射(0.40),与主集结论不一致。样本太小不足以裁决默认模式,**该问题作为评测边界保留,未通过挑选好看的单边结果来"解决"**。详见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。

### 受控 LLM 验收(2026-09-08,已冻结)

| 题目 | 输出模式 | Safety |
|---|---|---:|
| services_revenue | 受控 LLM | 通过 |
| r_and_d_expense | 受控 LLM | 通过 |
| operating_cash_flow | 受控 LLM | 通过 |
| china_sales | 受控 LLM | 通过 |
| effective_tax_rate | 受控 LLM | 通过 |

验收标准为 ≥3/5 受控 LLM 且 safety 通过,实际 **5/5**。此前轮次中,LLM 曾因复述数值被全部拒绝并回退规则版——门禁机制按设计工作。规则版报告始终保留为稳定回退路径。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 建立知识库

将含文字层的 `.txt` / `.md` / `.pdf`(如公司年报)放入 `data\knowledge_base`,然后:

```powershell
python -m investment_assistant.cli index
```

本地资料按文件名推断归属 ticker 并隔离检索;推断不出的标记 `unknown`。查询某标的时只检索该标的的资料,无资料则显式披露。

### 生成研究简报

```powershell
python -m investment_assistant.cli research --ticker AAPL --topic "服务业务、现金流与估值" --horizon 中期
```

支持美股(`AAPL`)、港股(`0700.HK`)、A股(`600519.SS`)。报告与审计 JSON 写入 `data\reports`。

### 可选:语义嵌入

```powershell
pip install -r requirements-semantic.txt
$env:RAG_EMBEDDING_MODE="semantic"
```

语义模型从本地缓存离线加载;缺失时显式报错,不静默降级伪装成语义结果。

### 运行评测 / 回归 / Web 演示

```powershell
python -m investment_assistant.evaluation --top-k 4   # 双模式隔离评测
python -m investment_assistant.golden_regression       # 黄金样本离线回归(无需网络)
.\start_demo.bat                                        # FastAPI + Streamlit 一键演示
```

## 已知边界

完整清单见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md),要点:

- **引用存在 ≠ 语义支持**:引用格式正确不代表该页内容支持该论断(KI-001),当前检索质量下受控叙述偏向保守的覆盖性陈述(KI-002);
- **数据源非官方**:yfinance 可能延迟、缺失或被修订(KI-003);新闻需回到原始链接核验(KI-004);
- **扫描版 PDF 不支持**:无文字层的 PDF 被跳过,未接 OCR,不伪造解析结果(KI-005);
- **不输出交易指令、目标价或收益承诺**;受控 LLM 的默认模式选择(main/holdout 集结论不一致)作为开放问题保留。

## 项目结构

```text
investment_assistant/
├── workflow.py          # LangGraph 七节点工作流(核心)
├── market_data.py       # yfinance 行情/财报/估值/新闻采集
├── rag.py               # Chroma 向量库:PDF 逐页解析、ticker 隔离、混合重排
├── llm_generation.py    # 受控 LLM:槽位模板、引用校验、数字溯源
├── safety.py            # 风险校验与报告级安全规则
├── evaluation.py        # 检索质量评测(R Recall/关键词核验/相关性)
├── golden_regression.py # 黄金样本离线回归
├── api.py / web_app.py / cli.py  # FastAPI / Streamlit / 命令行入口
tests/                   # 自动化单元与边界测试
data/                    # 知识库、评测集、报告产物、审计记录
```

## 开发方式与致谢

- 工作流思路迁移自老师的"感知 → 建模 → 推理 → 决策 → 报告"课程思想,重新实现为独立工程;
- 架构决策、验收标准、质量把关与边界管理由本人主导,代码实现由 AI 辅助完成——每个冻结决策(评测阈值、回退策略、已知边界)都有对应的可复现产物。

## 免责声明

本项目仅用于学习和研究,不构成任何投资建议。市场存在本金损失风险;历史价格、回报和新闻内容均不能预测未来表现。请在任何决策前核验原始来源,并咨询持牌专业人士。


## 团队身份与审核发布（R6/R7，单机演示）

所有 `/api/*` 数据入口（health 除外）要求个人 Bearer token。后端启动前在其环境配置 `IA_AUTH_TOKENS`：

```powershell
# 以下仅为本地演示占位凭证；真实使用应换成足够随机的秘密，不写入仓库。
$env:IA_AUTH_TOKENS = '{"replace-with-analyst-secret":{"actor":"analyst","tenant":"team-a","roles":["analyst"]},"replace-with-reviewer-secret":{"actor":"reviewer","tenant":"team-a","roles":["reviewer"]},"replace-with-publisher-secret":{"actor":"publisher","tenant":"team-a","roles":["publisher"]}}'
.\start_demo.bat
```

配置可附 `expires_at`（带时区 ISO 时间）。不配置就拒绝数据访问，不存在匿名兼容入口。Streamlit 登录填写个人凭证；角色和租户以服务端为准。旧报告缺少可信归属时不能直接公有化。

操作顺序：分析员创建任务 → 另一审核人打开工作台、手工登记完整 claims 并核验正文/证据 → 逐条确认和整篇覆盖声明 → 第三位发布人发布 → 同租户用户读取/问答。结构门禁通过不等于整篇语义自动证明；未经发布正文不交付。撤回或资料版本变化后旧审核/发布/关联记忆失效。

版本关注与研究记忆在页面下方；记忆最多 90 天，可撤销，只作为用户注记，不参与事实生成。只支持单 API worker，本地 JSON 与审计不等于企业权限基础设施。

### 完全离线的界面烟测

```powershell
.venv\Scripts\python.exe scripts\r6_r7_ui_smoke.py --port 18080
# 另一个终端：
$env:INVESTMENT_ASSISTANT_API_URL = 'http://127.0.0.1:18080'
.venv\Scripts\python.exe -m streamlit run investment_assistant/web_app.py --server.headless=true --server.port=18501 --server.address=127.0.0.1
```

脚本只在临时目录创建合成资料。测试凭证为 `fixture-analyst-token`、`fixture-reviewer-token`、`fixture-publisher-token`、`fixture-other-tenant-token`，**绝不可用于真实服务**。烟测不是生产金融数据验证。交付审阅记录见 `docs/REVIEW_LOG.md`；本地截图和临时运行日志不随仓库发布。
