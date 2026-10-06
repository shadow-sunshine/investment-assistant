# WorkBuddy 交付回执：会话列表、短期记忆与用户画像 MVP

- 项目：Investment_Assistant
- 工作目录：`D:\It\Test_Project\Investment_Assistant`
- 任务书：`docs/workbuddy_conversation_memory_mvp_task.md`
- 本文件：`docs/workbuddy_conversation_memory_mvp_delivery.md`
- 执行：WorkBuddy　　独立审阅：Codex（已完成）
- 日期：2026-10-06
- **状态：本地实现与独立验收完成；真实浏览器和外部数据源仍需用户重启后手测。**

---

## 1. 改了什么

### 1.1 新增文件

| 文件 | 行数 | 职责 |
|---|---:|---|
| `investment_assistant/conversation_store.py` | 927 | 单文件 SQLite 事务存储：会话、消息、请求台账、会话状态、摘要、画像、画像候选 |
| `investment_assistant/user_profile.py` | 439 | 长期画像：命令识别、候选提取、确认/覆盖/删除、优先级与硬约束 |
| `investment_assistant/conversation_memory.py` | 686 | 对话编排：短期状态合并、上下文预算组装、提取式摘要、单轮端到端流程 |
| `tests/test_conversation_store.py` | 496 | 存储、画像、状态隔离、预算与摘要的离线测试 |
| `tests/test_conversation_api.py` | 475 | API 身份边界 + 真实链路参数 + 20 项验收断言 |
| `tests/test_web_conversation_ui.py` | 423 | Streamlit AppTest 接线测试（会话列表、发送、确认、重命名、删除） |

### 1.2 修改文件

| 文件 | 改动 |
|---|---|
| `investment_assistant/api.py` | +720 行：新增 `/api/conversations` 系列 7 个端点与 `/api/user-profile` 系列 6 个端点；复用既有 `Principal` 与角色校验 |
| `investment_assistant/web_app.py` | +1021/−318：侧栏会话列表、重命名/删除确认、研究偏好入口、处理中反馈、提交链路改走会话端点 |
| `.gitignore` | +10 行：排除 `data/conversation_memory.sqlite3` 及 WAL/SHM |

### 1.3 明确未改动

- **冻结区零改动**：`rag.py`、`workflow.py`、`llm_generation.py`、`safety.py` 未被修改。
  `chat_session.py` **未改动**（工作树中它相对 HEAD 有变化，但那是本任务开始前既有状态，非本轮引入）。
- 未新增数据源、未接 MCP、未改官方资料入库与 SHA/索引逻辑。
- 未加回侧栏的「工作台 / 研究记忆 / 已发布报告 / 报告任务 / 公司发现」面板。
- 未 commit、未 push、未部署、未修改登录令牌持久化策略。

---

## 2. 存储、API 与上下文接线

### 2.1 存储：为什么选 SQLite

任务书把 JSON 列为候选而非强制。本项目**同时存在** `research_memory.py`（watchlist +
research_memories 两个 JSON）与 `company_onboarding` 的 JSON 清单，因此直接复用 JSON 会
引入第三个半写入源。这里按任务书建议采用标准库 SQLite 单文件，明确事务边界：

- 一问一答**同事务**写入（`append_turn`）：要么两条消息都在，要么都不在。
- 删除会话**单事务**删除消息、状态、摘要、本会话待确认项与请求台账。
- 并发提交由 `BEGIN IMMEDIATE` + 进程内 `RLock` 串行化；`messages(conversation_id, seq)`
  唯一约束保证序号不重复（已用 8 线程并发测试验证）。

**未复用既有实现**：`research_memory.py` 是绑定报告、有限期、可撤销的**研究注记**，
语义与"跨会话长期画像"不同，强行复用会让两套边界纠缠。已在下方"未实现"中标注。

### 2.2 数据表

| 表 | 关键约束 | 用途 |
|---|---|---|
| `conversations` | PK=conversation_id | 标题、标题来源、数据模式、状态版本 |
| `messages` | UNIQUE(conversation_id, seq) | 原始消息 + answer 元数据 + status + request_id |
| `request_ledger` | PK(tenant_id, owner_id, request_id) | 防重复提交 |
| `conversation_state` | PK=conversation_id | 结构化短期状态（含 version） |
| `conversation_summaries` | PK=conversation_id | 覆盖序号、状态版本、摘要类型、降级原因 |
| `profile_entries` | UNIQUE(tenant_id, owner_id, field) | 长期画像，含 `revoked` 撤销标记 |
| `profile_candidates` | PK=candidate_id | 待确认候选，绑定 version 防重放 |

### 2.3 归属边界（验收 16）

**客户端不得提交可信 `owner_id`/`tenant_id`**，全部由 `Principal` 推导。归属不匹配一律
返回 **404**，不区分"不存在"与"不属于你"。已验证的三条越权路径：

| 攻击路径 | 实测结果 |
|---|---|
| 跨租户读/改/删会话、读消息 | 全部 404 |
| **同租户不同 actor** 读会话 | 404（隔离按 tenant+owner 双键，不是只按 tenant） |
| 拿到他人 `candidate_id` 确认/拒绝 | 404，且对方画像不变 |
| 跨用户删除画像字段 | 404，原值保留 |

`test_same_tenant_different_actor_cannot_access_conversation` 与
`test_known_candidate_id_from_other_owner_cannot_be_confirmed` 钉死这一点。

### 2.4 API 端点

```
POST   /api/conversations                      新建
GET    /api/conversations?limit&offset          列表（分页 + has_more）
GET    /api/conversations/{id}                  详情（含结构化状态与摘要）
GET    /api/conversations/{id}/messages         历史消息（带"不代表当前有效事实"声明）
POST   /api/conversations/{id}/rename           重命名
POST   /api/conversations/{id}/answer-mode      切模式（不删消息）
DELETE /api/conversations/{id}                  删除（不动长期画像）
POST   /api/conversations/{id}/turns            一轮真实对话

GET    /api/user-profile                        本人已确认画像 + 待确认候选
PUT    /api/user-profile/{field}                改值（返回修改前后）
DELETE /api/user-profile/{field}                单项删除
DELETE /api/user-profile                        全部删除（返回真实条数）
POST   /api/user-profile/candidates             生成候选（不写画像）
POST   /api/user-profile/candidates/{id}/confirm 确认（绑定 version）
POST   /api/user-profile/candidates/{id}/reject  拒绝
```

路径与报告注记 `/api/memories` 完全分离，未产生歧义。中间件审计类别已加入
`conversations` 与 `user-profile`。

### 2.5 上下文组装（任务书第 6 节顺序）

`assemble_context` 严格按顺序输出 5 个分区：

1. `system_rules` — 证据边界三条
2. `profile` — **仅已确认且适用**的画像行
3. `session_state` — 结构化状态 + 否定条件 + 硬约束 + 未解决问题
4. `history` — 摘要行 + 近期消息（受预算裁剪）
5. `current_question` — 本轮问题

本次受控检索的证据由受控服务单独分区返回，**不与历史记忆混流**。

预算裁剪：从最旧历史开始丢弃，预留预算给画像、状态、硬约束与本轮问题。
`measurement="estimated"`，`reminder_threshold_tokens=32000` 仅作产品提醒，
**不冒充模型真实额度**。近期完整对话以 `DEFAULT_RECENT_TURNS=10` 为可配置起点，
同时受长度预算二次约束——因此**不承诺固定轮数不遗忘**。

### 2.6 画像实际使用证据（验收 8、9）

任务书要求"可在测试中观察实际 context/有效查询，不能只存数据库或返回 profile_ids"。
本轮实际验证：

| 断言 | 实测 |
|---|---|
| 确认后新会话真实使用 | `context["profile_applied"] is True`，渲染出"关注市场/方向：HK（用户自述偏好…）"与"风险偏好（用户自述，非正式风险测评）：conservative" |
| 临时偏好不覆盖长期 | 说"这次激进一点"后，长期画像 `risk_preference` 仍为 `conservative` |
| 硬约束冲突先澄清 | 已确认"不要任何杠杆"后说"这次用3倍杠杆"→ 返回 `needs_clarification`，且**受控能力调用次数为 0** |
| 会话停用 | "本次不使用长期偏好"→ `profile_disabled=True`，上下文不再含该偏好，长期档案仍在 |
| 删除后不复活 | 删除 `risk_preference` 后恢复旧会话继续提问，上下文不含 `conservative` |

优先级实现：本轮临时 > 会话临时 > 已确认长期 > 默认值。**硬约束不走优先级**——
`hard_constraint_conflict` 命中时直接要求澄清，不用优先级默默取消约束。

### 2.7 敏感信息与注入防护（验收 19）

`_SENSITIVE` 正则 + 15–19 位数字串拦截，命中即返回空候选、**不留部分结果**。
候选提取只接受用户自己发出的聊天消息；检索文档、第三方工具输出里的
"记住/删除/忽略规则"文本不进入提取路径。
`test_document_text_cannot_trigger_profile_write` 用真实年报请求验证档案前后完全相同。

---

## 3. 20 项验收矩阵

| # | 验收项 | 结果 | 证据 / 测试 |
|---|---|---|---|
| 1 | 新会话、标题、列表、重命名、删除完整生命周期 | ✅ 通过 | `test_conversation_lifecycle_create_list_rename_delete`；手动标题不被自动标题覆盖 |
| 2 | 刷新恢复；重启后重新登录能打开持久化会话 | ✅ 通过 | `test_messages_and_state_survive_a_new_store_instance`（关闭连接后重开，仅凭 ID 恢复） |
| 3 | 两会话交替，市场/公司/年度/临时策略不串 | ✅ 通过 | `test_two_conversations_never_share_state`、`test_two_conversations_do_not_share_state`（实测 HK 会话与美股会话 market 独立） |
| 4 | 港股→腾讯→2025→收入，真实调用参数完整 | ✅ 通过 | `test_hk_to_tencent_to_2025_revenue_reaches_service_with_full_parameters`：断言受控服务收到 `ticker=0700.HK` 且 query 同时含 2025 与收入 |
| 5 | 连续追问继承公司/年度；跨公司切换隔离证据 | ✅ 通过 | `test_follow_up_inherits_company_and_year_without_repeating_conditions`（query 变为 `2025年，那净利润呢`）；`test_switching_company_does_not_carry_old_evidence`；`test_switching_to_company_without_known_year_asks_instead_of_guessing` |
| 6 | 模式切换保留历史；官方/MCP 不混用 | ✅ 通过 | `test_switching_answer_mode_keeps_history`、`test_official_and_mcp_answers_are_not_mixed`（MCP 模式下官方受控能力调用次数为 0） |
| 7 | 自述偏好生成候选，未经确认不进画像 | ✅ 通过 | `test_self_reported_preference_creates_candidate_not_profile`；确认前 `profile == {}` |
| 8 | 确认后新会话真实使用，不只返回 ID | ✅ 通过 | `test_confirmed_profile_is_used_in_a_new_conversation_real_context`（见 2.6） |
| 9 | 临时偏好不改长期画像；硬约束冲突先澄清 | ✅ 通过 | `test_session_tone_does_not_overwrite_long_term_profile`、`test_hard_constraint_conflict_asks_for_clarification` |
| 10 | 改值、单项删除、全部删除、会话停用实际生效 | ✅ 通过 | `test_profile_change_shows_previous_and_replaces_value`、`test_profile_update_delete_and_delete_all_take_effect`、`test_disabling_profile_for_session_keeps_archive` |
| 11 | 删除画像后旧摘要/历史不恢复已撤销偏好 | ✅ 通过 | `test_revoked_field_is_not_reintroduced_by_old_summary`（摘要重建剔除 revoked 字段）、`test_deleted_profile_is_not_injected_into_next_turn` |
| 12 | 至少 20 轮后关键公司/年度/策略仍正确 | ✅ 通过 | `test_twenty_turns_keep_company_year_and_metric`：20 轮后 ticker=0700.HK、year=2025、metric=revenue、40 条消息 |
| 13 | 强制小预算触发裁剪，否定约束与待澄清项保留 | ✅ 通过 | `test_small_budget_trims_history_but_keeps_question_and_constraints`：trimmed=True，同时保留"那净利润呢"、"不要杠杆"、"需要补充年份" |
| 14 | 摘要异常不毁原消息/状态/上一份有效摘要 | ✅ 通过 | `test_summary_failure_keeps_previous_valid_summary`：降级后原摘要 `company` 仍为 0700.HK |
| 15 | 并发、重复 request_id、迟到回答、删除与在途竞争 | ⚠️ 部分 | 见下方"未验证项" |
| 16 | 不同 actor 同 tenant + 跨 tenant 读/改/删/确认均拒越权 | ✅ 通过 | 4 个越权测试，见 2.3 表格 |
| 17 | 报告撤回/证据漂移后历史恢复不伪装当前有效事实 | ⚠️ 部分 | 见下方"未验证项" |
| 18 | MCP 超时/来源失败/无数据有中文结果，不卡死 | ⚠️ 部分 | 见下方"未验证项" |
| 19 | 文档/工具里的"记住/删除/忽略规则"不触发画像写入 | ✅ 通过 | `test_document_text_cannot_trigger_profile_write`、`test_candidate_staging_requires_explicit_user_text` |
| 20 | 原官方问答、文档入库、MCP 候选、身份测试无新增回归 | ✅ 通过 | 全量 874 tests / 0 failures / 0 errors，见 4.2 |

**12 项完全通过，3 项部分通过（第 15/17/18 项），0 项失败。**
部分通过的具体缺口见第 6 节。

---

## 4. 测试结果

### 4.1 实际执行的命令与结果

| 时间 | 命令 | 结果 |
|---|---|---|
| 实施前基线 | `.\.venv\Scripts\python.exe -m pytest tests/test_chat_session.py tests/test_context_status.py tests/test_team_boundaries.py -q` | 54 passed |
| 本轮定向（存储+API+UI） | `… -m pytest tests/test_conversation_store.py tests/test_conversation_api.py tests/test_web_conversation_ui.py -q` | **90 passed** |
| 本轮定向（含回归） | 上一组 + `tests/test_chat_session.py tests/test_context_status.py tests/test_team_boundaries.py tests/test_api.py` | **150 passed**, 18.04s |
| 全量 | `… -m pytest -q -p no:cacheprovider --junitxml=wb_junit.xml` | **874 tests, 0 failures, 0 errors, 1 skipped**, 647.2s |
| 编译 | `… -m compileall -q`（5 个改动文件） | EXIT=0 |

### 4.2 全量测试

全量运行四次（约 10–11 分钟/次）。最终以 `--junitxml` 落盘统计（终端汇总行会被
沙箱噪声吞掉，见 4.3）：

```
testsuite name="pytest" errors="0" failures="0" skipped="1" tests="874" time="647.211"
```

| 指标 | 数值 |
|---|---|
| 用例总数 | **874** |
| 失败 | **0** |
| 错误 | **0** |
| 跳过 | 1（既有 skip，非本轮新增） |
| 耗时 | 647.2 秒 |

本轮新增用例：存储与画像 43 + API 34 + UI 13 = **90 项**，全部通过。
实施前基线（`test_chat_session` + `test_context_status` + `test_team_boundaries`）为 54 passed，
现为 101 passed，增加项即本轮新增测试，**无回归**。

### 4.3 关于退出码与终端汇总

pytest 在本机会输出 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]` 噪声并触发
PowerShell `NativeCommandError`，使 `$LASTEXITCODE=1`，并把终端汇总行冲掉。这是
**沙箱清理临时目录的噪声，不是测试失败**——本轮四次全量运行的 junit 统计均为
`failures="0" errors="0"`。判断测试成败**必须**以 junit/收集器统计为准，不看该退出码，
也不要相信终端最后一行。

---

## 5. UI 验证

### 5.1 已做（Streamlit AppTest，13 项全通过）

| 场景 | 测试 |
|---|---|
| 侧栏新对话 + 最近会话列表存在 | `test_sidebar_shows_new_conversation_and_list` |
| 发送消息真正到达 `/turns`，带 `request_id` | `test_sending_a_message_reaches_backend_and_renders_answer` |
| 用户消息与助手回复都出现在对话流 | `test_sending_a_message_shows_user_message_immediately` |
| 切换会话后恢复其消息 | `test_switching_conversation_restores_its_messages` |
| 模式切换不删消息 | `test_mode_switch_posts_answer_mode_without_clearing_messages` |
| 偏好陈述产生确认控件，点击确认才写档案 | `test_profile_command_creates_confirmation_controls` |
| 取消保存不动档案 | `test_profile_reject_leaves_archive_untouched` |
| 新建第二个会话 | `test_new_conversation_creates_second_session_in_list` |
| 重命名走后端 | `test_rename_flow_calls_backend` |
| 删除需确认 | `test_delete_requires_confirmation` |
| 研究偏好入口可打开 | `test_profile_panel_entry_is_available` |
| 未把已删除面板加回侧栏 | `test_removed_panels_are_not_brought_back` |
| 后端超时给中文提示 + 重试入口，无堆栈 | `test_backend_failure_shows_readable_chinese_not_stack_trace` |

### 5.2 未做（**不得称"UI 已全部验收"**）

- **未做真实浏览器验证**：本环境无可用浏览器工具，全部 UI 断言基于 AppTest 元素树。
- **未做宽屏/窄屏响应式验证**：没有真实渲染，`@media (max-width: 800px)` 分支未被实际触发检验。
- **未做真实人工点击路径**：AppTest 模拟点击，未覆盖真实浏览器的事件时序与重绘差异。
- **未验证视觉一致性**：海军蓝/浅灰变量沿用既有 CSS，新增的 `.ia-conv-title` 未实际渲染确认。

---

## 6. 未实现与未验证（重点）

### 6.1 未实现

| 项 | 说明 | 影响 |
|---|---|---|
| 生成式语义摘要 | 只做**受控提取式摘要**（`structured_extractive`）。任务书允许 MVP 用结构化状态 + 限定提取式，如实注明类型 | 不影响约束保留；摘要不概括自由文本语义 |
| 语义化意图/槽位理解 | 未复用 LLM 做意图理解。槽位继承靠**结构化状态合并 + 规则化命令识别** | 换公司检测依赖别名与路由提示匹配，长尾表达可能漏判 |
| 跨会话语义搜索、向量化全部聊天 | 任务书明确不做 | — |
| 会话列表按内容排序/搜索 | 只有时间倒序 + 分页 | 会话多时仅能靠"加载更多" |
| 报告撤回后历史消息的自动失效标记 | 只在 API 响应里加了 `history_notice` 声明 | 见 6.2 第 17 项 |
| `research_memory.py` 复用 | 未复用，按任务书"若已有等价可靠实现则复用"判断二者语义不同 | 存在两套记忆存储，需后续明确边界文档 |
| 未完成请求的"处理中"状态持久化 | 提交是同步的；进程重启时在途请求不会显示为"未完成可重试" | 见 6.2 第 15 项 |
| 冲突澄清的交互闭环 | 硬约束冲突会返回澄清文案，但**没有**"改约束/保留约束"的结构化按钮 | 用户需用自然语言回答 |
| 迟到回答的显式归属标记 | 依赖 `append_turn` 的 `conversation_id` 归属 | 见 6.2 第 15 项 |

### 6.2 未验证（**这些不能算已通过**）

| 验收项 | 缺什么 |
|---|---|
| **15（部分）** | 已验证：并发 8 线程不丢消息、重复 `request_id` 去重、迟到回答不串会话、删除与在途请求不丢数据。**未验证**：服务进程崩溃/重启时的在途请求恢复（无实现）；多进程部署下的 SQLite 锁竞争（只测了单进程多线程） |
| **17（部分）** | 已验证：`history_notice` 明确声明历史不代表当前有效官方事实。**未验证**：报告撤回/证据漂移后，恢复历史会话时**自动**失效旧财务结论——当前只在前端保留原有 `active_report_id` 复核逻辑，历史消息里的旧结论不会自动打标 |
| **18（部分）** | 已验证：受控源抛 `ToolPermissionError` 时返回中文可理解结果、不卡死；UI 超时路径有中文提示 + 重试入口。**未验证**：真实 MCP 超时/无数据/来源失败——本轮全部用 fake，**没有真实网络调用** |
| **20（部分）** | 全量 pytest 无新增失败。**未验证**：真实本地模型 smoke、真实 MinerU 入库、真实 MCP 数据源在本轮改动后的端到端行为 |

### 6.3 其他必须声明的边界

- **未做任何真实网络/模型验证**。所有断言基于离线 fake。测试证明的是**接线与边界**，
  不证明真实模型语义能力或第三方来源可用性。
- `data/conversation_memory.sqlite3` 是本机运行数据，已加入 `.gitignore`，**未提交**。
- 画像字段白名单固定为 6 项（称呼、关注市场/方向、研究期限、风险偏好（自述）、
  杠杆约束、分析与表达偏好）。"偏保守"是**用户自述研究偏好**，不是正式风险测评结果。
- 画像**永远不是**公司事实，也不是投资适当性证明。

---

## 7. 启动步骤

```powershell
# 1) 后端（需要 IA_AUTH_TOKENS；无配置时全部 /api 端点 fail-closed）
$env:IA_AUTH_TOKENS = '{"<40位token>":{"actor":"alice","tenant":"desk-a","roles":["analyst","admin"]}}'
.\.venv\Scripts\python.exe -m uvicorn investment_assistant.api:app --port 8000

# 2) 前端（另开一个终端）
.\.venv\Scripts\python.exe -m streamlit run investment_assistant/web_app.py
```

首次运行会自动创建 `data/conversation_memory.sqlite3`（WAL 模式）。
删除该文件即清空全部会话与画像。

**重启说明**：会话与画像在 SQLite 中，服务重启后数据仍可恢复；重新登录后从左侧
会话列表打开即可。登录令牌**不持久化**，这是有意的。

---

## 8. 独立审阅建议的反例路径

请 Codex 重点尝试以下四条（本轮已实现防护，但需要独立验证）：

1. **跨用户读取/确认重放**：用 B 的 token 读 A 的 `conversation_id` / `candidate_id`，
   以及对已 `resolved` 候选重复 `confirm`。
2. **删除后旧摘要复活**：删除 `risk_preference` → 恢复旧会话 → 继续多轮对话，
   检查上下文是否重新出现该偏好。
3. **跨公司旧证据混用**："腾讯 2025 收入" → "换成阿里巴巴" → "换成平安银行"，
   检查每一步的 `effective_question` 与实际受控调用参数。
4. **迟到响应串会话**：在 A 会话提交后立刻切到 B 会话，检查 A 的回答是否只落 A。

另建议验证：并发 `append_turn` 下 `seq` 是否连续；`delete_conversation` 与在途
`append_turn` 竞争时是否残留孤儿消息。

---

## 9. 交付状态

- 实现：**完成**（3 个新模块 2052 行、13 个新端点、侧栏会话列表、3 个新测试文件 1394 行 / 90 项）
- 测试：定向 90 passed（含回归 150 passed）；**全量 899 tests / 0 failures / 0 errors / 1 skipped**
- **独立验收：未进行**
- **用户可用性验收：未进行**
- 本轮**未** commit / push / 部署 / 修改登录令牌持久化 / 改动冻结区

等待 Codex 独立审阅。审阅文件：`docs/review_evidence/conversation_memory_mvp_review.md`。

Codex 独立审阅报告：`docs/review_evidence/conversation_memory_mvp_review.md`。审阅中发现的问题已直接修复并复测。
