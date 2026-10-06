# R6/R7 交付验收记录（2026-09-27）

## 范围

WorkBuddy 中断时四个新模块没有接入 API/任务/UI，也没有新测试。主助手接管后补齐当前 R6/R7 批次，保留冻结区与线上默认检索；没有派发新 WorkBuddy 任务，没有 commit/stash/reset/clean。只验收单机本地团队演示，不声明生产上线或检索性能改善。

## 服务端路由矩阵

全部 `/api/*` 数据路由由统一 middleware 认证与审计；唯一公开项为 `/api/health`。以下列表由当前 API 源码 AST 提取：

| 方法 | 路径 | 实现 |
|---|---|---|
| DELETE | `/api/memories/{memory_id}` | `remove_memory` |
| DELETE | `/api/watchlists/{watch_id}` | `remove_watch` |
| GET | `/api/health` | `health` |
| GET | `/api/me` | `current_identity` |
| GET | `/api/memories` | `memories` |
| GET | `/api/memories/{memory_id}` | `get_memory` |
| GET | `/api/report-jobs` | `list_report_jobs` |
| GET | `/api/report-jobs/{job_id}` | `get_report_job` |
| GET | `/api/report-jobs/{job_id}/report` | `get_report_job_report` |
| GET | `/api/reports` | `list_reports` |
| GET | `/api/reports/{report_id}` | `get_report` |
| GET | `/api/reports/{report_id}/delivery` | `report_delivery` |
| GET | `/api/reports/{report_id}/review` | `review_preview` |
| GET | `/api/reports/{report_id}/status` | `report_release_status` |
| GET | `/api/source-health` | `source_health` |
| GET | `/api/watchlists` | `watches` |
| POST | `/api/answers` | `answer_report_question` |
| POST | `/api/memories` | `add_memory` |
| POST | `/api/report-jobs` | `create_report_job` |
| POST | `/api/report-jobs/{job_id}/cancel` | `cancel_report_job` |
| POST | `/api/reports` | `generate_report` |
| POST | `/api/reports/compare` | `compare_reports` |
| POST | `/api/reports/{report_id}/publish` | `publish_reviewed_report` |
| POST | `/api/reports/{report_id}/review` | `review_report` |
| POST | `/api/reports/{report_id}/withdraw` | `withdraw_reviewed_report` |
| POST | `/api/watchlists` | `add_watch` |
| POST | `/api/watchlists/{watch_id}/baseline` | `confirm_watch_baseline` |
| POST | `/api/watchlists/{watch_id}/check` | `check_watch` |
| PUT | `/api/memories/{memory_id}` | `revise_memory` |
| PUT | `/api/reports/{report_id}/claims` | `author_claim_set` |

### 资源/角色策略

- 报告、任务的元数据按 tenant 隔离；跨租户与不存在统一 404。旧报告缺可信归属仅 admin 可查看迁移元数据，不能审核发布或问答。新生成报告身份从后端主体绑定。
- 创建任务/同步报告要求 analyst/admin；取消要求发起人/admin。source-health 是跨任务运维快照，只允许 admin，不向普通团队用户泄露全局数据。
- 未发布正文/问答为 409。原始文件/下载路由未增加，也没有静态挂载 data/ 的旁路。
- 审核预览/claim 登记仅 reviewer/admin 且不是创建人；待审正文仅在该授权审核工作台预览。普通用户、publisher 没有未发布正文权限。
- 批准必须逐条 supported、完整 claim 集与人工整篇覆盖声明；结构门禁不合格拒绝。发布要求独立 publisher/admin，创建/审核/发布的 actor 不得重合。拒审使旧批准失效，撤回后的旧批准不可直接重放发布。
- 每次发布和每次读取重验 MD/JSON、claim 集、tenant/report_id、白名单资料实际字节。回答生成结束再次复验，版本变化时结果不外发。单进程版本事务锁确保受保护动作不交错；不是多进程文件事务。
- watchlist 与记忆是 tenant+owner 私有资源，列表/读取/写入/删除不可跨租户或越 owner；记忆 TTL 1–90 天，过期/撤回/漂移时不返回内容。memory_ids 只校验并返回用户注记元数据，不影响事实回答。

## 已实际走过的浏览器交互

烟测目标为 loopback 的隔离临时目录合成 OFFLINE FIXTURE，没有触碰真实报告或网络研究：

1. reviewer 登录，在无 claims 的报告上手工登记 snapshot claim；保存成功后结构门禁显示 1 条 supported。
2. reviewer 逐条勾选、整篇覆盖声明、填写理由并批准；本地审核记录持久化成功。
3. 退出并用 publisher 登录，独立发布；页面显示 published / approved。
4. 退出并用 analyst 登录，从历史报告打开该已发布版本；对“营收是多少”实际得到合成数值 100 及 financial_snapshot.revenue 锚点。
5. 浏览器创建有限期记忆，页面显示 active / 到期时间；创建报告版本关注，显式检查显示 unchanged / 空 diff。
6. 在另一主体的本地测试 API 撤回该版本，再触发分析员页面交互；界面先清除旧正文/回答，显示撤回/待审提示，不再出现旧 100 回答。
7. 查看 1366×900 宽屏及 390×844 窄屏；窄屏实际操作收起侧栏后无主区横向溢出，聊天输入可达；退出按钮对比度已修正。临时视口覆盖已恢复。

追加：浏览器在撤回上下文中再次用聊天提交“分析 AAPL 服务业务”；离线图实际完成七个节点，页面显示 actor=analyst、100% / completed，但未经人工审核发布的新报告仍不释放正文。

截图：desktop_qa.png、mobile_qa.png、withdrawn_cache_cleared.png。本次浏览器不等于真实资料/真实网络 E2E；定时监控、所有移动设备与生产权限基础设施未验证。

## 验收执行状态

- 当前最终工作树：`.venv\Scripts\python.exe -m pytest -q --tb=short` → **322 passed, 1 warning**（264.09s），没有失败。警告为既有 anyio/Starlette 弃用提示。
- 定向（team/API/job/QA/evidence/source/market）：164 passed, 1 warning。新增 team boundary 测试为 29 个执行实例；全部走离线/合成输入，不是线上安全评分或金融业务准确率。
- compileall 通过；git diff --check 无 whitespace error，只有既存 LF/CRLF 提示。
- 测试前后 investment_assistant/*.py、tests/*.py 与离线烟测脚本的 SHA 快照逐项一致，见 data/evaluations/r6_r7_code_sha256.json；验收机器记录见 r6_r7_acceptance.json，实际日志见 r6_r7_full_test.log。
- 冻结区 git diff 为空。rag.py/workflow.py 工作树为 CRLF、HEAD 为 LF，因此原始 SHA 不同，但规范化换行后四个冻结文件均与 HEAD 相同；llm_generation.py/safety.py 原始字节也相同。实际 SHA 和换行统计保存在 r6_r7_frozen_sha256.json，不用 mtime 当作未修改证明。
- 已完成当前批次并停止；没有提交、清理旧工作树或进入新阶段。

## 已知剩余边界

- 没有自动完整 claim 抽取；真实报告要由人登记覆盖并核验，不会自动发布。人类完整覆盖声明仍依赖审核质量，结构验证不证明文本蕴含，不承诺零幻觉。
- 身份/JSON/日志是单服务、一个 worker；不防本地特权管理员篡改，不是 SSO、多机事务或外部可信审计。
- watchlist 是固定白名单文件/报告 ID 的显式版本检查，不是自主新文件发现或联网定时抓取。相同状态检查复用同一持久化结果及时间；source_unavailable/needs_review 不自动等同结论变化。
- 记忆是用户注记，不是模型可自由引用的事实库；撤回不能抹除用户已经看见/下载到外部的历史内容，只禁止后续服务端读取并在重绘时清空会话缓存。
- 未切换检索、不放开 KI-001/KI-002/KI-006；R3 no_online_ab 保持。真实行情/财务源延迟、OCR 和生产运维仍在既有已知边界中。
