# 阶段 D：应用服务、协议与 WebUI

阶段 C 已先提交并推送为 `012211b`，之后开始本阶段。依赖方向仍是 app → agent → ai；runtime/SDK 不依赖 services 或 Web 框架。阶段 D 不实现真实 provider、具体人设/记忆/QQ 协议扩展。

## 启动演示

后端使用显式离线 demo provider，**不是模型推理**。需 Python ≥3.12（当前实测 3.14.8）及 Node ≥22.12（仅前端）。

```powershell
# 在仓库根，已有开发环境可直接补装 server extra
.\.venv\Scripts\python.exe -m pip install --group dev -e .\packages\ai -e .\packages\agent -e ".\packages\moonfox-app[server]"
.\.venv\Scripts\python.exe scripts\export_protocol.py
npm.cmd --prefix packages/web-ui ci
npm.cmd --prefix packages/web-ui run build

# 在当前终端生成随机管理员令牌；请保存供连接表单使用，不要提交到 Git
$env:MOONFOX_ADMIN_TOKEN = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
# 后端显式指定私有数据目录，默认只监听 loopback
.\.venv\Scripts\python.exe -m moonfox_app.cli --demo --home .moonfox --static-dir packages/web-ui/dist
```

打开 `http://127.0.0.1:8765`，后端地址填同源，令牌填当前终端中的 `MOONFOX_ADMIN_TOKEN`。令牌仅存在页面内存，刷新后重新输入；不是 provider 密钥，也不能用于 QQ 连接。跨机器需 HTTPS/反向代理与严格 Origin allowlist，自行评估部署边界。

开发前端：`npm.cmd --prefix packages/web-ui run dev`；后端加 `--origin http://127.0.0.1:5173 --origin http://localhost:5173`，Vite 代理 `/api` 和 WS 到 8765。生产可以不提供 `--static-dir`，API-only 不依赖前端产物。前端构建不是 Python wheel 安装步骤。

## 服务与存储

`create_application(home=..., models=..., defaults=..., tools=..., registry=...)` 构造 SessionHub；`await hub.start()` 显式启动，`await hub.aclose()` 收尾。ASGI lifespan 可负责这两个步骤。只装普通 `moonfox-app` 仍能使用服务，无需 server extra。

- `host.sqlite` 保存会话目录、domain/scope、配置、durable inbox、逻辑输入预算、审批和 outbox；JSONL 保存 transcript。两个事实源不假装共享原子事务。
- 单宿主 OS lease 防止两个进程对同一目录恢复/执行。SQLite WAL/FULL；每次事务不跨 await，不持锁等待模型、审批或网络。
- 输入在返回 202/pending 回执前写盘。每会话一个执行者，跨会话并行；忙时 FIFO，不依赖浏览器在线。入站幂等键与 request ID 冲突拒绝覆盖。
- 未领取的 pending 输入可在重启后继续；已领取运行中输入标为 `unknown`，不自动重放有副作用操作。缺失/空/损坏历史文件隔离，不用空会话替换原历史。
- 会话 runtime/扩展实例跨输入保留；配置修订在下次 run 的安全边界生效，必要时重装扩展。退出应用取消运行并收敛所有受管资源，未领取队列保留。
- cancel 优先于聊天队列，取消当前运行和待处理输入。显式 continue 仅允许最新、可继续的 user/toolResult 上下文，沿用原 logical_id 的预算；不自动 retry，不对未知结果盲目重入。

## 配置、身份与授权

配置采用 Pydantic strict/frozen/extra-forbid；JSON seed 只初始化新宿主，后续以 SQLite CAS 修订为准。全局 defaults 用于新会话，现有会话保存自己的配置；并发/事件缓冲等宿主容量设置下次启动生效。`config/examples/host.json` 不含凭据。

- TokenAuth 由操作者显式注入 token→Principal，内存保存摘要；API 不暴露凭据值。可在可信 bootstrap 使用 `EnvironmentSecrets.resolve("env:NAME")`，无文件密钥读取 API。
- 每条会话命令、快照、订阅都检查 owner/admin；工具与扩展授权不可由可卸载插件代替，普通 owner 不能提升 allowlist、审批或启用能力。
- 浏览器只创建 agent 域。domain、scope 和通道路由由宿主生成，不接受前端/模型任意覆盖。QQ 会话显示不触发向 QQ 发信。
- 工具默认拒绝。通过白名单仍可要求单次审批；approval 绑定 session/input/tool，过期、取消、拒绝均 fail-closed。嵌套调用使用相同强制检查。

## 运行预算与模型注入

宿主必须显式登记 `ModelRegistration(model, stream_fn, label)`，产品配置只能选择登记 ID，不执行用户提供的 Python 路径。CLI 仅离线 demo；真实 provider 可通过 Python 装配注入，尚未提供厂商网络适配。

- 每个 logical input 持久记录请求次数、累计时间、token 预留；显式 continue 不清零。
- provider gate 共用宿主并发限制；等待者取消后不发请求、不扣请求次数。预算准入在调用 provider 之前。
- token 预留使用上下文估算 + 输出上限，失败/崩溃不退款；若实际 usage 超过预留，追加记账并阻止后续超限请求。估算不是精确 tokenizer，依赖 provider 遵守 max_tokens，不宣称严格计费金额上限。
- 压缩策略由 `compaction_factory(gated_stream, model)` 注入，需通过同一计量 stream；默认禁用。没有真实 provider 的金额/汇率策略、Retry-After 重试或分布式限流，留待有实际 provider 后实现。
- run 结束撤销临时 provider/权限端口，空闲扩展不能挪用上一次 logical input 的预算。

## 通道接缝（无 OneBot 实现）

可信装配调用 `hub.bind_channel(ChannelBinding(...))`，固定连接、bot、owner 和目标 allowlist。薄通道扩展只能提交该 capability 授权的消息和取消；绑定/去重覆盖 bot+target+external_message_id，domain/scope 为结构字段。

完成的公开文本进入 outbox 后再由受管发送任务派发，剥离 reasoning 和内部 payload。pending/sending/sent/unknown/failed 状态保留；网络异常/超时标 unknown，不自动重发，不声称 exactly-once。卸载/关闭 capability 停止接收和发送，其他会话不受影响。当前只用假 transport 验收；SnowLuma 和具体 QQ 扩展属于阶段 F。

## HTTP/WS 与前端

完整契约见 [STAGE-D-PROTOCOL.md](STAGE-D-PROTOCOL.md)，schema 单一来源 `server/schema.py`。导出 JSON Schema/夹具到前端，前端 AJV 校验。

- HTTP Bearer；WS 首帧鉴权，token 不进入 URL，Origin 显式 allowlist，严格消息大小/类型限制。
- snapshot 含 epoch/seq；订阅原子补发水位后的有界事件。断线/过旧水位/慢客户端溢出要求 resync，前端重取快照，不重放输入或拼接缺失文本。
- 稳定 message ID 关联 start/delta/end；完成消息替换已有卡片。wire 不发送 core `partial`、reasoning、内部 scope、审计或原始 provider 错误。
- 关闭页面/断开订阅不调用 cancel。只有显式控制命令改变运行状态。
- 中文独立 WebUI：认证、会话列表/新建、消息/工具卡片、流式状态、停止/显式继续、配置修订冲突、单次审批和扩展状态；无任意 HTML 执行、凭据持久化或浏览器业务队列。

## 检查

```powershell
.\.venv\Scripts\python.exe scripts\check.py --installation --web
npm.cmd --prefix packages/web-ui run test:browser
```

`--installation` 验证三个 sdist→wheel、五个干净安装环境（AI、Agent、headless、目录安装、server extra）和仓库外 `python -I` 示例。`--web` 运行独立前端测试/构建，需要事先 npm ci。完整数字和浏览器联调结果记录于 HANDOFF。
