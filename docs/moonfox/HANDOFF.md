# 会话交接摘要

> **用途**：当前实施交接文档，保留设计决策并记录已验证的阶段进展。新会话从本页继续，无需回溯历史对话。
>
> **当前状态**：**阶段 A 已实现并通过验收**：独立 ai/agent 包、依赖规则、faux provider、最小工具续轮链路和独立安装测试。下一步是阶段 B。

---

## 0. 一句话状态

阶段 A 的两包骨架、AI 原语、faux provider 与最小顺序工具循环已落盘；两个包的独立构建、干净安装及仓库外示例已验证。安装与检查命令见根 [README](../../README.md)。

### 当前代码边界

- `moonfox_ai.api`：消息/事件、有界单消费者流、transcript 重放、估算/溢出、faux provider；仅依赖标准库。
- `moonfox_agent.api`：最小顺序工具续轮、Pydantic 参数校验、运行事件、显式轮数预算、单向取消与等待 idle；项目内仅依赖 ai。
- `queues.py` 是已测试的独立 FIFO 原语，**尚未接入运行调度**；`hooks.py` 只预留阶段 B 的契约，**尚未派发**。
- 阶段 B 继续：steering/follow-up 双队列调度、完整请求/工具/turn 钩子、并行及嵌套工具、continue/reset 与全分支测试。不宣称这些能力已实现。
- 尚未创建应用包、WebUI、具体扩展或真实 provider。

### 已验证与复现

- `python scripts/check.py --installation`：Ruff、格式检查、mypy strict、单元/架构测试及安装验收。
- 安装验收分别构建 sdist → wheel，验证 AI 单包、Agent wheel 自动解析 AI 依赖、依次本地目录安装三个干净环境；均不安装宿主/扩展/Web 框架。
- 示例复制到仓库外，使用 `python -I` 执行，输出 `2 + 3 = 5` 与 `reason=completed; turns=2; requests=2`。
- 实测 Windows / Python 3.14.8；声明最低 Python 3.12，但本机没有对 3.12 单独执行测试。开发工具安装在本会话 scratch 虚拟环境，不改全局 Python。
- Git 已初始化为 `main`；阶段 A 基线将推送至公开仓库 `https://github.com/FYHC1/moonfox-agent`，提交使用 GitHub noreply 邮箱。

实现修正参考伪代码：取消信号以 AI 层协议表示，避免反向依赖；流不是广播，溢出明确失败且最终结果可等待；未终止流确定性报错；快照采用深拷贝；未知自定义消息要求显式 codec，编程错误不冒充模型失败。

文档维护说明：本轮 Ruff 曾误格式化 Markdown 代码块；当前 HANDOFF/DESIGN 已精确恢复后仅更新状态，mini-pi 与历史草案的排版调整经用户确认保留。Ruff 已限定 Python 文件并排除 docs，后续不再格式化参考文档。

---

## 1. 用户原始需求

> 我想要做个人 agent，以 pi agent 的核心设计思想来构建 agent 核心，可以通过 webui 使用和配置；同时对接 QQ，具有个性化人设和记忆的聊天机器人。项目下的 docs\mini-pi 路径中有着 agent 核心的实现方案，结合目前需求设计可行方案。

**性质**：个人自用项目，非商业产品，非多租户 SaaS。

---

## 2. 已确认决策（用户拍板，实施时不可再议）

| # | 决策 | 内容 |
|---|---|---|
| D1 | **核心必须独立** | Agent 核心可独立安装、运行、测试、修改、移植，不依赖应用层 |
| D2 | **业务能力扩展化** | 记忆、人设、QQ 接入都实现为可启停、可替换的扩展 |
| D3 | **QQ 接入路线** | 方案 A：直连协议端 + 自写薄 OneBot 11 客户端。**不用** TypeScript SDK / MCP / NoneBot2 |
| D4 | **协议端** | **SnowLuma**（对接 QQ 稳定），NapCat 作为可切换备选 |
| D5 | **自我认知** | 人设内回答（`honesty_mode="roleplay"` 默认），保留 honest/deflect 配置档 |
| D6 | **记忆隔离** | QQ 聊天记忆与 Agent 对话记忆**硬隔离**（结构性，非命名约定） |
| D7 | **轮数预算** | `max_turns_per_run` 外部可配置，应用解析后注入，核心不写死数值 |
| D8 | **架构总纲** | 两个独立核心包 + 一个业务应用包 + 一个显示包 |

### 决策的推导过程（供理解，非重新讨论）

- **D3 为什么不用 MCP**：MCP 没有事件推送原语。QQ 消息是外部事件驱动，MCP tools 是模型主动调用，方向相反。且 `invoke_action` 能调任意 OneBot action（含禁言/踢人），爆炸半径不可控。
- **D3 为什么不用 NoneBot2**：它会拿走 asyncio 事件循环，与 AgentSession 编排竞争；其 session 是键值存储，与我们的完整 agent 会话层级不同。
- **D6 为什么是结构性隔离**：靠 `qq:` 前缀约定，一次拼写错误就打穿隔离；显式 `domain` 列 + 无跨域查询路径，代码审计能一眼确认。隐私边界，结构 > 约定。

---

## 3. 最终架构

### 3.1 与 pi 的职责对应

| pi 层 | Moonfox 对应 | 说明 |
|---|---|---|
| `ai` | `packages/ai` | 统一模型能力 |
| `agent` | `packages/agent` | Agent 原语，只依赖 ai |
| `coding-agent` | `packages/moonfox-app` | 具体业务：运行时 + 宿主 + 服务端 |
| `tui` | `packages/web-ui` | 显示层 |
| `orchestrator` | **不设** | 多会话由 SessionHub 管理，不等于多 Agent 编排 |

### 3.2 依赖方向

```text
extensions/persona、memory、qq
    └─依赖→ moonfox_app.extension_api（轻量公开契约）

moonfox-app ──依赖→ agent ──依赖→ ai

web-ui ──HTTP/WS 协议──→ moonfox-app/server
```

**核心仅指 ai + agent 两包。**

### 3.3 运行时装配

```text
浏览器 web-ui ──命令/事件──→ moonfox-app/server（传输适配，不放业务规则）
                                    ↓
SnowLuma ↔ QQ 宿主扩展 ↔ ChannelGateway / 应用服务
                                    ↓
                         SessionHub / SessionFactory
                          ├─ 授权、隔离绑定、预算、inbox/outbox
                          ├─ 加载已授权的会话扩展
                          └─ runtime/AgentSession → Agent Core → AI Provider
```

---

## 4. 目录规划（ai/agent 与核心示例已创建，其余待后续阶段）

```text
moonfox-agent/
├── pyproject.toml                    # Python 工作区
├── docs/
│   ├── mini-pi/                      # 原始参考，不改动
│   └── moonfox/                      # 当前架构、接口设计、历史、本交接
├── packages/
│   ├── ai/
│   │   ├── pyproject.toml
│   │   ├── src/moonfox_ai/
│   │   │   ├── api.py                # 对外导出面
│   │   │   ├── types.py
│   │   │   ├── stream.py
│   │   │   ├── transcript.py
│   │   │   ├── estimate.py
│   │   │   ├── overflow.py
│   │   │   └── providers/            # base / faux / 首个真实 provider
│   │   └── tests/
│   ├── agent/
│   │   ├── pyproject.toml
│   │   ├── src/moonfox_agent/
│   │   │   ├── api.py
│   │   │   ├── types.py
│   │   │   ├── agent.py
│   │   │   ├── loop.py
│   │   │   ├── tools.py              # 执行机制，不含业务工具实现
│   │   │   ├── hooks.py
│   │   │   ├── queues.py             # steering / follow-up
│   │   │   └── abort.py
│   │   └── tests/
│   ├── moonfox-app/
│   │   ├── pyproject.toml            # server 依赖为可选 extra
│   │   ├── src/moonfox_app/
│   │   │   ├── api.py                # headless 公开入口
│   │   │   ├── bootstrap.py          # 唯一装配根
│   │   │   ├── runtime/
│   │   │   │   ├── session/          # entries / projection / manager / orchestration
│   │   │   │   ├── storage/          # SessionStore；JSONL / in-memory
│   │   │   │   ├── compaction/       # 可注入摘要策略
│   │   │   │   └── prompt/           # section 重放、diff、上下文管线
│   │   │   ├── services/             # hub / actor / routing / session_factory / outbox
│   │   │   ├── extensions/           # registry / dispatch / lifecycle
│   │   │   ├── extension_api/        # api / types / events / capabilities
│   │   │   ├── config/               # schema / loader / secrets / reload
│   │   │   ├── security/             # 身份、作用域、工具授权、审批
│   │   │   ├── jobs/                 # 后台任务、并发、预算、作业恢复
│   │   │   └── server/               # HTTP/WS、协议 schema、wire
│   │   └── tests/
│   └── web-ui/
│       ├── package.json              # 前端工程，非 Python 包
│       ├── src/{client,state,components,pages}/
│       └── tests/
├── extensions/
│   ├── persona/  → src/moonfox_ext_persona/   # plugin / schema / store / render
│   ├── memory/   → src/moonfox_ext_memory/    # plugin / scopes / store / retrieval / worker / tools
│   └── qq/       → src/moonfox_ext_qq/        # plugin / onebot / codec / trigger / sender
├── examples/
│   ├── core_only.py                  # 只装 ai + agent 即可运行
│   ├── headless_app.py               # 用业务包，不启动 HTTP/WS 或 QQ
│   └── minimal_extension/
├── config/examples/                  # 无密钥配置、人设示例
└── tests/architecture/               # 依赖图、公开接口、独立安装、UI 协议检查
```

### 业务包内部边界

| 模块 | 职责 | 约束 |
|---|---|---|
| `extension_api` | 扩展 API、DTO、事件、能力协议 | 轻量；不 import 具体服务 |
| `runtime` | 单会话 AgentSession、存储、投影、prompt、压缩 | 不 import services/server/具体扩展 |
| `extensions` | 注册表、派发、生命周期 | 通过注入能力执行，不直接拼装会话 |
| `config/security/jobs` | 配置、授权、后台作业与预算 | 不反向调用 server 或插件 |
| `services` | SessionHub、路由、inbox/outbox、业务用例 | 可用 runtime/extensions 的公开内部接口 |
| `server` | HTTP/WS、鉴权适配、序列化、静态托管 | 不直改 AgentState |
| `bootstrap` | 唯一装配根，拥有启停顺序 | — |

---

## 5. 关键契约摘要

完整定义见 [DESIGN.md](DESIGN.md)。

### 核心 run 契约

```python
@dataclass(frozen=True, slots=True)
class RunOptions:
    max_turns_per_run: int   # 必填，>= 1；宿主解析配置后注入

@dataclass(frozen=True, slots=True)
class RunOutcome:
    reason: Literal["completed", "aborted", "error", "limit_reached"]
    turns_used: int
```

- 一次 turn = 一次 assistant 响应 + 该响应的工具批次
- 达到上限返回 `limit_reached`，**不**自行压缩、提示、重载配置或清零预算
- 取消使用**单向**令牌，设置后不可 reset；宿主 wait_for_idle/settled 后才能建新 run

### 扩展 SDK

```python
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
```

- `setup` 只声明注册，不启动无限任务；`attach/start` 才获取资源
- 后台任务必须经 TaskService 提交，不得留下无人管理的 `create_task`
- **会话级扩展**：人设、记忆。**宿主级扩展**：QQ（不能每开一个会话就启动一个监听端口）

### 会话扩展能力

| API | 契约 |
|---|---|
| `on(event, handler)` | 快照派发，稳定顺序，返回注销句柄 |
| `register_tool(definition)` | namespaced；参数 schema 校验；经过强制权限与嵌套检查 |
| `register_prompt_section(provider)` | 返回 section 草稿；写入由 runtime 校验提交 |
| `register_context_provider(provider)` | 提供参考内容；不能覆盖权限或工具声明 |
| `register_command(definition)` | 显式命令，入口鉴权由宿主完成 |
| `stage_entries(drafts)` | 只能提交授权范围的 namespaced custom entry |
| `enqueue(message, delivery)` | 仅当前会话 steer/follow_up；不绕过预算 |

### 宿主扩展能力（QQ）

```python
class ChannelGateway(Protocol):
    async def submit(self, message: ChannelInput) -> InputReceipt: ...
    async def control(self, command: ChannelControl) -> None: ...
    def subscribe_replies(self, listener: ReplyListener) -> RegistrationHandle: ...
```

### 记忆隔离

```python
@dataclass(frozen=True, slots=True)
class MemoryScope:
    domain: Literal["qq", "agent"]
    scope_id: str

class ScopedMemoryStore(Protocol):
    async def search(self, query: str, limit: int) -> list[MemoryRecord]: ...
    async def remember(self, candidate: MemoryCandidate) -> str: ...
    async def forget(self, memory_id: str) -> None: ...
```

- 域绑定由应用服务在认证路由后生成**不可变 SessionBinding**
- 模型工具参数与前端输入**都不能**自由指定 domain
- 隔离覆盖：历史、摘要、记忆、画像、索引、检索缓存、请求快照、抽取作业、媒体、导入导出、删除、备份

---

## 6. 文档地图

| 文件 | 内容 | 何时读 |
|---|---|---|
| [README.md](README.md) | 文档入口、包布局、职责约束 | 先读 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 整体架构 11 节：目标映射、包边界、目录、核心保留/移出、扩展系统、记忆隔离、调用链、部署存储、实施顺序 | **实现前必读** |
| [DESIGN.md](DESIGN.md) | 接口设计 8 节：公开 API、run 契约、扩展 SDK、派发/上下文/事务、业务扩展契约、预算事件、验证清单 | 写接口时查 |
| [history/DESIGN-v1.md](history/DESIGN-v1.md) | 早期草案（2356 行） | **不要读**，已废弃 |
| [../mini-pi/DESIGN.md](../mini-pi/DESIGN.md) | pi 骨架 3499 行：类型、EventStream、transcript 重放、双层循环、钩子、边界草稿、19 条接缝 | **实现 ai/agent 层的主要参考** |

### mini-pi 中必须沿用的 19 条接缝（摘要）

1. AgentMessage/Message 分离 + `convert_to_llm` 唯一转换边界
2. `system_prompt` 只读，从 transcript 重放
3. 错误编码进流，异常只表编程错误
4. `EventStream` 的 `is_complete`/`extract_result` 参数化
5. 双队列 steering/follow-up + `one_at_a_time`/`all` 模式
6. 4 个钩子精确位置
7. `finish_turn` 的 continue/end/None 语义
8. session entry 带 `id` + `parent_id`
9. 投影是派生视图，原始 entry 永不删除
10. 扩展注册表 + 快照式派发
11. 钩子按返回值语义分四类（含 fail-closed）
12. 边界草稿 + 预览 + 原子提交
13. `SessionEvent` 独立于 `AgentEvent`
14. wire 格式剥离累积快照 `partial`
15. 背压作为独立订阅
16. 上下文原语（重放/估算/溢出）放 ai 层
17. 压缩/投影靠钩子挂载，不塞进 loop
18. 工具分两层（机制在 agent，实现在 app）
19. 事件类型逐层增加，不合并

---

## 7. 环境事实（已实测）

| 项 | 值 |
|---|---|
| 工作目录 | `C:\CustomFiles\CodeProjects\pythonProjects\moonfox-agent` |
| 操作系统 | Windows 10.0.26300 |
| Python | **3.14.8**（`py -0` 显示 3.14 64-bit） |
| pip | 26.2.1 |
| Node.js | **v24.18.1**（SnowLuma 需 ≥22.13 ✓） |
| pnpm / npm | 12.9.1 / 11.19.0 |
| git | 2.56.0.windows.1 |
| **uv** | **未安装** |
| **docker** | **未安装** |
| 已装 Python 包 | uvicorn、pydantic（fastapi/websockets/httpx/aiosqlite/pytest/tiktoken **未装**） |

### SQLite（关键验证）

- 版本 **3.50.4**
- **FTS5 已编译，`trigram` 分词器中文检索实测通过**：
  ```python
  CREATE VIRTUAL TABLE m USING fts5(text, tokenize='trigram')
  # 存入 "用户喜欢喝冰美式，不加糖"
  # 查询 "用户喜欢喝什么咖啡" → 命中 ✓
  ```
- `unicode61` 需手工切词兜底；WAL 可用；`enable_load_extension` 可用（sqlite-vec 可加载）

### PyPI Python 3.14 wheel 兼容性（已全部验证，无编译障碍）

fastapi 0.143.0 / uvicorn 0.54.0 / pydantic 2.14.0 / pydantic-core 2.50.0(cp314) / aiosqlite 0.22.1 / websockets 17.2(cp314) / httpx 0.28.1 / pytest 9.1.1 / pyright 1.1.414 / tiktoken 0.14.0(cp314) / APScheduler 3.11.3 / jinja2 3.1.6 / sqlite-vec 0.1.9(win_amd64) / numpy 2.5.3(cp314)

### 可访问的 LLM 端点（均需 key）

api.deepseek.com、dashscope.aliyuncs.com、api.moonshot.cn、open.bigmodel.cn、api.siliconflow.cn（均返回 401）
api.openai.com、api.anthropic.com 返回 403（区域/WAF 拦截）

---

## 8. 实施路线

| 阶段 | 产出 | 独立性验收 |
|---|---|---|
| **A** | 包骨架、依赖规则、faux provider | ai/agent 分别构建安装，脱离宿主运行 |
| **B** | 核心循环、双队列、工具/事件/取消、运行预算 | 无领域依赖，全分支确定性测试 |
| **C** | moonfox-app 的 runtime、extension_api、扩展管理器、headless 装配 | headless 示例可跑；不启用 server/前端/扩展也能保存恢复会话 |
| **D** | 应用服务、SessionHub、配置/授权、server 协议、独立 web-ui | 前端只通过协议工作；关闭浏览器不取消 Agent |
| **E** | 人设扩展、记忆扩展 | 禁用后核心不受影响；跨域负例测试 |
| **F** | QQ 扩展 + SnowLuma 联调 | 假 OneBot 测通后真机验收；可禁用 QQ 保留 Agent |
| **G** | 运维与后续扩展 | 备份恢复、删除传播、outbox unknown、预算、插件生命周期 |

### 必须自动化的检查

- 核心两包独立 wheel 安装
- 业务包 headless 测试
- 包依赖闭包（AST / import-linter）
- runtime 不 import services/server/具体扩展
- SDK 导入无启动副作用
- 前后端协议契约
- 插件启停资源释放
- 权限失败拒绝
- 边界批提交恢复
- 首轮 context hook
- 多会话隔离
- QQ/Agent 记忆隔离
- 慢客户端 resync
- 取消/重试总预算

---

## 9. 阶段 A 交付与验收（已完成）

### 交付物

1. **根 `pyproject.toml`** —— Python 工作区、统一测试/检查入口
2. **`packages/ai/`** —— pyproject + `src/moonfox_ai/`（api/types/stream/transcript/estimate/overflow + providers/{base,faux}）+ tests
3. **`packages/agent/`** —— pyproject + `src/moonfox_agent/`（api/types/agent/loop/tools/hooks/queues/abort）+ tests
4. **`tests/architecture/test_layering.py`** —— AST 依赖断言
5. **`examples/core_only.py`** —— 只装 ai + agent 即可运行
6. **`packages/web-ui/package.json`** —— 按原计划后置，阶段 A 不创建前端工程

### 阶段 A 的验收

- [x] `pip install ./packages/ai` 与 `./packages/agent` 在干净环境成功
- [x] 不安装 moonfox-app、扩展、Web 框架，`examples/core_only.py` 可跑
- [x] faux provider 跑通「工具调用 → 下一轮响应」
- [x] `test_layering.py` 通过：`ai` 不 import `agent`；`agent` 不 import 应用/扩展；包含负例
- [x] 两个核心包的 `src` 无 `persona`/`memory`/`qq`/`moonfox_app` 业务词汇（自动化扫描）

### 阶段 A 实现顺序记录

```
1. 根 pyproject + 两个包骨架（空模块）
2. tests/architecture/test_layering.py（先立规矩）
3. ai/types.py（Message / ContentBlock / Usage / StopReason / Model）
4. ai/stream.py（EventStream + AssistantMessageEventStream）
5. ai/providers/{base,faux}.py（契约 + 假 provider）
6. ai/transcript.py / estimate.py / overflow.py
7. agent/types.py / queues.py / abort.py / hooks.py
8. agent/tools.py / loop.py / agent.py
9. examples/core_only.py + 端到端 faux 测试
```

**第 3-6 步的类型定义以 [mini-pi DESIGN.md](../mini-pi/DESIGN.md) §3 为准，第 7-8 步以 §4 为准。**

---

## 10. 禁止事项

实现时不要做这些（都已论证过）：

| 禁止 | 原因 |
|---|---|
| 在核心中放 `persona_id` / QQ 号 / 群号 / memory domain | 核心必须可移植 |
| 把四类消息队列放进核心 | 核心保留 steering/follow-up；业务 inbox 在应用层 |
| 让核心读产品配置或固定数据目录 | 破坏可移植性 |
| 核心调用 `compact()` 或 import `SessionEvent` | 压缩属于 runtime |
| 用可重置的 AbortToken 维持常驻 | 单向令牌；取消与新 run 由宿主协调 |
| 靠 `qq:` 前缀约定实现记忆隔离 | 必须用显式 `domain` 列 + 无跨域查询路径 |
| 把域绑定/授权做成可关闭的插件 | 安全边界不能随扩展关闭 |
| 提前引入 orchestrator | 多会话 ≠ 多 Agent 编排 |
| 让 QQ 每开一个会话就启动一个监听端口 | QQ 是宿主级扩展，一个实例管所有会话 |
| 引入 NoneBot2 / MCP / TypeScript SDK 做主链路 | 见 D3 推导 |
| 在扩展里 import `moonfox_app.runtime` 内部实现 | 只能用 `extension_api` |
| 每层各写一套协议定义 | 协议 schema 单一来源（server 维护） |
| 假设 JSONL「追加写」就等于 kill -9 零丢失 | 需定义 flush/fsync 边界 |

---

## 11. 附录：本会话确认的工具事实

排查会话压缩问题时确认（避免新会话重复踩坑）：

- **不存在名为 `compress` 的工具**。系统内压缩功能叫 **compaction**，用户命令是 **`/compact`**。
- 相关包：`dsh-compaction`（引擎）、`dsh-compaction-basic`（后端，默认 `thresholdRatio=0.8`、`retainRatio=0.16`）、`dsh-command-compact`（`/compact` 命令）。
- 「Compaction could not produce a useful summary」是 `dsh-command-compact` 中 `error.code === "summary"` 的既定文案。
- 另一条拒绝条件是 `busy`：有活跃压缩或 agent 未 idle。
- 会话很长时摘要可能失败，规避方式：分段压缩、调小 `thresholdRatio`、换摘要模型（`summarizationProvider`/`summarizationModel`）。

---

*文档生成时间：设计会话结束时。设计文档共 4 份 + 本交接，总计约 2900 行。*
