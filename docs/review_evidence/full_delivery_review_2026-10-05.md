# 全量数据源闭环独立复核（2026-10-05）

## 结论

WorkBuddy 交付不是“只有 P1-B”，代码已经覆盖 P1-A、公司候选发现、受控 onboarding、结构化数据工具边界、API 和聊天路由。但交付报告的“全量闭环完成”需要降级为：**代码闭环已实现，真实外部源和阿里巴巴官方证据仍未完成生产验收**。

## 独立证据

- 当前 HEAD：`abd5514`；未提交、未暂存；冻结区 `rag.py` / `workflow.py` / `llm_generation.py` / `safety.py` 的 diff 为空。
- 定向测试：139 passed。
- 全量测试：**759 passed / 1 skipped / 0 failed**（2026-10-05）。
- `import investment_assistant.web_app`：已修复并通过；裸 import 仅用于静态/启动诊断，正式运行仍必须使用 `streamlit run`。
- 实际 API 夹具复核：
  - 阿里巴巴无市场：`ambiguous`、候选 `9988.HK` / `BABA`、`verified=false`、`answerable=false`。
  - `9988.HK` 未接入官方材料：`MATERIAL_NOT_ONBOARDED`，不返回数字。
  - 已接入的 `0700.HK`、`000001.SZ` 可返回带来源、SHA、页码和限制的答案。

## 本轮修复

1. `chat_session.py` 增加受控公司发现回调：未知公司/未知代码先进入发现，不再直接落到笼统提示；候选结果不能直接进入财务问答，也会清除旧报告上下文，防止跨标的证据污染。
2. `web_app.py` 聊天层接入 `/api/company-discovery`；未知主体显式启用候选源，候选仍保持 `candidate` 证据等级，不提升为官方事实。
3. `web_app.py` 初始化裸 import 的身份变量，避免登录分支在无 Streamlit ScriptRunContext 时落入 `NameError`；已用 `.venv` 实测导入成功。
4. 新增聊天路由回归测试，覆盖未知公司发现和未知代码发现路径。

## 尚不能宣称完成的部分

- `.venv` 未安装 `baostock`、`akshare`、`mcp`，真实网络适配器未做真实源 smoke；当前只证明了懒加载、fake client、错误边界和离线契约。
- `data/onboarded_materials.json` 与 `data/company_onboarding_allowlist.json` 仍为空；阿里巴巴官方年报没有完成 URL、SHA256、页数、文本层和字段锚点的人工审批链。
- MCP 传输层仍未暴露；当前交付的是内部白名单工具边界。
- 未使用真实凭证做浏览器 UI smoke；重启 API/Streamlit 后还应完成一次登录、未知公司发现和已收录公司问答。

## 交接验收顺序

1. 先在隔离环境安装并验证可选依赖，执行真实源 smoke；失败必须保留 `source_unavailable`，不能伪造 available。
2. 选择阿里巴巴主体（`9988.HK` 或 `BABA`）与年度，从 HKEX/SEC 官方文件完成审批、SHA、页数、前三页公司名和字段锚点登记。
3. 重启 API 与 Streamlit，使用测试身份完成 UI smoke：登录 → “阿里巴巴 2025 年收入” → 市场澄清/待接入提示；再验证腾讯/平安银行已核验字段回答。
4. 只有以上三项均通过，才可以把项目描述为“候选发现 + 官方资料接入 + 证据约束问答闭环”；否则简历应写成“受控接入流程已实现”。

## 后续 UI/上下文修复（同日）

复核用户截图后发现，原交付虽然有公司发现端点，但结果没有回写聊天上下文：市场选择会变成一条新问题，侧栏查询也只是只读展示。现已补齐：

- 聊天发现结果保存原问题、候选列表、市场和 selected ticker；
- “港股 / A股 / 美股”及候选按钮复用原问题，不要求用户重新输入；
- 后续“收入怎么样”继承原问题年度并进入同一 ticker 的受控问答；
- 侧栏候选支持“在当前对话使用”；
- UI 显示“已关联标的”，候选诊断折叠展示；
- 修正路由提示把整句用户问题当作公司名称的展示问题。

全量测试在本轮最终状态为 **760 passed / 1 skipped / 0 failed**。
