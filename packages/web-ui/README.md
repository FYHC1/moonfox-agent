# Moonfox Web UI · Stage D

独立的中文 Agent 工作台。前端只负责显示服务端事实、提交 DTO 与维护瞬时连接；不拥有 Agent、推理状态机、业务队列或持久化凭据。管理员可在无会话时管理多个提供商、模型与生成参数；离线预设回复仅通过后端 `--demo` 显式启用。

## 设计约定

- **任务**：让一段输入的接收、排队、执行、审批和结束都可辨认；出错时保留上下文，所有重试由用户明确触发。
- **颜色**：墨蓝画布 `#0d121c`、侧栏 `#101722`、面板 `#151e2b`；暖白正文 `#ede9e3`、灰蓝次文 `#a1abba`；暖铜强调 `#e9a477`、紫色仅用于静态月晕。成功/警告/危险分别使用青绿、琥珀、浅珊瑚，并始终配文字。
- **字体**：系统中文无衬线；正文 14/22，辅助 12/18，标题 20/28；工具参数和协议水位使用系统等宽字体。无远程字体依赖。
- **节奏**：4px / 8px 间距；控件圆角 8px，卡片 12px，主容器 16px。主操作和移动端触控目标至少 40–44px。
- **动效**：悬停/按下即时反馈；新卡片使用短促、弹性减速的位移与淡入。流式状态通过文字和三点指示，不展示模拟进度；遵守 `prefers-reduced-motion`。
- **Do**：语义控件、明确焦点、稳定网格、长文字换行、响应式抽屉；把已接收与已完成分开；配置冲突必须显式重新载入。
- **Don't**：不渲染用户 HTML、不存 token、不在切换/关闭页面时停止任务、不自动重试推理、不暗示 QQ 发信能力、不提供无后端支持的假按钮。

## 运行

```powershell
cd packages/web-ui
npm.cmd ci
npm.cmd run dev
npm.cmd test
npm.cmd run build
```

Vite 开发服务默认 `http://localhost:5173`；`/api`（含 WebSocket）代理到 `http://localhost:8765`。连接表单默认使用当前 origin；生产构建使用相对静态资源，可由后端托管。宿主必须将实际浏览器 origin 加入显式 allowlist。连接其他后端时填写 **origin**（不含路径、查询、token），后端同时需要正确 CORS 与 WS Origin 配置。

后端启动命令见 [阶段 D 说明](../../docs/moonfox/STAGE-D.md)；本包不会自行启动或伪装生产后端。管理员 token 只留在 JS 内存，刷新后需重新输入；退出连接不会取消正在运行的任务。

## 协议与依赖

唯一契约源：`moonfox_app.server.schema`、`moonfox_app.config.schema/providers` 与 `docs/moonfox/STAGE-D-PROTOCOL.md`。服务端 exporter `scripts/export_protocol.py` 负责生成 `src/generated/protocol.json` 和 `src/generated/fixtures.json`；先运行 exporter，再安装/构建/测试。**本包不维护生成内容或平行 DTO schema。** AJV2020 对每个完整导出 schema 独立编译，保留其 `$defs`。

运行依赖仅 `ajv`；开发依赖 `vite` 与浏览器验收用 `@playwright/test`。`node:test` 覆盖契约、事件投影、HTTP 错误和连接竞争；浏览器集成可由宿主针对真实服务执行。任何验收 mock 都不参与生产包。

`npm.cmd run test:browser` 默认自动启动仅本机的 `browser/fixture-server.mjs` 和 Vite，运行全部 11 项交互/布局测试（含四项管理场景），退出后关闭服务。夹具导入服务端生成的 schema/数据，只使用固定测试 token，不进入生产构建。Windows 默认使用本机 Edge；其他环境先安装 Playwright Chromium，或设置 `MOONFOX_BROWSER_CHANNEL`。

验证真实后端时设置 `MOONFOX_UI_BASE_URL`、`MOONFOX_TEST_BACKEND`、`MOONFOX_TEST_TOKEN` 后运行 `test:browser`；只有基础/真实会话用例执行，夹具专用的错误注入测试跳过。不要对含真实用户数据的服务执行这些创建/输入测试。

## 边界与假设

- HTTP 入口是 `/api/v1`，同源是默认部署方式。
- 仅管理员可读取配置目录；没有目录或没有 `configure` capability 时配置保持只读。模型的思考级别使用服务端允许的字符串，不捏造枚举。
- 会话域不可编辑，浏览器只创建 agent 会话；QQ 域仅展示，不提供 QQ 操作。
- session capability 决定操作可用性；`compact` 不存在时完全不展示该按钮。
- 输入网络错误保留原始 request/idempotency key 供手动重试；没有客户端持久队列。WS 重连只重取快照与订阅，不重放输入。

## 提供商、模型与参数

侧栏“提供商与模型”仅管理员可见，空会话目录也能打开。支持 OpenAI-compatible/Anthropic 协议、CRUD/CAS、启停、显式检测/发现模型、手工模型 ID、能力与上限、默认模型。发现只访问模型列表，不发送聊天或生成请求。

API 密钥只写：空白保留、填写替换、勾选明确清除；提交前清空密码输入，失败不回显服务端原文，不写 localStorage/sessionStorage。远端 HTTPS，本机可 HTTP；修改 stored-key 地址需重新输入/清除密钥。非 Windows 默认环境变量模式。

模型参数作为默认值；会话配置下相同控件用于覆盖，空值继承、0 是有效值、`stop=[]` 明确清空停止词。模型输出上限与逻辑输入预算独立，服务端最终限制；管理保存不修改活动 run，不自动切换既有会话。详见 [提供商管理](../../docs/moonfox/PROVIDER-MANAGEMENT.md)。
