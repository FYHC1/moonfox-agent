# Moonfox Agent 设计文档

当前已确认：**两个独立核心包 + 一个业务应用包 + 一个显示包，产品能力扩展化。** 阶段 A–E 已实现：独立核心、持久运行时与扩展宿主、SessionHub/授权配置、HTTP/WS 协议、独立 WebUI、共享提供商/模型管理，以及可选的人设与隔离记忆。QQ 客户端与真实联调属于阶段 F。详细验证与交付记录见 [交接摘要](HANDOFF.md)。

## 阅读顺序

**要开始写代码，先读 [交接摘要](HANDOFF.md)** —— 一页覆盖需求、已确认决策、架构、目录、契约、环境事实、实施路线和第一步。

1. [交接摘要](HANDOFF.md)：**新会话入口**。决策、禁止事项、阶段 A–E 进展和阶段交付规则。
2. [整体架构](ARCHITECTURE.md)：与 pi 的职责对应、四个主包、完整目录、依赖边界和实施顺序。
3. [接口设计](DESIGN.md)：核心运行契约、应用扩展 SDK、前后端协议、隔离与验证要求。
4. [mini-pi 参考](../mini-pi/DESIGN.md)：模型/Agent/会话/应用分层及关键接缝。保留参考设计，不将伪代码直接视为已验证实现。
5. [应用包说明](../../packages/moonfox-app/README.md)：阶段 C 的实际 API、存储耐久性、扩展授权与生命周期、prompt/压缩和示例。
6. [阶段 D 说明](STAGE-D.md) 与 [HTTP/WS 契约](STAGE-D-PROTOCOL.md)：启动/开发、配置/预算/恢复、协议与前端验收。
7. [提供商与模型管理](PROVIDER-MANAGEMENT.md)：共享持久目录、Chat Completions/Responses/Messages 三种实际协议、凭据安全、名称默认 ID、模型默认值/会话覆盖与管理 API。
8. [阶段 E 说明](STAGE-E.md)：可选包安装、配置/管理、隔离、后台抽取预算和遗忘/删除边界。
9. [qq-bridge 借鉴评估](QQ-BRIDGE-REFERENCE.md)：固定提交的只读调研与阶段 F 接入取舍，不是 QQ 运行验收。

## 包布局（QQ 待阶段 F）

~~~text
packages/ai/            独立模型 API，对应 pi-ai 的职责
packages/agent/         独立 Agent 核心，对应 pi-agent-core 的职责
packages/moonfox-app/   业务应用，对应 coding-agent：runtime/services/扩展宿主/config/server
packages/web-ui/        浏览器前端，对标显示层：输入、渲染、配置表单与审批交互
extensions/persona/    会话级扩展：人设
extensions/memory/     会话级扩展：记忆与隔离存储
extensions/qq/         待实现的宿主级扩展：薄 OneBot 11 客户端，SnowLuma 优先
~~~

代码依赖：moonfox-app → agent → ai；扩展只使用 moonfox_app.extension_api；web-ui 通过 HTTP/WS 协议连接业务包 server。

会话运行时与宿主首版在同一业务包，内部保持分层，不提前单独发布。以后出现第二个真实复用宿主再评估抽包。首版不设多 Agent orchestrator：会话并发由 SessionHub 管理。

## 职责约束

- 核心不读取产品配置，不依赖业务存储、扩展、Web 框架或 QQ；只安装 ai + agent 即可使用。
- 业务包拥有会话、压缩/重试、配置、权限、预算和扩展生命周期；应用的 headless 模式不要求 WebUI 或 HTTP/WS 服务。
- 显示层只维护交互/显示状态，关闭浏览器不结束后台 Agent 或 QQ；业务决定以后端为准。
- 业务包内的 extensions 是注册/派发机制，根目录 extensions 是具体实现；扩展系统先于业务扩展完成。
- 域绑定、身份与访问授权是应用服务的强制职责，不是可随意关闭的插件。
- 提供商/模型/凭据归应用共享目录，WebUI 与后续 QQ 共用；AI 只接受注入的传输和请求快照，不读取产品配置。

## 保留的用户决策

- QQ 采用直连协议端方案，协议端为 SnowLuma。
- 人设采用人设内回答，由人设扩展管理，不改变工具权限。
- QQ 聊天记忆与 Agent 对话记忆硬隔离；WebUI 查看 QQ 会话不改变其域。
- max_turns_per_run 外部可配置，应用解析，核心只消费运行快照。
- 核心保留 steering/follow-up；业务 inbox、连发合并与主动策略位于应用/扩展。

## 当前落点

- 四个主包已独立；会话运行时在业务包内部 runtime，核心/headless 无需 Web 或业务扩展。
- 具体人设/记忆只通过 `moonfox_app.extension_api` 接入；宿主负责绑定、权限和预算，扩展拥有自己的存储。
- 前端只用 HTTP/WS 和服务端导出 schema，扩展配置/记忆管理不投递模型输入。
- E 已覆盖两域多会话的存储/检索/工具/快照/作业/删除隔离；无浏览器 QQ 运行和真实 OneBot 联调留在 F。

[历史草案](history/DESIGN-v1.md) 仅供查阅讨论过程，不作为当前实施方案。
