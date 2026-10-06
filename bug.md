# bug.md —— 投研助手评测链路踩坑汇总（R0 / R1 阶段）

> 用途：把已犯过的错固化成"根因 + 修复 + 预防规则"，**以后不要再犯、不要重复造轮子**。
> 适用范围：R0 四象限双语评测、R1 候选池敏感性实验，以及后续任何评测器/检索改动。
> 维护规则：每修一个 bug 就补一条；不要只写"改了什么"，要写"为什么错"和"下次怎么提前拦住"。

---

## 0. 通用教训（最高优先级）

1. **先调研再开工。** 动手改评测器/检索前，先把相关代码完整读一遍：检索路径、候选池公式、现有命中判断函数、已有工具。
   不要凭"应该就是这样"动手——本轮多个 bug 都源于没先读 `rag.search()` 的候选池公式。
2. **不要重复造轮子。** 同一语义的判断逻辑只应有一处实现（DRY）：命中判断统一走 `_is_target_source()`，sha/样本指纹统一走 `_sha256_of_file()` / `_eval_set_gold_summary()`。
   改一处后必须 `grep` 全部同义表达式，避免漏改（见 BUG-5）。
3. **证据边界必须 fail-closed。** 任何"资料是否可信""样本是否被改动"的检查，结论不一致时**不跑检索、不产指标、报告写 BLOCKED**。
   绝不能"记录 drift 然后照常出 Recall"（见 BUG-2）。
4. **长耗时重跑任务不要吞 stderr。** 别用 `2>/dev/null` 包住整条链；报错会被掩盖，状态显示 failed 却看不出原因（见 BUG-7）。
5. **基线/冻结产物不要顺手重跑。** R0 的 `bilingual_r0.{json,md}` 是基线，不要为修评测器字段而擅自重跑改动它；要改先告知用户或用 `git checkout` 还原（见 BUG-6）。
6. **不可复现的对照指标不得当精确数字读。** unscoped 臂重建索引后有 ±1 波动，引用时要带容差（见 BUG-8）。

---

## BUG-1：unscoped 命中只比页码，不校验标的（P1-A，已修）

- **现象**：`unscoped_page_hit_at_k` 用 `any(_page_of(s) in target_pages for s in unscoped)`，把异标的同页码误判为命中。
  实测 `zhzh_moutai_inventory`（600519.SS，目标页 [14,57]）的 unscoped Top-4 含 `0700.HK page 57`，旧逻辑判 True，正确应为 False。
- **根因**：命中判断缺少 ticker 维度，只做了页级包含。多标的语料下，不同 PDF 的同页码必然碰撞。
- **修复**：新增 `_is_target_source(source, case)`（ticker == case["ticker"] **且** 页码在 gold page 内），scoped / unscoped 两臂统一走它；并新增 `unscoped_pages` 字段便于审计。
- **预防规则**：任何"是否命中目标页"的判断必须过 `_is_target_source()`；页级判等必须携带 ticker 维度。
- **关联文件**：`bilingual_eval.py`、`candidate_pool_eval.py`、`tests/.../test_unscoped_hit_requires_same_ticker_not_just_same_page`

## BUG-2：资料 SHA 漂移只记录不阻断（P1-B，已修）

- **现象**：`verify_materials()` 算出 drift/missing 后，代码只是记下来，仍继续 `run_mode()` 出 Recall/Top-1 报告，违反"资料不一致就拒绝可比结论"的承诺。
- **根因**：把"校验"和"阻断"拆成了两步，且阻断没真正落地——只存了 `material_drift` 字段。
- **修复**：`MaterialDriftError` + `assert_materials_comparable` + `render_blocked_report`；drift/missing 时 fail-closed：不调 `run_mode`、不产指标、JSON `status=blocked`、md 写 BLOCKED；`reattribute_payload` / `render_from_result` 也拒绝在 BLOCKED 上工作。
- **预防规则**：证据边界类检查一律 fail-closed，绝不"记录后照常出指标"。
- **关联文件**：`bilingual_eval.py`、`candidate_pool_eval.py`、`tests/.../test_material_drift_blocks_*`

## BUG-3：候选池实验目标值 100/200 写死却无法精确命中（调研不足）

- **现象**：R1 文档与命令都承诺"候选池 48 / 100 / 200"，但实际取到 48 / 108 / 204。
- **根因**：设计实验前没读 `rag.search()` 的候选池公式 `min(max(limit*12, 48), matching_count)`——不改 `rag.py` 时候选池只能取 12 的整数倍。
  在没核实可行性的情况下就把 100/200 写进文档承诺，导致事后需要解释"实际池 ≠ 目标池"。
- **修复**：对每个目标池取"不小于目标的最小可达值"（48/108/204），并在报告里同时记录目标池与实际池、说明原因；不为了凑整数去动冻结文件。
- **预防规则**：设计依赖冻结路径的实验前，**先读相关代码确认约束**；不在未核实可行性的前提下在文档/承诺里写死精确值。
- **关联文件**：`candidate_pool_eval.py`（`limit_for_pool` / `pool_from_limit`）、`docs/投研服务台升级总纲.md`

## BUG-4：R1 谎称"与 R0 同一份样本"但无运行期保障（P2-1，已修）

- **现象**：模块允许 `--eval-set another_eval_set.json`，但报告仍写"与 R0 同一份样本，未增删、未改 gold page"。样本是否被改动完全靠使用者自觉。
- **根因**："可比性声明"没有运行时校验支撑，纯文本承诺。
- **修复**：
  - R0/R1 产物都写入 `eval_set_sha256` + `eval_set_case_count` + `eval_set_gold_summary`（per-case ticker+目标页指纹）；
  - R1 启动时跨文件读取 R0 基线产物的 `eval_set_sha256`，算出 `matches_r0_eval_set`；
  - `render_from_result` 在评测集 sha 与产物记录不一致时拒绝重渲染（RENDER BLOCKED）；
  - 报告"与 R0 同一样本"措辞按 `matches_r0_eval_set` 条件输出（一致 / 不一致 / 无法校验）。
- **预防规则**：任何"与基线可比"的声明必须有运行时校验，不能靠自觉；样本集指纹 + sha 是标配。
- **关联文件**：`bilingual_eval.py`（`_sha256_of_file` / `_eval_set_gold_summary`）、`candidate_pool_eval.py`（`_read_r0_eval_set_sha` / `render_from_result`）、`tests/.../test_run_experiment_records_*`

## BUG-5：top_result_is_relevant 只比页码（P2-2，已修）

- **现象**：R0/R1 的 `top_result_is_relevant` 是 `bool(scoped_pages and scoped_pages[0] in target_pages)`，与其它命中判断（已统一成 `_is_target_source`）口径不一致。
- **根因**：上一轮修 BUG-1 时只 grep 了 `page_hit_at_k` / `unscoped_page_hit_at_k`，漏掉了 Top-1 这一处同义表达式——典型的"改了一处、漏了同义实现"。
- **修复**：新增 `top_result_is_relevant(scoped, case)`，统一走 `_is_target_source`；两评测器都改用它。
- **预防规则**：同一语义的判断逻辑只应有一处实现（DRY）；改一处后必须 `grep` 全部同义表达式（如 `in target_pages`、`_page_of(...) in`）。
- **关联文件**：`bilingual_eval.py`、`candidate_pool_eval.py`、`tests/.../test_top_result_is_relevant_rejects_wrong_ticker_same_page`

## BUG-6：基线产物被擅自重跑改动（流程问题）

- **现象**：上一轮为修 unscoped 字段，直接重跑 `bilingual_r0.{json,md}`，用户事后才被问"是否接受动 R0 产物"。
- **根因**：把"修评测器"和"重跑基线"混在一起，没有先确认基线产物能否被改。
- **修复**：本轮重跑 R0 是为了补 `eval_set_sha256` 字段（R1 跨文件校验需要），已说明必要性；用户仍可用 `git checkout -- data/evaluations/bilingual_r0.*` 还原。
- **预防规则**：基线/冻结产物（R0 json/md）不要为评测器修复而顺手重跑；要改先告知用户并获得确认，或说明还原方式。

## BUG-7：链式 `&&` + `2>/dev/null` 重跑评测隐藏失败（工程习惯）

- **现象**：后台跑 `bilingual_eval && candidate_pool_eval` 时整条链 `2>/dev/null`，模块报错被吞，状态显示 failed 却看不出原因，浪费一次排查。
- **根因**：stderr 被重定向到 /dev/null，且 `&&` 让第一个模块一报错就短路，日志全空。
- **修复**：改为前台分跑、保留完整 stderr；先读日志再判断，不靠猜。
- **预防规则**：长耗时/重跑任务不要吞 stderr；链式命令要么拆开前台跑，要么保留完整日志；"failed" 先看日志再看猜测。

## BUG-8：unscoped 对照臂不可 bit 复现却当精确指标读（认知偏差）

- **现象**：R0 重跑后跨标的来源计数从 2.0625→2.0312、个别样本 ±1 波动；重建索引后 Chroma 距离并列/浮点排序会让 unscoped Top-4 构成在个别样本上变。
- **根因**：设计时把 unscoped 臂当精确指标引用，没验证其稳定性；scoped 臂可完全复现，但对照臂不行。
- **修复**：结论层明确"跨标的污染计数有 ±1 容差，不精确读"；scoped 臂才是可复现主指标。
- **预防规则**：对照指标的稳定性要在设计时就验证；不可复现的指标不得作为精确数值引用，要带容差。

---

## 已固化的评测口径（不要再推翻）

- `failure_reason` 只按 **scoped** 臂判定主因；unscoped 臂的跨标的污染是独立对照指标，不顶替主因。
- 任何"是否命中目标页"必须走 `_is_target_source()`（ticker + 页码）。
- 资料 sha256 漂移/缺失必须 fail-closed。
- 样本集不可变：R1 必须与 R0 同一 `eval_set_sha256`（运行时校验）。
- Top-K 冻结为 4，与 Apple 20 条基线一致。
- 结论一律不外推为业务准确率；样本、资料 sha256、命令要可追溯。
- 冻结文件 `rag.py` / `workflow.py` / `llm_generation.py` / `safety.py` 改动必须先提变更依据 + 对照评测 + 用户确认。

## BUG-9：腾讯财报请求被“公司范围未确认”误拒（2026-10-04，代码已修，UI smoke待测）

- **截图原句**：`给我一份今年的腾讯财报`、`给我一份2025年的腾讯财报`。页面已显示当前资料为 `0700.HK` 腾讯官方资料。
- **实际表现**：两次都提示“无法确认问题只涉及请求的公司；请写明单一已收录公司与期间”。这不是“腾讯未收录”，而是**已识别公司仍误拒**。
- **本地复现（当前工作树）**：两句的 `references()` 都返回 `{'0700.HK'}`；`route_message()` 都返回 `company` 且 ticker 为 `0700.HK`；`CompanyAnswerService.answer('0700.HK', ...)` 均返回 `refused / COMPANY_SCOPE_UNVERIFIED`。尚未对用户正在运行的后端进程作版本核对。
- **已确认原因**：`chat_session.py` 的报告指令 `_RESEARCH` 没覆盖“给我一份……财报”，于是进入普通 `company` 问答。`company_qa.py` 的最终范围检查调用 `unverified_company_subject(..., include_non_fact=True)`；普通请求词如“给我一份”“财报”被当成残留的未知实体，即使公司已识别为腾讯也遭拒。**这是意图路由与主体检测职责混淆，不是检索召回问题；不是修一条 UI 文案就能解决。**
- **期间歧义**：截至 **2026-10-04**，“今年”按自然年份是 **2026**；当前本地清单对腾讯收录的是 **2025 年年报**。不能把“今年”静默替换成 2025，也不能声称已经有 2026 年全年年报。应明确询问用户要 2026 年已披露期间资料，还是当前已核验的 2025 年年报；是否能提供前者必须先核验清单。
- **期望行为**：已识别单一公司不再收到“公司未确认”；明确 `2025 年腾讯财报` 时，区分“原始官方年报/年报事实问答/生成研究报告”，复用现有受控能力提供可执行入口，不能把普通财报请求硬当字段问答，也不能凭空生成整份财报。对于“今年”先处理期间歧义和资料可用性。未收录、多公司、季度与全年不匹配、来源失效仍需拒绝或澄清。
- **修复方向（待实现，不等于已定具体代码）**：先读 `chat_session.route_message()`、`company_qa.references()`、`unverified_company_subject()`、`CompanyAnswerService.answer()` 及现有报告任务/资料展示逻辑；在共享入口区分**公司身份验证**与**用户请求意图**，复用目录和来源校验。不要只给 `_NEUTRAL_WORDS` 添“财报”，不要在前端复制一套公司识别器，不要把自然年与最新报告年混为一谈。
- **验收矩阵**：两条截图原句（含当前选中腾讯和空白上下文）；`腾讯/Tencent/0700.HK`、其他已收录公司；`2025/今年(2026)/2024/季度`；未知公司、跨公司、旧报告上下文、证据 SHA 漂移。分别检查**路由、最终服务、API、页面**结果，不可只断言提示文案或只跑一条成功案例。
- **关联**：`investment_assistant/chat_session.py`、`investment_assistant/company_qa.py`、`investment_assistant/web_app.py`；与 BUG-10 属于同一类“自然语言残余词误判”，但入口和实际拒答层不同。
- **状态**：**代码已修复，回归已通过；浏览器实机复测待重启服务后完成。** 已覆盖路由、任务参数、未知公司、多公司和跨期矩阵；未授权时仍不生成任务。

## BUG-10：平安银行“2025 年收入怎么样”曾被误判未知公司（已修，防回归）

- **原句**：`平安银行2025年的收入怎么样`。资料目录已收录 `000001.SZ` 的 2025 年官方年报。
- **当时根因**：`unverified_company_subject()` 把常见追问“怎么样”当成未知公司残余字；绕过路由后，通用字段抽取对收入、单位和同比比较列的证据绑定仍不足。
- **已做修复**：现有 `company_qa.py` 覆盖“怎么样”，并为该公司的已核验年度收入比较加入原 PDF 字节、SHA、页码、单位及行值校验；对应 `tests/test_company_qa.py` 中有原句与负面边界用例。**这些既有实现应优先复用或参考，不要再在前端单独做同类补丁。**
- **预防规则**：每次扩充自然语言问法，必须分别检查公司命中、意图、字段与期间、最终服务、证据和 UI；成功路径之外同时覆盖跨公司、无资料、跨期和证据漂移。固定一家公司的一句可用不代表所有相似表达都可用。
- **状态**：已有代码与测试；本条记录历史问题，避免与 BUG-9 的报告请求路由混为一谈。

---


## BUG-11：公司发现与聊天上下文断开，市场确认后无法继续追问（已修）

- **现象**：用户先问“给我一份今年阿里巴巴的收入”，系统返回港股/美股候选；随后输入“A股”或直接输入“收入怎么样”时，系统把它当成全新问题，要求重新填写公司和年份。侧栏“公司发现”查询也只显示结果，不能绑定到当前聊天。
- **根因**：发现结果只存在于本轮 UI 展示，没有写入会话上下文；路由没有保存原始问题、候选列表、已选 ticker 和市场，也没有把市场选择作为上一轮问题的延续。
- **修复**：`chat_session.new_context()` 增加 `discovery_query`、`discovery_candidates`、`discovery_ticker`、`discovery_market`；市场/候选按钮复用原始问题调用发现 API；确认市场后绑定 ticker，并继承原问题年度，下一句“收入怎么样”进入同一公司的受控问答链路。侧栏候选增加“在当前对话使用”按钮。
- **附带修复**：路由提示候选的展示名称不再回显整句用户问题，改为命中的公司别名；候选诊断收进折叠区域，主界面只保留可执行的市场选择。
- **预防规则**：任何多轮澄清必须持久化“原问题 + 候选 + 已选主体 + 市场 + 年度”；UI 的候选展示不能只读，必须有继续动作；不能让用户重复输入上一轮已确认的信息。
- **关联文件**：`investment_assistant/chat_session.py`、`investment_assistant/web_app.py`、`investment_assistant/company_discovery.py`、`tests/test_chat_session.py`
- **状态**：已修复；定向测试与全量测试通过（760 passed / 1 skipped）。

---

## 下次改问答链路前的防重复造轮子清单

1. 先读 `bug.md` 和现有 `chat_session` / `company_qa` / 中文证据服务 / 报告任务路径，不平行创建公司别名、年份或来源校验规则。
2. 用用户**原句 + 当时会话上下文**复现，记录公司识别、路由、服务状态与错误码；先定位拒绝发生在哪层，再谈修复。
3. 将“未收录公司”“请求意图不支持”“报告期间不匹配”“缺字段证据”“工具/来源不可用”分成不同错误，避免都显示成“公司未确认”。
4. 对“今年/最新”写明当前日期、自然年、财报报告期和清单内可用年度；不静默转换。
5. 修共享根因并加正反两类回归；完整链路验证包括 API 与页面。若只更新本文，必须保持 **未修复** 状态。
