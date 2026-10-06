# 当前任务（唯一入口）

> 更新：2026-09-27。WorkBuddy 因费用耗尽中断；用户已授权由主助手直接完成当前 R6/R7 批次，不再派发 WorkBuddy。

## 当前状态

**R6/R7 接管实施与当前工作树验收完成，已停止。** 最终全量回归 `322 passed, 1 warning`（264.09s）；定向门禁 `164 passed, 1 warning`；compileall / git diff --check 通过。测试前后源代码 SHA 快照一致。禁止重复派发旧任务或自动扩展阶段；交付记录已回填审阅日志和验收证据。

## 范围

任务原规范见 `docs/R6_R7_BATCH_TASK.md`。实现服务端身份/租户授权、人工 claim 登记/完整覆盖审核、职责分离发布/撤回、版本实际字节复验、审计与单会话 UI；在此边界上实现显式版本关注、有限期 owner 私有记忆与同标的事实比较。

没有改冻结工作流或线上检索路径，没有提交/stash/reset/clean。R0–R5 历史结论保留；R3 no_online_ab、KI-001/KI-002/KI-006 不因本批次完成而解除。自动语义证明、完整自动 claim 抽取、生产 IdP、多机事务、真实网络/OCR 不在本批次验收范围。

详细交付、路由矩阵、烟测复跑与截图见 `docs/review_evidence/r6_r7/ACCEPTANCE.md`、`README.md`、`docs/DECISIONS.md`。不自动进入 R8 或任何检索切流；如需新增业务阶段，先由用户授权新的当前任务。
