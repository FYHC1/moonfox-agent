# 阶段 D 协议实施契约

版本源：`moonfox_app.server.schema` 的 Pydantic DTO。`scripts/export_protocol.py` 导出 JSON Schema 和事件夹具到 `packages/web-ui/src/generated/`，前端用 AJV 校验，不复制 Python 内部对象。HTTP 路径前缀 `/api/v1`，纯 JSON，UTF-8。

## 鉴权与启动

REST 用 `Authorization: Bearer <token>`。token 只注入宿主（示例环境变量 `MOONFOX_ADMIN_TOKEN`），不返回给客户端、不写日志/URL/产品配置。WebUI token 仅保存在内存，刷新需重连认证。服务端校验非空 Origin（必须为显式 allowlist），WS 必须有合法 Origin。WS 接受连接后 5 秒内第一帧为 Subscribe DTO（含 token），认证/会话授权成功前绝不发送会话数据。CLI 默认 loopback，空目录可先配置提供商；离线 demo 须显式启用。

## HTTP

- `GET /health` → `{status:"ok", protocol_version:1}`，无敏感信息。
- `GET /api/v1/me` → `{id, is_admin}`，认证。
- `GET /api/v1/sessions` → `{sessions: SessionSummary[]}`，只返回可访问会话。
- `POST /api/v1/sessions`，CreateSession → SessionSummary（201）；浏览器只创建 agent 域，不接受 domain/owner/scope 参数。
- `GET /api/v1/sessions/{id}` → Snapshot，快照含 epoch/seq 水位。
- `POST /api/v1/sessions/{id}/inputs`，SubmitInput → Receipt（202），仅已接收不是已完成；同一会话 idempotency_key + 同文本重复返回原回执，冲突 409。忙时持久 FIFO 排队，不随浏览器退出取消。
- `POST /api/v1/sessions/{id}/control`，ControlCommand → Receipt（202）。cancel 优先于队列并取消该会话待处理输入；continue 必须 input_id 指向可继续的逻辑输入，共用 max_requests/tokens 总预算；compact 只在 idle 且已注入策略时允许。不自动 retry。
- `PUT /api/v1/sessions/{id}/config`，UpdateConfig → `{revision, settings}`，CAS 修订；只影响下次 run，运行中的 options 不变。model/tool/extension 必须在宿主登记且授权；不载入用户提交的 Python 路径。
- `GET /api/v1/config` → ConfigView，管理员。
- `PUT /api/v1/config`，UpdateHostConfig → ConfigView，管理员，CAS；新 defaults 用于新会话，现有会话保留已保存配置；全局队列/并发参数明确启动期生效。
- `POST /api/v1/sessions/{id}/approvals/{approval_id}`，ApprovalCommand → ApprovalView。审批只在对应会话/工具调用生效一次；拒绝、过期、取消都 fail-closed。
- `GET /api/v1/protocol` → `{protocol_version:1, schemas:{...}}`，认证。
- 管理员提供商/模型 CRUD、检测/发现、设默认接口及 DTO：见 [PROVIDER-MANAGEMENT.md](PROVIDER-MANAGEMENT.md)。所有写入 CAS，密钥只写、不在模型/会话/配置响应中出现；有依赖的删除拒绝，配置编辑只影响下一次 run。

`SessionSettings.generation` 为可选新增字段（旧持久配置按默认空对象加载）；temperature/top_p/max_tokens/penalties/seed/stop 的 null 表示继承模型默认，stop=[] 表示清空。JSON Schema 与前端由同一个定义导出。

异常：认证 401、会话授权 403、不存在 404、冲突/忙/修订不符 409、预算拒绝 429、输入/schema 422。错误不包含堆栈/provider 密钥。可选静态托管根 `/`；没有前端产物时 API 仍可工作。

## WS `/api/v1/sessions/{id}/events`

前端先取得 Snapshot，再传 Subscribe 的相同 epoch 和 after_seq。服务端原子注册并补发水位后的缓冲事件；epoch 变化、过旧/未来 cursor、慢消费者满缓冲均发送 `resync_required` 并结束订阅。前端重新获取快照再订阅，不拼接残缺流。

事件统一 WireEvent。seq 每会话/epoch 单调，前端忽略重复，缺号触发 resync。heartbeat 不推进 seq。断开 WS 不触发 Agent cancel。

payload：
- message_start/message_end：`{message:MessageView}`。id 生命周期稳定，message_end 替换同 id，不重复追加。
- text_delta：`{message_id,delta}`，只公开文本增量，无 partial 累积快照、reasoning、原始 provider 错误。
- input_received：`{input:InputView}`。
- run_started/run_finished：`{input:InputView, status}`；status 包括 running/completed/aborted/error/limit_reached/unknown。unknown 恢复不自动重放副作用。
- approval_requested/approval_resolved：`{approval:ApprovalView}`。
- config_changed/session_changed：`{}`，前端取新快照（与服务端事实一致）。
- resync_required：`{reason}`；heartbeat：`{}`。

服务端序列化屏蔽 AgentEvent 的 partial。前端不获取内部存储路径、scope id、工具审计 details 或 provider 凭据。

## 显示职责

独立 `packages/web-ui`，Vite + 原生 JS 模块，Node test runner + AJV schema 校验；无需为后端安装前端。中文界面：登录/连接、会话列表/新建、消息与流式反馈、运行/队列/预算、停止/显式继续、配置修订冲突、审批、扩展状态、管理员多提供商/模型与参数管理；无会话/载入/错误/离线/重连状态。无任意 HTML 渲染，不在浏览器持久保存 provider 密钥或业务 run/队列。
