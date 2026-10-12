# qq-bridge 借鉴评估（阶段 F 输入）

用户提供项目：[Derpyu520/qq-bridge](https://github.com/Derpyu520/qq-bridge)。本次只读检查提交 **`9df6a7e7fc5abcb36793337f778483bd442a3d2d`**；未安装依赖、未执行其脚本、未进行真实 QQ 联调。以下判断来自实现与测试源码，不是运行验收结果。没有复制其代码或第三方动作目录。

## 结论

**借鉴 QQ 边界上的机制与踩坑用例，不替换 Moonfox 架构。** 对方是 QQ ↔ DeepSeek Harness 的 Node 桥接：SnowLuma SDK 的 WS 接收事件，HTTP 发送动作，MCP 承载模型工具。不是可直接接入 Moonfox 的 Python 插件，也没有可直接复用的薄 OneBot echo 实现——请求关联在其 SDK 依赖内部。

Moonfox 继续保持：自写薄 OneBot 11 客户端、SnowLuma 优先/NapCat 备选、首版 reverse WS Universal；具体 QQ 扩展只依赖 extension_api。共享提供商/模型目录、宿主权限/预算、durable inbox/outbox 和 domain/scope 隔离不退回桥接层。

## 有价值的机制

| 机制 | 源码依据（固定提交） | Moonfox 取舍 |
|---|---|---|
| 连接与账号健康分开 | [订阅 heartbeat/status](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L10792-L10852) | F 区分 WS、账号登录、事件接收健康；不能拿 socket open 当在线证明。其 `status.good` 含 SnowLuma 特性，要在采用版本联调。 |
| 双 token/端口不能混用 | [连接信息与探活](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/snowluma-conn.js#L45-L173) | 提示用户核对具体连接的 token/端口。首版只支持已声明模式，不照搬扫描其它账号配置、尝试多把密钥和自动改写端点。 |
| 引用归属与负缓存 | [resolveReplyInfo](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L8963-L9027) | 引用 ID 不能单独作为读取授权；绑定 bot/会话目标后校验返回归属，再注入低优先级参考。缓存键也带完整范围。 |
| 群触发、合并窗口、原因升级 | [触发条件](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L9314-L9349)、[合并调度](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L9655-L9719) | 可借鉴段级 @、引用机器人、短关键词边界与有界 debounce；合并窗口里出现明确 @ 时提高优先级。首版不默认开启概率插话或无限自主唤醒。 |
| 结构化发送与文本拆分 | [URL/Unicode 分段](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L6840-L6950)、[发送校验](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L7026-L7064) | 用数组消息段发送文本，不解析模型输出里的 CQ 指令；限制 @all。按长度拆分时保护 URL/Unicode，避免照搬“空格表示分条”的人设产品语义。其负数 message_id 处理也应加入协议夹具。 |
| 发送时刻复检权限 | [captureSendGuard](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L7490-L7520) | outbox 入队授权不替代 transport 前复检；配置撤销后取消尚未发送内容。超时仍是 unknown，不能因串行队列就假定可安全重发。 |
| 媒体与转发访问收窄 | [图片输入边界](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L7151-L7245)、[已见转发 ID](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/src/bridge.js#L9081-L9106) | 后续媒体能力按本会话已见资源授权，限制数量/字节/像素并校验内容类型；不能让模型自由指定本地路径或内网 URL。本阶段 E 不声称完成 QQ 媒体验收。 |
| 工具声明与执行双层限制 | [schema restrict 与执行 guard](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/plugins/qq-agent-presets/qq-tool-restrict.mjs#L96-L203) | 只声明宿主允许的工具，执行时仍强制授权；不提供能调用任意 OneBot action 的万能工具。使用进程内 capability，不把会话令牌放进模型 prompt。 |

## 不照搬的部分

- `src/bridge.js` 约 1.1 万行，混合平台接入、DSH、控制台与社交策略。Moonfox 将连接、codec、触发和 sender 拆在 QQ 扩展里，不复制单体结构。
- 对方主接收链没有按 OneBot message_id 的持久去重；离线队列为内存，发送链也不是 durable outbox。文本相同去重、转发 ID 见过列表、工具已发送标记都不能替代 Moonfox 的 bot + target + message_id 原子入队。
- 对方会话记忆是社交状态 JSON，没有 Moonfox 的 agent/qq 结构性域隔离；不能拿其缓存/画像结构直接当本项目记忆库。
- DSH Cookie/RPC/mux、preset 与 MCP 是对方宿主特有适配，不进入 Moonfox 核心或通道扩展。
- `src/core/session-router.mjs` 在所查提交中没有接入主运行链；不能把该文件的拟议路由当作线上已实现行为。

## 许可与联调边界

[LICENSE 的 MIT 正文](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/LICENSE#L1-L21) 覆盖项目自身代码；若将来实质复制，需保留版权和许可声明。该文件后半部分同时说明 SnowLuma 及 SDK/MCP 的独立许可问题，并提出移除这些依赖。本轮只参考机制，不复制 SDK、动作目录快照或第三方组件，也不替上游部署/启停服务。

这些上游说明不是本项目的法律保证；采用 SnowLuma 的具体版本、部署和分发方式前应独立核对其当时条款。网络协议边界不自动免除许可义务。

真实联调才能确认：采用版本的 reverse WS 握手/echo/重连形状、heartbeat.good 语义、账号上下线、引用预览、负数消息 ID、图片/markdown 段差异、平台限流与发送回执。F 应先完成假 OneBot 的断连/重放/超时/跨账号负例，再用明确授权的测试目标验收；不能把对方仓库的实测描述算作 Moonfox 已通过。
