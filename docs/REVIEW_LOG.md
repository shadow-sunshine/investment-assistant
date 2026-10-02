# 阶段审阅摘要

## R1：2026-09-25 至 2026-09-26

- F1/F2/F3/F4 评测护栏已完成。
- 目标测试最终结果：49 passed。
- 全量测试最终结果：155 passed，1 warning。
- `git diff --check`：退出码 0，无 whitespace error。
- 发现并由主助手直接修复：`render_from_result()` 对 `pending`、`running`、`failed`、缺失状态统一 fail-closed，不再把非 `ok` 结果渲染为正常报告。
- 冻结区 `rag.py`、`workflow.py`、`llm_generation.py`、`safety.py` 未修改。
- R1 历史材料已归档到 `docs/archive/R1_2026-09-25/`。

## 下一阶段

等待 WorkBuddy 按 `docs/CURRENT_TASK.md` 执行 R2 中文召回最小可证伪实验。完成后停止并等待主助手统一验收。


## R2：2026-09-26

- WorkBuddy 初版完成了离线 page-level 三策略实验，但主助手复核发现检索侧从评测 `keywords` 注入目标术语及目标数值，属于标签泄漏，初版数字不可作为有效增益结论。
- 主助手直接修正为 query-only：术语同义扩展只由 question 中命中的短语触发；数值锚点只从 question 抽取；gold keywords 仅用于结果核验，不进入候选打分。
- 重新运行 `python -m investment_assistant.chinese_anchor_eval --top-k 4`，R0 样本 SHA 匹配，AAPL/MSFT/600519.SS/0700.HK 资料 SHA 全部 match。
- 修正后指标：baseline Recall@4 0.125 / Top-1 0.0625；lexical_anchor 0.625 / 0.1875；numeric_anchor 0.125 / 0.0625。
- 词面锚点表人工精选且参考当前 R0 术语，仍有同集调参风险；不能据此宣称泛化或直接修改线上检索。数值锚点在本实验无增益。
- 目标回归：67 passed；全量：173 passed, 1 warning；`git diff --check` 退出码 0。
- 下一步设为 R3：新建独立 holdout、冻结策略后，在临时 Chroma 索引验证真实检索路径。不得把 gold labels 输入查询/打分。


## R3 复核与 R3 v3 收尾：2026-09-27

- WorkBuddy 的 R3 v2 通过了与 R0 双层独立检查，但复核发现 holdout 内部仍把相同 ticker/page/keyword/value 的事实以双语改写重复计入多个象限，因此 v2 不作为独立泛化结果。
- 主助手接管最小修复：重建 `data/r3_holdout_eval_set_v3.json`；与 R0 两层重叠 0、v3 内部事实重叠 0；四象限各 8 条；目标页关键词核验无缺失（MSFT PDF 个别标签乱码，使用原文数值作为核验锚点）。
- R3 目标测试：`20 passed in 450.50s`；真实运行 `python -m investment_assistant.r3_real_chroma_eval --top-k 4` 成功；结果：Recall@4 0.0625→0.0938，平均跨 ticker 来源 2.0000→2.0312，受污染样本 23→24，Top-4 全错 12→11。综合污染门禁后明确 `no_online_ab`。
- 临时 Chroma 目录默认改为一次性目录并自动清理；传入路径限定 `data/evaluations/r3_*` 且非空拒绝；已有安全测试覆盖哨兵保护。工作树中的历史 `r3_chroma_tmp/` 未删除。
- 冻结区未改。R3 已结束，下一阶段 R4 数据源/工具治理。
- 全量回归：`.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q` → `193 passed, 1 warning`；`git diff --check` → exit 0（仅有既存 LF/CRLF 提示）。

## R4 独立复核与收尾：2026-09-27

- WorkBuddy 报告的 R4 核心实现经独立复核：结构化 HTTP 错误、unsupported ticker 健康登记、失败任务 degradation 传递、fetch_materials 调用审计已补齐；后续补充 yfinance 三类调用的统一重试与健康/审计登记，以及新闻抓取失败与正常空结果的区分；报告任务从自身 final_state 归集来源错误，API 对完成任务也返回 job-scoped degradation；本地新闻别名配置损坏会显式失败。
- 主助手发现并直接修复一个边界缺陷：`ToolCallLedger` 原先只按全局 fingerprint 去重，会把不同 job 的合法同请求误吞；现改为按 `(job_id, fingerprint)` 在同一任务边界内幂等，不同任务保留独立审计记录。
- 新增回归覆盖不同 job 的同 fingerprint；原有同 job 去重语义保持不变。
- 定向回归：`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_source_governance.py tests/test_fetch_materials.py tests/test_api.py tests/test_report_jobs.py -q` → `66 passed, 1 warning`。
- 定向回归（含 market_data、news_filter 和 R4 API/job/fetch）：`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_market_data.py tests/test_news_filter.py tests/test_source_governance.py tests/test_fetch_materials.py tests/test_api.py tests/test_report_jobs.py -q` → `85 passed, 1 warning`。
- 最终全量回归：`.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q` → `239 passed, 1 warning`（547.77s）。`git diff --check` 无 whitespace error，仅有既存 LF/CRLF 提示。
- 冻结区未改；未执行 `git add`、commit、stash、reset、clean；既有 R3 未提交修改与未跟踪文件均保留。
- R4 剩余边界：健康/审计账本为进程内存态；yfinance SDK 调用的底层网络超时无法在离线 mock 中证明，财务快照包含多次 SDK 请求，仍需实网验证总预算行为；工具账本中的 yfinance `job_id` 目前为空（冻结工作流未传递任务上下文），但每个报告 job 的来源错误另由 final_state 单独保存；`requested_by` 仍只是标签，不是认证/授权；未做真实网络和浏览器 E2E。

## 下一阶段

R4 核心目标通过离线独立验收，并记录上述实网/身份上下文剩余边界。下一阶段为 R5「证据与交付门禁」，执行入口改为 `docs/R5_EVIDENCE_DELIVERY_GATES_TASK.md`。

## R5 独立复核与收尾：2026-09-27

- 初版 `tests/test_evidence_gates.py` 为 38 passed，但独立攻击复核证实多个 fail-open：来源 period/unit 未验证、数值 `10` 子串命中 `100`、source_id 缺失、SHA 缺失/漂移可绕过、旧快照值和无 value 字段被判 supported、好坏锚点混合错误降为 partial；`GET /api/reports/{id}` 与 job report 可绕过 delivery，blocked answer 仍返回正文。
- coder agent 完成了来源问答锚点补齐和 delivery 查询 409 的初步改动，未完成其余漏洞且明确未验证；主助手接管并修复。现由原始 PDF 同页文本、报告来源 SHA、canonical manifest/资料实际字节、结构化字段及当前 ticker/report/source/page 共同校验。读原文时同一份 bytes 再核 SHA，缺版本/来源/字段/单位元数据时 fail-closed；任一关键锚点不支持不再作为 partial 放行。
- 整份报告目前没有完整 claim 覆盖证明，即使个别 fixture claim 验证成功，报告路由仍 `needs_review`（409），不返回正文；问答的单条结构化结果可以独立核验，但未经门禁的回答在非 2xx 响应中清空 answer/claims/evidence_refs。job 的 `completed` 仅表示生成完成，不等于交付放行。
- 当前独立验证：`.venv\Scripts\python.exe -m pytest -q tests/test_evidence_gates.py tests/test_api.py tests/test_report_jobs.py` → `81 passed, 1 warning`；**最终工作树** `.venv\Scripts\python.exe -m pytest -q` → `293 passed, 1 warning`（333.85s）；`compileall` 与 `git diff --check` 成功（有既存 LF/CRLF 提示，无 whitespace error）。没有改冻结区或线上默认检索路径；没有 commit、stash、reset、clean。
- 验收范围：R5 的**离线后端证据结构校验与 fail-closed 交付接口**通过。不是 claim 文本语义蕴含证明，更不是零幻觉；尚无自动完整 claim 抽取或人工覆盖审核，当前真实报告一般不可发布。真实网络、生产身份、浏览器 UI 主交互和人工审核链均未验证；Streamlit 对新 409/422 的友好提示与真正发布流列入下一批次。
- 用户已授权剩余 R6/R7 一次性交给 WorkBuddy；执行入口 `docs/CURRENT_TASK.md`，详见 `docs/R6_R7_BATCH_TASK.md`。这不是 WorkBuddy 自测即通过，最后仍由 Codex 独立验收；R1 线上检索切流仍受 R3 `no_online_ab` 限制。


## R6/R7 WorkBuddy 中断接管与最终验收：2026-09-27

- 用户授权主助手接管，未继续派发 WorkBuddy。中断时仅四个新模块的雏形，缺 API/任务/UI 集成和新测试；接管后的首轮授权回归存在误插入变量和旧 requested_by 契约断言，均已按真实服务端主体修复，而非恢复匿名旁路。
- R6 补齐统一 token 认证、actor/tenant/roles、可选过期时间、报告/job 隔离、角色/owner 检查、请求与后台任务归属、服务端生成不可被 requested_by 覆盖；旧无可信归属报告不默认公有。
- 补齐人工完整 claim 集登记、逐条确认/整篇覆盖声明、当前字节和原件资料 SHA 绑定、创建/审核/发布三人分离、批准/拒绝/发布/撤回及重审；每次读取重新复验，问答生成结束再复验。拒审撤销旧批准、撤回后旧批准不能直接重放；单进程事务锁与原子 JSON 写入限制并发交错。
- 独立攻击复核发现并修复：同秒报告相互覆盖、effective_review 忽略后续拒绝、资料只信 manifest 声明 SHA、缓存保留旧正文/回答、身份变化后控件残留、缺单位/期间比较及数据不可用仍引用残留财务快照等路径。普通用户/跨租户不得从 report/job/answer/delivery/status/review/publish/withdraw/cancel/list 获得资源正文或详情；没有增加 data/ 静态或下载旁路。
- R7 接通私有 watchlist/显式版本检查/基线确认、1–90 天 owner 私有记忆 CRUD/撤销/版本与期限复验、memory_ids 有效性检查、同 ticker 已发布报告的事实比较；不把来源缺失/SHA 漂移当作投资结论变化，自由文本记忆不注入事实生成。
- 审计落 JSONL，记录 intent/allow/deny 与主体/目标摘要/时间/错误码/版本，不记录 token、正文或记忆内容；存储损坏 fail-closed，身份/审核/发布/审计/记忆持久化路径已加入 gitignore。
- 浏览器实测（隔离临时目录、合成 OFFLINE FIXTURE）：登录 → 无 claims 报告人工登记 → reviewer 批准 → 独立 publisher 发布 → analyst 打开/问营收得到 100 与字段锚点 → 记忆创建 active/期限 → 关注项创建/检查 unchanged → API 撤回后页面重绘清空旧正文/回答 → 聊天再次提交离线任务并完成七步，未经发布的新报告仍不可见。1366×900 宽屏和 390×844 窄屏已观察/操作，侧栏与退出按钮对比度已核验；截图和路由矩阵见 docs/review_evidence/r6_r7/。没有真实网络研究或生产数据验证。
- 最终工作树全量回归：**322 passed, 1 warning**（264.09s）；定向门禁：**164 passed, 1 warning**；compileall 与 git diff --check 成功。测试前后代码 SHA 快照逐项一致，实际日志/机器记录在 data/evaluations/r6_r7_*。冻结区无代码差异：rag/workflow 原工作树 CRLF 与 HEAD LF 的差异已明示，规范化换行后四个冻结文件均 match；实际 SHA 留存，不以时间戳证明未触碰。
- 验收结论：**当前 R6/R7 单机团队演示批次通过并停止**。这不是生产上线、零幻觉、自动完整语义证明、企业 SSO 或多机事务。完整覆盖仍依赖人工质量；记忆只是注记；固定白名单显式监测不自动联网；真实数据授权/网络预算/OCR 和 KI-001/KI-002/KI-006 保持原限制，R3 no_online_ab 不解除。没有 commit/stash/reset/clean，既有改动保留。
