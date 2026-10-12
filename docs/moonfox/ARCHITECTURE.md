# Moonfox 整体架构：独立核心 + 业务应用 + 显示层

> 当前已确认的架构基线：**两个独立核心包 + 一个业务应用包 + 一个显示包，具体能力通过扩展实现。** 阶段 A–E 已实现，QQ 属于 F；实际进展与验证以 [HANDOFF.md](HANDOFF.md) 为准。
> 继承 [mini-pi 设计](../mini-pi/DESIGN.md) 的分层与契约，但不把其中的示例代码视为已经验证的实现。
> 接口细节见 [DESIGN.md](DESIGN.md)。[历史草案](history/DESIGN-v1.md) 仅记录早期讨论，不再作为实施依据。

## 1. 已确认目标与 pi 的对应关系

对照用户提供的 pi 分层，学习其职责和依赖方向，不要求 Python 项目照搬 npm 工作区或全部包数量。

| pi 的职责层 | Moonfox 首版对应 | 边界 |
|---|---|---|
| ai：统一模型能力 | packages/ai | 可独立调用 provider，不依赖 Agent 或业务 |
| agent：Agent 原语 | packages/agent | 通用循环、工具执行、队列、取消、事件，只依赖 ai |
| coding-agent：具体应用 | packages/moonfox-app | 会话运行时、配置、权限、扩展宿主、调度与服务端 |
| tui：终端交互/渲染设施 | packages/web-ui | 对标显示职责；浏览器前端通过协议收发，不承载业务编排 |
| orchestrator：多 Agent 协作 | 首版不设对应包 | 多会话并发不等于多 Agent 编排，有真实协作需求后再设计 |

保留的用户决策：

- Agent 核心可以独立安装、运行、测试、修改和移植，不依赖 Moonfox 应用。
- 记忆、人设、QQ 接入是可启停、可替换的扩展，而不是核心内建业务。
- QQ 使用方案 A：通过 OneBot 11 直接连接 SnowLuma，不经 TypeScript SDK/MCP 转发主消息流。
- 人设默认使用人设内回答，属于人设扩展的表达策略，不改变权限边界。
- QQ 聊天记忆与 Agent 对话记忆严格隔离。
- max_turns_per_run 由外部配置解析并注入，核心循环不写固定数值。

**常驻的是应用宿主，不是无限运行的 Agent run。** 空闲时核心正常结束，后续消息启动新 run，忙碌时使用 steering/follow-up 投递。连发合并、持久化 inbox 和主动搭话属于应用/扩展。

## 2. 总体关系

代码依赖：

~~~text
extensions/persona、memory、qq
    └─依赖→ moonfox_app.extension_api（轻量公开契约）

moonfox-app（业务与装配） ──依赖→ agent ──依赖→ ai
       └─按需使用 ai 的公开模型 API

web-ui ──HTTP/WS 协议──→ moonfox-app/server
~~~

运行时装配：

~~~text
浏览器：web-ui ──命令/事件──→ moonfox-app/server（传输适配）
                                    ↓
SnowLuma ↔ QQ 宿主扩展 ↔ ChannelGateway / 应用服务
                                    ↓
                         SessionHub / SessionFactory
                          ├─授权、隔离绑定、预算、inbox/outbox
                          ├─加载已授权的会话扩展
                          └─runtime/AgentSession
                               └─Agent Core → AI Provider
~~~

runtime 在这里是 moonfox-app 内部的会话运行时模块，**不是第三个核心包或单独进程**。此前独立运行时与宿主的职责合并到一个业务包，仍保留内部接口与测试边界。

只有 bootstrap 装配具体实现和显式启用的扩展；业务包的公开接口、SDK 导入不能隐式启动进程、服务或插件。扩展依赖 SDK，而宿主通过配置/entry point 加载扩展，不形成应用包与扩展包的强制依赖环。

## 3. 包边界与依赖规则

### 3.1 四个主包

| 目录 | 分发名 / 命名空间（暂定） | 职责 | 不应依赖 |
|---|---|---|---|
| packages/ai | moonfox-ai / moonfox_ai | 模型消息、流、provider 适配、上下文原语 | agent、应用、扩展、Web 框架 |
| packages/agent | moonfox-agent-core / moonfox_agent | 通用循环、工具执行、双队列、钩子、取消 | 应用配置、会话数据库、扩展加载器、QQ/UI |
| packages/moonfox-app | moonfox-app / moonfox_app | 会话、投影、压缩、恢复、配置、授权、扩展管理、多会话服务、server | 前端源代码或浏览器运行状态 |
| packages/web-ui | 前端工程，非 Python wheel | 消息渲染、输入、配置表单、审批交互和状态展示 | Python 私有模块、数据库、provider 密钥 |

Python 三个主包分别拥有 pyproject、wheel 和测试；其中**核心仅指 ai 与 agent 两包**。首版共同锁定兼容版本即可，不要求单独运营发布流程。WebUI 有自己的前端依赖和构建入口，不因此新增后端微服务。

模型网络实现可为 ai 的可选 provider 依赖。FastAPI、OneBot 网络实现、记忆数据库不能进入两个核心包的依赖闭包。业务包的 server 可作为可选安装 extra，headless 使用不强制启动或导入 Web 框架。

具体扩展可采用独立 Python 分发目录，但无需一开始分别发布；它们通过 moonfox_app.extension_api 接入，依赖范围由 SDK 版本声明与测试控制。

### 3.2 业务包内部也有边界

| 模块 | 拥有的职责 | 允许使用的内部能力 |
|---|---|---|
| extension_api | 扩展 API、DTO、SessionEvent、能力协议的轻量定义 | 标准库、必要类型依赖、ai/agent 公开类型；不 import 具体服务 |
| runtime | 单会话 AgentSession、存储/投影、prompt、压缩/恢复机制 | 核心公开 API、extension_api 的协议/类型、构造时注入的服务 |
| extensions | 注册表、派发、生命周期、插件实例管理 | extension_api；行为通过已注入的能力执行，不直接拼装业务会话 |
| config、security、jobs | 配置/凭据、身份与授权、后台作业和预算 | 轻量契约、各自存储；不反向调用 server 或具体插件 |
| services | SessionHub、SessionFactory、路由、inbox/outbox、业务用例 | runtime、extensions、config/security/jobs 的公开内部接口 |
| server | HTTP/WS、鉴权适配、序列化、静态产物托管 | 注入的应用服务门面与协议 DTO；不直改 AgentState |
| bootstrap | 唯一装配根，拥有启动和关闭顺序 | 装配以上模块并加载配置中显式启用的扩展 |

会话是业务包的事实，不是浏览器或 QQ 插件私有状态。压缩、重试和授权决策由后端承担；UI 禁用按钮只是显示反馈，不是安全检查。

### 3.3 扩展机制与扩展实现分开

- moonfox-app 内部的 extensions 模块是**扩展基础设施**，负责注册、派发、生命周期；extension_api 是稳定的公开契约。
- 仓库根目录的 extensions/persona、memory、qq 是**具体业务扩展**。
- 扩展不 import moonfox_app.runtime、services、server 的内部实现，不持有可变 AgentSession 或全局数据库。
- 核心只暴露通用钩子和工具协议，不扫描扩展目录，也不认识人设/记忆/通道扩展名。
- UI 与 QQ 是并列入口，都调用业务服务；QQ 不经 WebUI 转发，浏览器断开不停止后端会话。

### 3.4 独立性与未来拆分

1. 只安装 ai + agent，就能在仓库外运行 faux provider 的“工具调用 → 下一轮响应”，无需应用和扩展。
2. 安装 moonfox-app，可用 headless 示例持久化并恢复会话，不安装 WebUI 构建产物、不启用 QQ/人设/记忆也能工作。
3. wheel 验证不依赖 workspace 路径注入；核心不隐式读取产品配置、固定目录或全局运行实例。
4. 只有第二个独立应用确实需要同一会话运行时、其依赖边界和 API 已经稳定时，才考虑抽取 runtime。当前内部模块边界为以后抽包留空间，但不提前维护独立发行包。
5. 只有出现任务分解、子 Agent 生命周期、结果汇总、协作失败传播等需求，才考虑 orchestrator；SessionHub 的多会话调度不需要它。

## 4. 规划目录

以下为目标目录；A/B 已创建独立 ai/agent，C 已创建 runtime/extension_api/extensions，D 已增加 services/config/security、可选 server、CLI 和独立 web-ui，管理补充增加共享 ModelCatalog、凭据保护与 Chat Completions/Responses/Messages 流式适配。AI 只使用注入 HTTPTransport，httpx 属应用可选 `providers` extra；WebUI/headless/QQ 使用同一目录。具体业务扩展/jobs 的完整持久作业系统按后续阶段推进。实际范围及限制见 [STAGE-D.md](STAGE-D.md) 与 [PROVIDER-MANAGEMENT.md](PROVIDER-MANAGEMENT.md)。

~~~text
moonfox-agent/
├── pyproject.toml                    # Python 开发工作区、统一测试/检查入口
├── docs/
│   ├── mini-pi/                     # 原始参考，不改动
│   └── moonfox/                     # 当前架构、接口设计、历史归档
├── packages/
│   ├── ai/
│   │   ├── pyproject.toml
│   │   ├── src/moonfox_ai/
│   │   │   ├── api.py               # 对外导出面
│   │   │   ├── types.py
│   │   │   ├── stream.py
│   │   │   ├── transcript.py
│   │   │   ├── estimate.py
│   │   │   ├── overflow.py
│   │   │   └── providers/           # base / faux / HTTP 契约 / OpenAI / Anthropic
│   │   └── tests/
│   ├── agent/
│   │   ├── pyproject.toml
│   │   ├── src/moonfox_agent/
│   │   │   ├── api.py
│   │   │   ├── types.py
│   │   │   ├── agent.py
│   │   │   ├── loop.py
│   │   │   ├── tools.py             # 执行机制，不含业务工具实现
│   │   │   ├── hooks.py
│   │   │   ├── queues.py            # steering / follow-up
│   │   │   └── abort.py
│   │   └── tests/
│   ├── moonfox-app/
│   │   ├── pyproject.toml           # server 依赖为可选 extra
│   │   ├── src/moonfox_app/
│   │   │   ├── api.py               # headless 应用服务公开入口
│   │   │   ├── bootstrap.py         # 装配入口，拥有进程生命周期
│   │   │   ├── runtime/
│   │   │   │   ├── session/         # entries / projection / manager / orchestration
│   │   │   │   ├── storage/         # SessionStore；JSONL / in-memory 实现
│   │   │   │   ├── compaction/      # 可注入的摘要策略
│   │   │   │   └── prompt/          # section 重放、diff、上下文管线
│   │   │   ├── services/            # hub / actor / routing / session_factory / outbox
│   │   │   ├── extensions/          # registry / dispatch / lifecycle manager
│   │   │   ├── extension_api/       # api / types / events / capabilities
│   │   │   ├── config/              # schema / loader / secrets / reload
│   │   │   ├── security/            # 身份、作用域、工具授权、审批
│   │   │   ├── jobs/                # 后台任务、并发、预算、作业恢复
│   │   │   └── server/              # HTTP/WS、协议 schema、wire；不放业务规则
│   │   └── tests/                   # runtime / extensions / services / server 契约
│   └── web-ui/
│       ├── package.json            # 前端依赖、构建与测试；不是后端包
│       ├── src/
│       │   ├── client/             # 类型化命令/事件客户端
│       │   ├── state/              # 事件归约与纯显示状态
│       │   ├── components/         # 消息、工具卡片、审批等交互组件
│       │   └── pages/              # 对话、配置、扩展管理
│       └── tests/                  # 快照/事件夹具、交互、重连恢复
├── extensions/
│   ├── persona/
│   │   ├── pyproject.toml
│   │   ├── src/moonfox_ext_persona/ # plugin / schema / store / render
│   │   └── tests/
│   ├── memory/
│   │   ├── pyproject.toml
│   │   ├── src/moonfox_ext_memory/  # plugin / scopes / store / retrieval / worker / tools
│   │   └── tests/
│   └── qq/
│       ├── pyproject.toml
│       ├── src/moonfox_ext_qq/      # plugin / onebot / codec / trigger / sender
│       └── tests/
├── examples/
│   ├── core_only.py                # 只安装 ai + agent 即可运行
│   ├── headless_app.py             # 使用业务包，不启动 HTTP/WS 或 QQ
│   └── minimal_extension/
├── config/examples/                # 无密钥配置、人设示例
└── tests/architecture/              # 依赖图、公开接口、独立安装、UI 协议检查
~~~

仓库目录、分发包和进程不是同一概念：业务包内的 runtime 不再单独分发；生产前端构建成静态文件，可由 server 托管，不要求额外的前端常驻服务。前端工程的构建工具不成为核心两个 Python 包的运行依赖。

**不设一个所有包共同依赖的 Moonfox config/core 杂物包。** 小型通用类型留在拥有它的层；产品配置只由业务包解析、转换为参数注入核心。根 Python 工作区与前端 package.json 各管各的，无需只为模仿 pi 就把所有包改为 npm workspace。

## 5. 核心保留什么、移出什么

本节的 core 指 packages/agent；runtime 指业务包内部的运行时模块。宿主是 moonfox-app 的职责角色，不是另一个分发包。

| 能力 | 位置与原则 |
|---|---|
| Message / AgentMessage 分离 | ai/core；业务自定义消息通过 codec 与 convert_to_llm 转换 |
| system prompt 重放 | ai 原语 + runtime 编排；修改形成版本化系统消息，不原地漂移 |
| 双层工具循环 | core；沿用 mini-pi tools + steering / follow-up 的语义 |
| user/autonomous 四队列 | 不纳入 core；用户 inbox、批量合并与任务调度在宿主/扩展 |
| 常驻在线 | 宿主服务常驻；空闲 core 不请求模型、不等待 QQ 网络事件 |
| 取消 | core 每次执行使用单向 AbortToken；不 reset 已取消 token。打断并重启由宿主等待 settle 后以新 token 启动 |
| max_turns_per_run | core 接受运行参数；配置解析、通道覆盖与 UI 告警在宿主；到限停止，不无限自动重入 |
| 压缩、重试、会话树 | runtime；core 不调用 compact，不 import SessionEvent |
| QQ 身份、触发、收发、限流 | QQ 扩展 + 宿主授予的通道能力 |
| 记忆、画像、人设 | 扩展；无这些扩展仍可对话和执行工具 |
| 费用上限与 provider 并发 | 宿主服务，所有调用（含压缩/抽取）经过同一计量网关 |
| 文件/shell/MCP 等工具实现 | 独立扩展或应用工具集；核心只提供工具执行协议 |

扩展可以组合使用，但不能要求核心添加具体业务分支。若未来确实要扩展核心，只新增具有独立用例的通用原语，并保持无扩展测试通过。

## 6. 扩展系统是基础设施，不再延期

### 6.1 两种作用域

**会话级扩展**：每个 SessionBinding 一个实例，例如人设、记忆的会话适配器。通过受限上下文、prompt provider、工具注册与会话事件工作。

**宿主级扩展**：每个宿主/配置实例一个，例如 QQ 通道。通过 ChannelGateway 等能力接口创建/选择会话、投递输入、订阅回复；不得持有所有 AgentSession 的可变对象。QQ 不应每创建一个会话就启动一个监听端口。

两种作用域共用扩展标识、版本、生命周期、能力授予和诊断规范，但 API 不同。SDK 位于 moonfox_app.extension_api，只声明类型和能力协议；业务包的 services 提供实现，extensions 模块管理插件实例，runtime 通过注入的 hooks 使用它们。runtime 不实现 OneBot，也不反向 import services 或插件。

### 6.2 通用接缝与产品用法

| 通用能力 | 人设扩展 | 记忆扩展 | QQ 扩展 |
|---|---|---|---|
| prompt section provider | 注册 namespaced 稳定 section | 不把不可信记忆升级成系统命令 | 无 |
| request context provider | 可选示例/场景上下文 | 注入预算内、带来源的参考数据 | 说话人由入站 DTO 传入 |
| register_tool | E 不提供切换工具 | search/get/remember/forget（强制绑定 scope） | 可选受限 QQ 工具，不暴露万能管理 action |
| SessionEvent | 版本切换诊断 | settled 后登记可选抽取作业 | 通过宿主回复投影订阅最终结果 |
| generic custom entry | 人设版本标识 | E 的召回快照保存在绑定扩展库，不重复写聊天 | 由宿主记录通道回执 |
| host channel capability | 不需要 | 不需要 | OneBot 接入、标准化输入、最终回复发送 |

persona_change、memory_ref、qq_delivery 使用命名空间 CustomEntry/扩展诊断，不再扩展核心 entry/event 枚举。扩展缺失时历史仍能打开，未知数据保留原样；未知的上下文关键消息必须显式报不兼容，不能静默丢失。

阶段 E 通过显式 `SessionExtensionDefinition` 声明工厂、JSON 配置 schema/默认值和管理命令。宿主保存不可变 `extension_config`，以 CAS 更新；每会话工厂获得 `ExtensionResources(binding, domain, data_dir, models)`。管理员管理命令不启动聊天或模型，停用扩展后仍可管理数据。`models` 能力复用共享模型网关与原 logical input 预算，而不是插件自建提供商客户端。

### 6.3 生命周期与安全

加载顺序：读取显式启用列表 → 检查 API 版本/配置/依赖 → 宿主授权能力 → setup 注册 → start；会话建立时 attach，关闭时 detach；卸载 stop 并 dispose 全部注册与后台任务。

- 普通通知失败隔离并记录；转换失败使用上一次有效结果；权限检查失败拒绝执行。
- 边界草稿先校验、预览，提交失败整体回滚；JSONL 实现需批提交标记，不靠逐行 append 假装事务。
- 扩展卸载移除工具声明与 context provider；不能保留上次请求的记忆或人设缓存。
- 普通扩展不能覆盖宿主强制权限。所有嵌套工具走同一权限检查；核心只接受被注入的检查器，不自行加载权限插件。
- 身份认证、域绑定、访问授权和预算准入是业务包的强制服务，不是可关闭的普通扩展。移除记忆/QQ 扩展不能把安全策略变成默认放行。
- 首版只加载操作者安装并启用的可信本地扩展。Python 同进程插件不是沙箱；恶意代码仍可越过 API 读文件。运行不可信扩展需另行进程/OS 隔离。

## 7. 记忆隔离：按业务域，不按浏览器窗口

域名采用 qq 与 agent。WebUI 是管理入口，不等于业务域：在 WebUI 查看 QQ 会话时，该会话仍是 qq 域，不能被自动转换为 agent 域。

moonfox-app 的 security/services 在认证与路由后生成不可变 `SessionBinding(session_id, scope_id)`；domain、受限数据目录与模型能力经 `ExtensionResources` 另行注入，不进入核心。记忆算法和存储由扩展拥有，作用域授权由业务包强制执行。模型工具和管理参数不能覆盖 domain/scope/session 来绕过授权。

隔离目标覆盖所有业务副本。阶段 E 已实现记录/索引、字面检索、工具 ID 读取、召回缓存与快照、抽取作业/恢复、遗忘/删除和管理授权；媒体、跨库导入导出及完整备份保留策略在后续阶段验收，不声称已完成。

E 使用 `home/extensions/memory/{domain}/{sha256(scope_id)}/memory.sqlite3` 分目录存储，同时在所有 SQL 中强制 domain + scope 过滤和复合约束；分目录不是唯一防线。检索、更新、删除、合并全部校验 scope，不只是 search。主体相同不等于可见性相同，QQ 私聊和群聊默认也不自动互通。首版是安全参数化关键词/ngram 索引，没有向量或语义召回。

操作者可通过受鉴权的管理端查看不同域，但管理结果不能自动成为 Agent 模型的工具结果或上下文。首版不提供自动跨域同步工具。

人设文件可作为静态配置被两域选用；这不意味着共享用户记忆或关系状态。角色表达采用已确认的人设内回答；真实操作结果、权限、外部副作用仍按事实反馈。

遗忘阻止未来自动召回和模型 `get`；管理员可以显式查看。删除/清空清理扩展副本及作业原文，并防止旧工作器返回后复活数据；不等于擦除追加式聊天、已产生的工具结果/模型复述、提供商副本、备份或正在执行的请求上下文。

## 8. 消息与扩展调用链

### QQ（阶段 F 目标；[qq-bridge 参考](QQ-BRIDGE-REFERENCE.md)）

~~~text
SnowLuma → QQ 宿主扩展
  → 鉴权 / self_id 绑定 / 去重 / 触发 / 连发合并
  → ChannelGateway.submit(normalized input)
  → 应用服务路由：qq:{bot_account}:{conversation...}
  → SessionHub 单写者 + 权限/预算 + 已绑定的会话扩展
  → 业务包 runtime 构造请求：人设 section + 同域记忆参考 + 会话投影
  → core loop → provider / 工具 → AgentEvent
  → runtime 完成恢复与边界 → SessionEvent / agent_settled
  → 应用服务提取最终可交付文本 → 持久化 outbox → QQ 扩展发送 → SnowLuma
  → 记忆扩展可选后台抽取（默认关闭，同域、有来源、幂等、原输入预算）
~~~

第一条请求也必须运行上下文构造，不能只挂在 prepare_next_turn（首轮可能不调用）。同一轮工具续跑沿用绑定和快照；发现新的显式输入才重建相关 recall。每次准备从规范投影开始，避免重复注入。

QQ 首版默认最终回复发送，不在每个 message_end 直接发：中途 assistant 可能是工具前言或失败重试的产物。只投递公开文本，不投递 reasoning/工具参数/内部错误。WebUI 可展示流式增量，但断线和慢消费者不应阻塞会话；溢出时 resync，不静默留下残缺状态。

### WebUI

WebUI 命令 → moonfox-app/server → 应用服务 → 同一个 runtime/core。server 仅做协议、鉴权适配与序列化；SessionHub、权限、扩展配置等业务决策在 services 内。独立 Agent 对话绑定 agent 域；查看 QQ 会话只订阅已授权视图，输入/人工发往 QQ 是明确区分的操作，不能因为打开页面自动发消息。

显示层边界：

- web-ui 只管理输入草稿、选中会话、卡片展开等显示状态，并归约服务端发来的会话视图。
- run 生命周期、压缩/重试、可执行工具、人设版本、记忆作用域和授权结果以后端为准。
- 前后端通过版本化命令/事件协议衔接；schema 和契约夹具归 server 管理，前端消费类型与夹具，不手写另一份业务状态机。
- 关闭或刷新浏览器只影响订阅；取消 run 需要显式控制命令。独立 Agent 与 QQ 在没有浏览器时照常运行。
- 扩展声明配置 schema 和管理命令，WebUI 可以据此显示表单；不在浏览器加载扩展 Python 代码或持有 provider 密钥。

### 空闲、插话与取消

宿主 inbox 持久化外部输入，快速返回接收回执；每会话一个执行者。忙时默认排队，显式纠正可投 steering，普通追加走 follow-up 或下一个 run。停止是控制命令，不排在聊天消息后等待。取消后等待本次执行收敛，未知副作用保留审计，不假装回滚。

## 9. 部署、存储与故障边界

SnowLuma 与 Moonfox 业务应用独立运行。反向 WS 是 QQ 扩展在 Moonfox 进程内监听、SnowLuma 主动连接；同机默认绑定 loopback。跨机器时必须提供可达地址与网络保护，反向 WS 不等于“不需要监听端口”。QQ 登录状态与 WS 连接状态分别显示。

web-ui 运行在浏览器；生产部署时构建产物可由 moonfox-app/server 静态托管。开发时可独立运行前端开发服务器，但这不是额外的业务宿主。Python 应用可不启用 server，也可只启用 API 而不携带前端产物；核心库不依赖这些运行方式。

~~~text
MOONFOX_HOME/
├── config/                         # 用户配置、WebUI override；密钥另存
├── sessions/{qq|agent}/             # 不透明 session ID，JSONL
├── host.sqlite                     # 路由、inbox/outbox、作业、计量等宿主事实
├── extensions/
│   ├── persona/                    # 人设与版本
│   └── memory/                     # 记忆 DB、索引；强制 domain/scope
├── media/{qq|agent}/
├── logs/
└── backups/
~~~

- JSONL 是会话 entry 的事实源；记忆库、inbox/outbox、配置是各自的事实源，不能宣称 SQLite 全部可重建。
- JSONL 需定义 flush/fsync 边界，备份需固定 checkpoint；仅“追加写”不能保证 kill 后零丢失。
- QQ 发送超时可能已经成功。outbox 记录 pending/sending/sent/unknown/failed；没有协议端幂等保障时不能承诺 exactly-once，unknown 不盲目重发。
- 并发、时间、请求数/token 预算由宿主统一网关约束，记忆抽取和压缩同样计入；当前不是金额/汇率硬预算。E 作业持久化原输入票据，恢复不借用后来输入额度；未知执行不自动重试，后台任务没有预算豁免。
- 上下文发给云模型时，注入其中的历史和记忆也会发出；本地存储不等于不出境。
- SnowLuma 许可与 QQ 使用条款应按采用版本审查；网络接入不自动免除商用、部署或分发限制。

## 10. 从旧草案迁移的设计决定

| 旧设计 | 当前设计 |
|---|---|
| 所有代码放一个应用目录，靠约定分层 | ai 与 agent 独立分发；业务在 moonfox-app，显示在 web-ui |
| 会话运行时与应用宿主提前独立分发 | 首版合并到 moonfox-app，内部 runtime/services 分层；出现第二个复用宿主后再考虑抽包 |
| WebUI 前端与服务端混放 | 浏览器前端独立为 web-ui；HTTP/WS 服务端留在业务包 server |
| 把多会话调度等同于 orchestrator | SessionHub 管多会话，首版不引入多 Agent 编排包 |
| 核心 user/steering/follow-up/autonomous 四队列 | 核心 steering/follow-up；inbox 和主动策略在宿主/扩展 |
| 用软中断 reset 同一个 token | 每次执行单向 token；取消与新运行由宿主协调 |
| core 到轮数上限自行 compact 并无限重入 | core 返回通用限制原因；runtime/宿主决定是否继续，受总预算约束 |
| SessionHub 直接构造人设/记忆/QQ 对象 | 宿主 SessionFactory 组装扩展；runtime 不解析 QQ key |
| 业务 entry/event 类型进入核心 | 命名空间 CustomEntry / 扩展诊断 / 通道 DTO |
| 扩展系统后补 | 在人设、记忆、QQ 实现之前完成最小 SDK 和生命周期 |
| QQ 必须 self-write driver 因框架不能兼容 | 保持已选直连方案；NoneBot 可作为未来通道实现，并非必然冲突 |
| MCP 无通知且权限无法控制 | MCP 是可选工具集成，通知不等于 QQ 接入；参数授权仍可做，当前不用于主链路 |

不修改原始 mini-pi 参考文档；对其示例还需验证：单 Queue 多消费者是竞争消费而非广播，浅拷贝不是嵌套内容的稳定快照，未完成流需确定性终止，压缩不能丢 prompt/工具声明或拆散工具配对。

## 11. 实施顺序与验收

| 阶段 | 产出 | 独立性验收 |
|---|---|---|
| A | 包骨架、依赖规则、faux provider | ai/core 分别构建安装，脱离宿主运行 |
| B | 核心循环、双队列、工具/事件/取消、运行预算 | 无领域依赖，全分支确定性测试 |
| C | moonfox-app 的 runtime、extension_api、扩展管理器及 headless 装配 | headless_app 示例可跑；不启用 server/前端/业务扩展也能保存与恢复会话 |
| D | 应用服务、SessionHub、配置/授权、server 协议、独立 web-ui | 前端只通过协议工作；关闭浏览器不取消 Agent；无前端产物也可使用业务包 |
| E（已实现） | 独立人设/记忆包、通用配置/管理与 WebUI | 禁用后核心不受影响；跨域记录/检索/工具/缓存/作业/删除，原输入预算与独立安装 |
| F | QQ 扩展 + SnowLuma 联调 | 假 OneBot 测通后真机验收；可禁用 QQ 而保留 Agent |
| G | 运维与后续扩展 | 备份恢复、删除传播、outbox unknown、预算、插件生命周期 |

必须自动化的检查：核心两包独立 wheel 安装、业务包 headless 测试、包依赖闭包、runtime 不 import services/server/具体扩展、SDK 导入无启动副作用、前后端协议契约、插件启停资源释放、权限失败拒绝、边界批提交恢复、首轮 context hook、多会话隔离、QQ/Agent 记忆隔离、慢客户端 resync、取消/重试总预算。

**最终目标：只安装 ai + agent，就能在别的项目中使用 Agent 核心；安装 moonfox-app 可获得完整业务宿主。移除 WebUI 不影响 QQ，关闭人设/记忆/QQ 扩展不影响基础 Agent 对话。**
