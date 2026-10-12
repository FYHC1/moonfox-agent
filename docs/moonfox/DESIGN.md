# Moonfox 接口设计：核心与扩展契约

> 当前有效设计，替代 [历史草案](history/DESIGN-v1.md)。整体边界与目录以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准。
> 下列签名覆盖完整实施路线；阶段 A–D 的核心、运行时/扩展、应用服务/配置授权与 HTTP/WS 子集以公开 API 和测试为准。具体业务扩展仍是后续拟定契约。阶段 D 实际能力与限制见 [STAGE-D.md](STAGE-D.md)。

## 1. 包归属、公开 API 与可移植性

**核心只有 ai 与 agent 两包。** 会话运行时和扩展宿主都在 moonfox-app 业务包内，web-ui 是独立的前端工程。本文中的 runtime 指业务包内部模块，不再表示独立分发包；“宿主”指业务包的配置、授权、调度和装配职责。

### 1.1 对外公开面

| 公开面 | 使用者 | 保证 |
|---|---|---|
| moonfox_ai.api | Agent 核心、业务包或其他独立程序 | 统一模型消息/流，不依赖业务应用 |
| moonfox_agent.api | 业务包或其他 Agent 应用 | loop、工具与队列机制，不加载插件或产品配置 |
| moonfox_app.api | headless 使用者、服务端装配入口 | 业务服务的显式工厂和门面；使用持久会话不要求启动 Web 服务 |
| moonfox_app.extension_api | 具体业务扩展 | 版本化 DTO、事件、能力协议和注册接口；不泄漏内部对象 |
| moonfox-app/server 的 HTTP/WS 协议 | web-ui 或未来其他显示客户端 | 版本化命令、快照、事件；不是 Python 内部对象的直传 |

- 两个核心包与业务包各自声明 Python 依赖并构建 wheel；前端使用独立的 package.json/构建入口。首次发行前验证分发名可用性。
- 核心两个 wheel 在干净环境独立验证。业务包另做 headless 安装/运行检查，server 依赖作为可选 extra；前端构建不是 Python 核心安装步骤。
- 扩展对 moonfox-app 的依赖通过其轻量 extension_api 命名空间表达，manifest 声明 SDK 版本范围。业务包不强制依赖所有具体扩展，只在显式启用时加载。
- extension_api 的导入不能传递导入 bootstrap/server 或触发插件发现；应用包顶层初始化不得造成上述副作用。
- 核心不导入应用配置、扩展实现、Web 框架、OneBot、记忆数据库或固定工作目录。
- 模型调用、工具实现、时钟等通过参数或协议注入核心；业务包内部 runtime 的存储和上下文策略同样可替换。
- 不以修改 sys.path、运行时 monkey patch 或直接写私有字段作为扩展方式。
- 模型/工具运行失败归一化为结果/事件；API 误用、错误类型或编程 bug 不得被吞掉当作业务重试。

### 1.2 业务包内部的依赖与状态归属

- extension_api 定义轻量契约；runtime 与扩展管理器只经契约协作，避免彼此导入具体实现。
- runtime 拥有单会话 transcript、投影、恢复状态；services/SessionHub 拥有会话目录、入口路由和执行调度。
- services/security 在授予扩展能力前完成身份与作用域检查；权限检查器作为回调注入 runtime/core，不靠可卸载插件保证安全。
- runtime 不 import config/security/jobs/server 的具体实现，通过 SessionFactory 注入所需策略与服务端口。
- server 是传输适配器，只调用注入的业务服务门面，不能直接写 AgentState、绕过 inbox 或直接读取记忆数据库。
- web-ui 拥有输入草稿与显示状态，不拥有持久会话、重试/压缩规则、工具授权、人设选择或记忆隔离的最终决定。
- bootstrap 是唯一装配根；只在显式启动时创建服务、工作器、扩展实例。关闭浏览器仅移除订阅，不等于关闭会话或应用。

未来若抽离 runtime，先用第二个真实宿主验证边界与兼容性，再移动包；当前不额外建立协议包、通用框架包或多 Agent orchestrator。

## 2. 核心 run 契约

保留 mini-pi 的工具循环、steering 与 follow-up 两种投递语义；应用持续接收消息不要求核心持续发请求。

~~~python
@dataclass(frozen=True, slots=True)
class RunOptions:
    max_turns_per_run: int   # 必填，>= 1；宿主解析配置后注入

@dataclass(frozen=True, slots=True)
class RunOutcome:
    reason: Literal["completed", "aborted", "error", "limit_reached"]
    turns_used: int
~~~

一次 turn 为一次 assistant 响应及该响应的工具批次。发起模型调用前检查轮数，已发出的工具调用必须产生对应结果或明确的未知/取消状态，不能留下非法配对。

达到上限返回 limit_reached。core 不发送 QQ 提示、不压缩上下文、不重新加载配置、不自行清零预算重新开始。业务包 runtime 输出会话事件，services 决定后续人工继续或有界恢复。限制续跑、重试、压缩和扩展请求均计入同一个逻辑请求的总成本/时间额度，不能靠重入规避限额。

配置优先级：全局 → 使用场景 → 单会话 → 经授权的单次覆盖。合法配置形成不可变快照，从下一个 run 生效。具体数值放配置文件，示例不是代码常量。

取消使用单向令牌：设置后不可 reset；宿主 wait_for_idle/settled 后才能创建新 run。若工具不支持真正中断，宿主记录其仍在执行或结果未知，不能宣称副作用已经撤销。进程关闭属于宿主生命周期，不属于 Agent 的 QQ 特性。

## 3. 扩展 SDK

SDK 定义在 moonfox_app.extension_api；registry/dispatch/manager 实现在业务包内部的 extensions 模块。仓库根目录的 extensions/persona、memory、qq 则是分别可启用的扩展实现。两者不能混为一个层。

### 3.1 Manifest 与两类 API

~~~python
@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    id: str
    version: str
    sdk_version_range: str
    scope: Literal["session", "host"]
    capabilities: tuple[str, ...]
    requires: tuple[str, ...] = ()

class SessionExtension(Protocol):
    async def setup(self, api: SessionExtensionAPI) -> None: ...
    async def attach(self, ctx: SessionExtensionContext) -> None: ...
    async def detach(self) -> None: ...
    async def dispose(self) -> None: ...

class HostExtension(Protocol):
    async def setup(self, api: HostExtensionAPI) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def dispose(self) -> None: ...
~~~

setup 仅声明注册，不启动无限任务。attach/start 才获取该作用域的资源。注册返回 RegistrationHandle，由管理器兜底释放。后台任务必须经 TaskService 提交，可列举、取消、超时、等待；不得留下无人管理的 create_task。

HostExtensionAPI 与 SessionExtensionAPI 在轻量 SDK 中声明，不 import 业务服务实现。SessionFactory/扩展管理器按绑定的作用域提供 capability；权限不够时拒绝启用或明确禁用该能力。首版支持显式 entry-point/注册工厂加载，不自动执行未知目录代码，不做插件市场。会话扩展与宿主扩展生命周期不同，不能每开一个会话就启动一个 QQ 服务。

### 3.2 会话扩展能力

| API | 契约 |
|---|---|
| on(event, handler) | 快照派发，稳定顺序，返回注销句柄 |
| register_tool(definition) | namespaced 工具；参数 schema 校验；经过宿主强制权限与嵌套调用检查 |
| register_prompt_section(provider) | 返回系统 section 草稿；仅授权提供者可用，写入由 runtime 校验并提交 |
| register_context_provider(provider) | 在请求投影中提供参考内容；不能覆盖宿主权限或工具声明 |
| register_command(definition) | 显式命令，入口鉴权由宿主完成 |
| stage_entries(drafts) | 只能提交授权范围的 namespaced custom entry，不能原地改存储 |
| enqueue(message, delivery) | 仅当前会话的 steer/follow_up；不绕过预算、不能冒充认证身份 |

请求上下文只提供当前会话的不可变视图、绑定 scope handle、限定存储、已计量的模型服务与日志；不提供全局数据库连接、可变 AgentState 或无限权限的宿主引用。

### 3.3 宿主扩展能力

QQ 需要网络服务和多会话接入，但不需要 Agent 私有状态：

~~~python
class ChannelGateway(Protocol):
    async def submit(self, message: ChannelInput) -> InputReceipt: ...
    async def control(self, command: ChannelControl) -> None: ...
    def subscribe_replies(self, listener: ReplyListener) -> RegistrationHandle: ...
~~~

ChannelInput 包括已认证的通道连接句柄、bot account、会话目标、speaker、外部 message ID、接收时间、结构化内容。宿主通过句柄验证来源，生成 SessionBinding；不能信任消息正文自报 owner/domain。

宿主负责 channel identity → opaque session ID 的映射、durable inbox、单会话执行者、权限和预算；扩展只做平台协议与策略。返回接收回执不等于完成推理。ReplyEnvelope 含稳定 run/message ID、通道目标和最终公开内容，不能包含 reasoning 或原始工具 payload。

## 4. 派发、上下文和事务

### 4.1 派发规则

沿用 mini-pi 的语义分类，而不是“一切回调都吞异常”：

| 类别 | 失败与结果 |
|---|---|
| 通知 | 有超时，错误诊断隔离；返回值不改变状态 |
| 链式转换 | 每步校验结果；失败回退该步之前的有效快照 |
| 就地参数修改 | 在副本上执行并验证后替换，避免半次修改泄漏 |
| 权限/取消 | fail-closed；拒绝不能被后续 handler 覆盖 |
| 边界草稿 | 预览、验证、批提交；非法草稿不写入，不续跑 |

应用服务的强制授权在通用工具执行前生效，不是扩展可任意取消注册的普通 handler。停用权限之外的任意扩展，不能关闭身份/域检查或变成默认放行。核心仅调用注入的通用权限钩子；业务包决定策略。结果转换也不能隐去审计中的失败状态。

### 4.2 首轮与每次模型请求

~~~text
装载规范会话投影
  → 应用已批准的人设/工具 section 变更（版本化落盘）
  → 构建请求快照，调用 context providers（首轮也执行）
  → 校验 role、工具配对、prompt、token 预算
  → 唯一 convert_to_llm 转换边界
  → provider 正规化/序列化 → 模型调用
~~~

稳定人设使用系统 section。记忆、检索文档、群聊旁听均是有来源的参考数据，不自动成为高优先级指令。运行时允许泛型 context provider 产生可重建的上下文块，应用通过唯一 codec 边界转成 provider 可接受的消息。

每次从规范投影重建临时上下文，不把“上次增强后的 context”再次增强。一次用户输入的 recall 可在该 run 内缓存，键至少包含 binding、输入 ID、投影版本、检索版本；不在全局共享无域缓存。

需要可回放时，保存扩展版本、选中记录 ID/版本、实际注入文本或内容寻址快照；只存记忆 ID 不能保证记录编辑后的精确回放。快照也必须参与遗忘删除。

人设卸载/更换时提交 section 删除/替换；记忆扩展不可用时不沿用上一请求的 recall。必需的人设加载失败可拒绝该聊天会话启动，普通检索超时可按配置空结果降级；授权失败绝不降级放行。

### 4.3 会话存储

SessionStore 位于 moonfox-app 的 runtime/storage，是可替换的接口，默认 JSONL，实现单写者。会话 entry 是业务包的运行时类型，非 agent 核心存储；仅有通用消息/模型变更/压缩/custom 等，不为具体 QQ 或 memory 扩展增加专用枚举。

批草稿采用事务 ID + commit 标记或等价原子方案，恢复时忽略未提交批次。损坏尾行可截断到安全边界；中部损坏/孤儿 parent 应报错隔离，不擅自改父子关系。恢复缺失工具结果时标记“结果未知”，有副作用工具不自动重放。

未知扩展 custom entry 保留原始 payload；不认识的关键上下文类型不静默过滤。投影压缩保留系统/工具重放基线及完整工具配对；摘要允许纠正过期事实，不采用“只能追加不能删除”的无限增长规则。

## 5. 业务扩展契约

### 5.1 人设扩展

- 保存人设版本、身份/语气/示例，默认人设内回答；不更改核心 Assistant 类型。
- 通过 prompt section provider 提供 namespaced section，变更下次安全请求边界生效。
- 人设可共用静态模板，但关系状态/用户画像不跨记忆域。
- 人设表达不能伪造工具成功、自动提升权限或掩盖重要操作失败。

### 5.2 记忆扩展

记忆扩展拥有 SQLite、迁移、FTS/可选向量、抽取工作器和记忆工具；业务包负责作用域授权并在工厂中配置扩展。runtime 不创建 memory 表，也不直接 import 或调用具体扩展的 retrieve。

宿主提供不可变 SessionBinding 并授权绑定作用域的存储 handle：

~~~python
@dataclass(frozen=True, slots=True)
class MemoryScope:
    domain: Literal["qq", "agent"]
    scope_id: str

class ScopedMemoryStore(Protocol):
    async def search(self, query: str, limit: int) -> list[MemoryRecord]: ...
    async def remember(self, candidate: MemoryCandidate) -> str: ...
    async def forget(self, memory_id: str) -> None: ...
~~~

MemoryScope 属于记忆扩展；域授权由应用服务维护，runtime 只认识注入的 opaque scope handle。模型和前端不能传 domain 覆盖 scope。SQL 的 search/update/delete、工具直接读 ID、候选合并、索引与缓存均检查绑定域和会话范围。数据库用 CHECK/复合约束辅助，不能把 NOT NULL 当作完整隔离证明。

agent_settled 只创建幂等抽取作业，不在事件处理器等待第二次 LLM 调用。作业包含域、会话、entry 范围、版本和去重键，通过宿主计量服务执行。候选事实带来源、置信度和撤销路径，不把群友声称当作操作者事实，也不让模型自由改写权限。

检索有硬 token 上限；偏好不能无限置顶绕过预算。FTS trigram 是字面匹配，不等于语义召回；用中文关键词/ngram 查询构造及实测用例评估，必要时加 embedding，不复用旧草案中未经结果支持的命中断言。

“忘记”区分不再召回与彻底删除：后者需处理记录、画像、索引、快照、抽取待办、可恢复副本和备份保留策略。首版不开放自动 QQ↔Agent 记忆复制。

### 5.3 QQ 宿主扩展

选定 SnowLuma + OneBot 11。扩展拥有连接/鉴权、event/self_id 校验、echo 调用关联、协议消息编解码、触发条件、连发合并和出站格式。

- 默认一个宿主级实例管理配置中的连接；QQ 号写入会话路由键，防串号。
- 只支持经过验收的协议模式，首版 reverse WS Universal；forward/HTTP 是后续能力，不在配置中假装已实现。
- 去重键至少含 bot account、会话类型/目标、message_id；去重登记与 inbox 接收原子化。TTL 要配合历史恢复窗口。
- action allowlist 之外还检查账号、目标群/用户、资源所有权和参数；delete_msg 不能靠名称白名单保证“只撤回自己的消息”。
- 未知扩展 action 通过 capability 检查后显式开启；不能假设所有 OneBot 实现行为完全相同。
- api 返回失败按 retcode/status 判定；超时记录 unknown。HTTP/WS 请求重试与消息重发不是同一件事。
- reply 段使用规范的消息 ID 字段，渲染/缓存细节在 SnowLuma 实机验收，不保证所有实现相同。
- 最终回复经宿主 ReplyEnvelope 进入 outbox；已持久化的 outbox 由宿主 worker 按授权 transport 派发，不让 agent_settled 监听器等待网络发送。
- 卸载 QQ 扩展停止接收新输入、关闭连接并暂停待发 outbox；不影响 WebUI 和独立 Agent 会话。

### 5.4 WebUI 与应用服务

前端为 packages/web-ui；服务端为 moonfox-app 内部的 server。它们通过 HTTP/WS 协议交互，不共享 Python 私有对象。业务包内的 api.py 提供 headless 入口，server 只是使用这些服务的另一种入口。

| 操作 | 显示层负责 | 业务包负责 |
|---|---|---|
| 聊天 | 输入草稿、发送操作、消息/工具卡片渲染 | 认证、会话选择、入队、执行、持久化 |
| 停止/重试/压缩 | 发送明确命令、展示状态 | 判定命令合法性、取消收敛、恢复与预算策略 |
| 配置/扩展管理 | 表单、校验提示、状态展示 | schema 校验、修订冲突、持久化、加载/卸载、权限授予 |
| 工具审批 | 显示审批项、提交用户决定 | 审批人与作用域校验、超时、单次生效、审计 |
| 记忆管理 | 用户选择域、显示经授权的数据 | 绑定真实域、访问校验、调用限定扩展能力、删除传播 |

前端可做即时输入校验，但后端必须重新校验；浏览器不得判断“工具已经获准”后直接执行动作。WebUI 管理入口的数据不能自动并入 agent 域模型上下文。

### 5.5 前后端协议基线

首版采用 HTTP 命令/查询 + WebSocket 事件订阅。实时性依靠事件协议和状态修订，不要求所有命令与事件共用一条连接；不在首版维护 HTTP 与 WS 两套相同的命令入口。

- 命令含 request_id 和 target session_id；需要幂等时附 idempotency_key。校验/鉴权后返回接收回执与 input_id/run_id（尚未分配 run 时回执只含 input_id），不把“已接收”当作“已完成”。
- 会话执行结果和状态变化走事件；命令回执、事件中携带可关联的标识，客户端不假定 HTTP 回包一定先于第一个事件。
- 事件信封至少带 protocol_version、epoch、session_id、seq、type、payload。seq 在该会话/epoch 内单调，不能拿不同会话 seq 排全局顺序。
- HTTP 会话快照返回对应水位；订阅从该水位续传，服务端以原子订阅/缓冲回放避免“取快照与订阅之间漏事件”。进程重启或回放窗口不足时明确要求 resync。
- message_id 在生命周期开始前分配；text_delta 与最终结果关联同一 ID，前端按序归约、不重复追加；被取消或重试的中间输出与最终结果要有明确状态。
- 慢客户端各自使用有界缓冲；溢出后断开或发 resync 控制信息，不反压所有 Agent，也不把缺字的显示伪装成完整结果。
- server 对快照、订阅、历史查询和每条控制命令都检查身份与会话访问权。WebUI 登录凭据与 SnowLuma 接入 token 分离；WebSocket 校验 Origin 等连接约束，避免把密钥写进 URL/日志。

协议 DTO 的 schema、wire 编码和版本源由 server 维护。前端类型和测试夹具由该协议导出或经过相同 schema 校验，禁止两个工程各自维护含不同语义的协议定义。首版无需新增独立 protocol 发布包；只有多语言客户端形成真实复用需要后再拆。

验收包含：后端 headless 用例、前端纯事件夹具测试、HTTP/WS 契约测试、断线恢复测试；关闭浏览器只解除订阅，显式 cancel 命令才改变运行状态。

### 5.6 扩展配置与 UI 资源

扩展声明版本化配置 schema、可写字段和作用域，应用服务统一校验与保存。reload 使用快照/修订号，冲突时拒绝覆盖；密钥只接受引用或专门的写入接口，读接口脱敏。

前端显示扩展状态（disabled/loading/ready/degraded/failed/stopping）、最近错误、权限、当前会话启用列表；使用服务端返回的授权命令和 schema 展示表单。首版不提供任意扩展前端脚本注入，也不允许运行时上传任意 Python 代码。

packages/web-ui 独立构建为静态产物，可随应用发行物托管；业务包 headless 安装不要求有该产物。未提供静态产物时 server 可只提供 API。开发前端服务器不承担 SessionHub 或权限服务，浏览器只通过配置的后端地址连接。

## 6. 预算、事件与后台服务

- 核心只接收通用运行预算；宿主消费 core outcome 生成 UI Notice。核心不能 emit QQ/宿主事件，也不能调用压缩策略。
- provider 并发与速率配额按 provider/credential 分组，聊天/压缩/记忆抽取共用调度；支持排队取消、超时和 Retry-After。
- 有界流不能无条件 put_nowait 到无限队列。单次 provider 流消费者与事件广播是不同抽象；广播为每订阅者独立缓冲并定义恢复策略。
- message ID 在 message_start 前生成且整个生命周期稳定，不使用不断变化的内容哈希；wire 用 epoch + session_id + seq 支持断线恢复。
- 部分文本丢失时 resync 到明确快照水位，再订阅后续 seq；不能依赖一个也被截断的 message_end 修复 UI。
- 背景作业、权限审批、QQ 发送都不能持有会话写锁等待无限网络 IO。
- Python 插件同进程只提供契约隔离，不提供恶意代码隔离；能力接口用于管理可信扩展，不声称它是沙箱。

## 7. 必须先完成的验证

1. ai 与 agent 两个核心 wheel 在干净环境单独安装；不装 moonfox-app、扩展、Web 框架或前端产物依然可用。另测 moonfox-app 的 headless 示例，不以 UI 启动作为持久会话测试前提。
2. AST/import-linter + 包依赖闭包验证单向依赖，包含相对 import、动态入口与可选依赖；业务包内部 runtime 不 import services/server/具体扩展，SDK 导入不启动宿主。
3. ExtensionRegistry 快照派发、顺序、短路拒绝、超时、异常隔离、注册冲突、版本不兼容。
4. 生命周期 attach/detach、重复 start/stop、启动部分失败回滚、后台任务退出、卸载时工具声明清理。
5. 首轮记忆/人设注入、工具续跑不重复注入、section 移除、压缩后 prompt/工具配对完整。
6. QQ 与 Agent 在相同 subject、相同文本、碰撞 ID 下的负例：检索、画像、缓存、history tool、更新/删除、异步作业均不能越域。
7. run 轮数来自配置；热改只影响下一 run；重试/边界续跑不能重置逻辑请求总预算。
8. 假 OneBot 验证 echo、断连、重放、账号/目标授权、unknown delivery，不先拿真实群做压力测试。
9. 扩展全关闭时基础 Agent 和业务包仍可用；移除 WebUI 不影响 QQ；关闭浏览器不取消运行；单个扩展失败不能污染另一个会话。
10. server/web-ui 使用同一版本化协议 schema 和契约夹具；校验命令鉴权、接收/完成分离、增量归约、快照水位、断线重连及审批服务端校验。

## 8. 状态说明

阶段 A–D 已实现：独立 ai/agent；moonfox-app 的持久会话/投影/压缩和扩展管理；SessionHub、SQLite inbox/outbox/预算、配置 CAS、身份/审批、可选 HTTP/WS，以及独立中文 WebUI。CLI 仅提供离线 demo；真实 provider 网络适配、人设/记忆/QQ 具体实现仍未完成。

阶段 C 的实际接缝：`SessionBinding(session_id, scope_id)` 由 headless 调用者注入，runtime 仅校验 opaque 绑定，完整业务授权后置；context provider 只提交带 source 的参考块，required custom context 使用显式 codec；boundary 当前允许 namespaced custom 草稿，同次预算内继续，不提供自动重试/重入。工具未配置宿主权限检查器则拒绝，使用校验后的实际参数；所有嵌套调用同链检查。摘要策略显式注入，默认不产生额外模型请求，阈值触发也有次数/超时界限。详细行为见 [应用包说明](../../packages/moonfox-app/README.md) 和 [HANDOFF.md](HANDOFF.md)，不以旧伪代码作为兼容性证明。

阶段 D 使用域/路由/作用域结构字段和 owner/admin 授权，持久输入与完成事件分离；显式继续复用 logical input 预算，不自动重放 unknown。HTTP/WS schema 单源导出给前端，稳定 message ID、epoch/seq、独立有界订阅和 resync 均有验收。完整实现说明见 [STAGE-D.md](STAGE-D.md)，详细传输契约见 [STAGE-D-PROTOCOL.md](STAGE-D-PROTOCOL.md)。
