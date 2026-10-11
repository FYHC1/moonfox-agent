# 历史草案：已被独立核心与扩展架构替代

> 仅保留讨论历史，不作为实施契约。四类核心队列、可重置取消令牌、核心直接压缩/发送 QQ、框架必然冲突等论述均不应直接沿用。当前设计见 [整体架构](../ARCHITECTURE.md) 与 [接口设计](../DESIGN.md)。

# moonfox agent 设计方案

> 目标：以 pi agent 的核心设计思想构建 agent 核心，通过 WebUI 使用与配置，对接 QQ，具备个性化人设与长期记忆的聊天机器人。
>
> 本文是落盘方案：需求映射、架构分层、模块契约、数据模型、事件协议、实施路线图、风险取舍。所有结构性决定都给出「缺了它会怎样」。
>
> 阅读前置：[mini-pi 参考](../../mini-pi/DESIGN.md)（agent 核心骨架）。本文不重复 mini-pi 已定义的类型，只写**改动**与**新增**。

---

## 0. 需求拆解与结论摘要

### 0.1 需求 → 架构映射

| 需求 | 落点 | 关键决定 |
|---|---|---|
| 以 pi 核心设计思想构建 agent 核心 | `src/moonfox/ai` + `src/moonfox/agent` 两层，按 mini-pi 实现 | 保留 19 条接缝中的 18 条，只改循环退出条件 |
| WebUI 使用 | `src/moonfox/webui` 只订阅 `SessionEvent`，通过 wire 协议 | UI 不得 import agent 以下任何包 |
| WebUI 配置 | `src/moonfox/config` + `src/moonfox/app/runtime.py` 的命令通道 | 配置写入独立 override 文件，不覆写手写配置 |
| 对接 QQ | `src/moonfox/channels/qq`，OneBot 11 反向 WS 适配器 | 协议用 OneBot 11，协议端用 **SnowLuma**，驱动层可换 |
| 个性化人设 | `src/moonfox/persona` → prompt `persona` section | 人设是**可热替换的 prompt section**，不是字段 |
| 长期记忆 | `src/moonfox/memory` + SQLite | 存储、检索、写入全在 agent 层之外，靠钩子挂载 |

### 0.2 三个不可逆决定（先说结论）

**决定 1：agent 循环从「批处理」改为「持续会话」，但用「有界内层循环 + 外层重入」实现。**

pi / mini-pi 的循环假设：一次 `prompt()` 处理一批输入，处理完 `agent_end`，run 结束。
聊天机器人的现实：QQ 群里消息随时到达，agent 可能需要连续多轮，也可能长时间空闲。

天真做法是「每来一条消息起一次 run」。**这会导致**：工具执行期间到达的消息要么丢失要么排队到下一轮，无法 steering；无法做「打断」；并发起多个 run 会与 session 的投影/压缩打架。

本方案的解法：**循环退出条件参数化**。内层循环在「没有待发消息且没有工具结果」时**不一定**结束，而是查外部队列与自主预算；同时给内层循环加**轮数上限**，到上限就正常 `agent_end`，由外层 `AgentSession` 重入。这样压缩/重试/边界这些 run 级策略仍然定期有机会执行。

**决定 2：消息队列从「两类」扩展为「四类」，队列原语下沉到 agent 层。**

| 队列 | 语义 | 投递时机 | 谁写 |
|---|---|---|---|
| `user` | 外部用户消息（QQ / WebUI） | 每轮边界立即投递 | channel adapter |
| `steering` | 工具执行期间的插话 | 当前 turn 结束后注入 | 外部 + 扩展 |
| `follow_up` | agent 本应停止后追加 | 内层循环耗尽后注入 | 外部 + 扩展 |
| `autonomous` | agent 自己发起的话题 | 仅在前三类都空且预算 > 0 时 | 扩展 / 定时任务 |

前两类在 mini-pi 已有；`user` 是新增的「真实外部输入」通道，`autonomous` 是新增的「agent 自发」通道，用**预算**硬约束，防止 agent 自我驱动无限循环烧钱。

**决定 3：记忆与人设都不进 agent 核心，全部靠已有的三个钩子挂载。**

| 机制 | 挂载点 | 用的现有接缝 |
|---|---|---|
| 人设注入 / 热更新 | `prepare_next_turn` + `sections` diff | 接缝 2（system_prompt 只读、从 transcript 重放） |
| 记忆召回 | `prepare_next_turn` 生成 `memory` section | 接缝 17（压缩/投影靠钩子挂载，不塞进 loop） |
| 记忆写入 | 订阅 `agent_settled` 会话事件 | 接缝 13（SessionEvent 独立于 AgentEvent） |
| 记忆工具 | 注册为普通 `AgentTool` | 接缝 18（工具分两层） |

**收益**：`src/moonfox/agent` 完全不知道人设和记忆的存在，可以被单独复用、单独测试；换一套记忆算法不需要动核心。

### 0.3 与 mini-pi 文档的差异总览

| 项 | mini-pi | 本方案 | 理由 |
|---|---|---|---|
| 循环退出条件 | 队列空即 `agent_end` | 加外部队列 + 自主预算 + 轮数上限 | 聊天机器人需要持续在线 |
| `Agent.continue_()` | 队列空且末条是 assistant 时抛错 | 返回 `bool`，支持 `allow_context_only` | 会话层需要「还能不能继续」的判断 |
| 中断 | 单一 `abort()` | 三级：`interrupt()` / `abort()` / `shutdown()` | 「打断这句」和「停掉整件事」是两回事 |
| 队列 | 2 类 | 4 类 | 见决定 2 |
| 模型目录 | 手写 TOML | 手写 TOML + 运行时覆盖 + WebUI 编辑 | 个人项目，配置要能在 UI 里改 |
| 内置工具 | read/write/edit/bash/grep/find/ls | 加 `memory_*`、`persona_*`、`channel_*` 命名空间 | 聊天场景不需要写代码，但需要操作记忆 |
| 扩展系统 | 完整四类语义 | **阶段 6 再上**，先只用「钩子 + 工具注册」 | 个人项目，扩展生态是负担 |
| UI | TUI / Web | 只做 Web | 需求明确 |
| 通道 | 无 | 一等公民 `src/moonfox/channels`；QQ 走「直连 SnowLuma + 自写薄 OneBot 客户端」 | 需求明确；不引入 Node/MCP/NoneBot2，见 §10.1 |
| 存储 | JSONL 会话文件 | JSONL 会话 + SQLite 索引/记忆 | 会话是流式追加，记忆是关系型查询 |
| 依赖 | pydantic | pydantic + fastapi + aiosqlite | 见 §1.3 |

---

## 1. 整体架构

### 1.1 六层依赖图

```
        ┌─────────────────────────────────────────────────────┐
        │  webui          channels            (边界层)          │
        │  HTTP / WS      QQ / OneBot 11                       │
        └───────────────┬─────────────────┬───────────────────┘
                        │                 │
        ┌───────────────▼─────────────────▼───────────────────┐
        │  app          runtime (装配) · prompt · tools ·      │
        │               persona · memory · scheduler          │
        └───────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────▼─────────────────────────────────────┐
        │  session      store · projection · compaction ·      │
        │               orchestration · boundary · events      │
        └───────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────▼─────────────────────────────────────┐
        │  agent        loop · tools · hooks · queue · agent   │
        └───────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────▼─────────────────────────────────────┐
        │  ai           types · stream · transcript · estimate │
        │               overflow · providers/{openai,faux,...} │
        └─────────────────────────────────────────────────────┘

        config  ← 被所有层读取，不依赖任何层
        core    ← 日志/错误/ID/时钟，被所有层依赖，依赖零
```

### 1.2 依赖规则

| 包 | 可以 import | 不可以 import |
|---|---|---|
| `core` | 标准库 | 任何 moonfox 包 |
| `config` | `core` | 其他 |
| `ai` | `core`、标准库、pydantic | `agent`/`session`/`app`/`persona`/`memory`/`channels`/`webui` |
| `agent` | `ai`、`core`、`config` | `session` 及以上 |
| `session` | `ai`、`agent`、`core`、`config` | `app` 及以上 |
| `persona` | `core`、`config`、pydantic | `ai` 及以上（**纯数据 + 渲染**，不碰模型） |
| `memory` | `ai`、`core`、`config`、pydantic | `agent` 及以上 |
| `app` | `ai`、`agent`、`session`、`persona`、`memory`、`core`、`config` | `channels`、`webui` |
| `channels` | `app`（只依赖其公开的 `SessionHub` 接口）、`core`、`config` | `webui` |
| `webui` | 全部（只读事件、只发命令） | — |

**强制方式**：AST 测试，见 §15.1。规则要写成表，新增包必须同时加一行，否则测试失败。

`persona` 被刻意做成**不依赖 `ai`**：它只做「配置 → 一段文本」，可以脱离 agent 单独测试，也防止有人在人设渲染里偷偷调模型。

### 1.3 技术选型与依据

**运行时**：Python 3.14（本机 3.14.8）。已实测全部依赖有 3.14 兼容 wheel，无编译障碍：

| 依赖 | 版本 | 用途 | 3.14 状态 |
|---|---|---|---|
| `pydantic` | 2.14 | 工具参数 schema、配置校验 | `pydantic-core` 有 cp314 wheel |
| `fastapi` + `uvicorn` | 0.143 / 0.54 | WebUI HTTP + WS | 纯 Python |
| `httpx` | 0.28 | provider HTTP 客户端 | 纯 Python |
| `websockets` | 17.2 | QQ 反向 WS 服务端 | 有 cp314 wheel |
| `aiosqlite` | 0.22 | SQLite 异步访问 | 纯 Python |
| `jinja2` | 3.1 | WebUI 模板（可选，走无构建路线） | 纯 Python |
| `apscheduler` | 3.11 | 定时任务（主动搭话、记忆整理） | 纯 Python |
| `tiktoken` | 0.14（可选） | 精确 token 计数 | 有 cp314 wheel |
| `pyright` / `pytest` | — | 类型检查 / 测试 | 纯 Python |

**不引入的**：

- `sqlmodel`/`SQLAlchemy`：schema 很小，手写 SQL + 一个薄封装更透明，也避免 ORM 与异步的额外心智负担。
- `nonebot2`/`astrbot` 等机器人框架：它们会接管事件循环与插件生命周期，与我们的 `AgentSession` 编排冲突。只借用 OneBot 11 协议（标准文档）。
- `langchain`/`llama-index`：记忆系统要的检索逻辑不到 300 行，引入后反而难以调优。
- 向量数据库：见 §6.2，先用 SQLite FTS5（**已实测本机 SQLite 3.50.4 支持 FTS5 且 `trigram` 分词器对中文检索有效**）。

**前端**：无构建起步 —— 原生 ES Module + 一个 `index.html` + 少量 CSS。理由：这个 WebUI 的复杂度上限是「聊天 + 表单 + 表格」，引入 Vite/React 会让「改一行要重新构建」的成本高于收益。如果后续确实需要，`webui/static` 目录可以被任意前端工程替换，因为它只消费 WS 事件与 HTTP 命令，**接口面是稳定的**。

**存储**：双轨。

| 数据 | 载体 | 理由 |
|---|---|---|
| 会话条目 | JSONL 追加写（`~/.moonfox/sessions/<id>.jsonl`） | 追加 O(1)；进程被杀只丢半行；可直接 `tail -f` 调试；可被外部工具消费 |
| 记忆 / 索引 / 配置 / 用量 | SQLite（`~/.moonfox/moonfox.db`） | 需要查询、排序、聚合、事务 |

两者用 `session_id` 关联。JSONL 是事实源，SQLite 里的会话索引是**派生视图**，可随时重建（对应接缝 9 的思想：投影是派生视图）。

### 1.4 部署拓扑

```
                    ┌──────────────────────────────────────────┐
                    │  moonfox 单进程（单个 asyncio event loop） │
                    │                                          │
  QQ 客户端 ◄──NTQQ──► NapCat ──反向WS──► ┌──────────────────┐    │
                    │                    │ QQChannel        │    │
                    │                    │ (websockets 服务端)│    │
                    │                    └────────┬─────────┘    │
                    │                             │              │
                    │                    ┌────────▼─────────┐    │
   浏览器 ◄──HTTP/WS──┼──────────────────► │ SessionHub       │    │
                    │                    │ channel_key→会话  │    │
                    │                    └────────┬─────────┘    │
                    │                             │              │
                    │                    ┌────────▼─────────┐    │
                    │                    │ AgentSession × N │    │
                    │                    │ (按会话隔离)      │    │
                    │                    └────────┬─────────┘    │
                    │                             │              │
                    │         ┌───────────────────┼───────────┐  │
                    │         │                   │           │  │
                    │    JSONL store        MemoryWorker   WebUI │
                    │    (sessions/)        (SQLite)      (WS)  │
                    └──────────────────────────────────────────┘
```

**为什么单进程**：会话状态、记忆缓存、WebUI 订阅、QQ 连接全部共享内存，没有 IPC 序列化成本，也没有「两个进程看到不同投影」的一致性风险。个人机器人量级（< 100 会话，< 10 msg/s）单进程绰绰有余。

**瓶颈与出路**：CPU 密集工具走 `run_in_executor`；SQLite 写入集中到单个 writer task（§6.2）；如果将来需要多进程，`SessionEvent` 的 wire 格式（§8.4）已经是完整的跨进程协议，直接架在 Unix socket / WS 上即可，**不需要改核心**。

**Windows 注意**：本机是 Windows，没有 `uvloop`；`SIGTERM` 语义受限，优雅退出用 `KeyboardInterrupt` + WebUI 的 `POST /api/shutdown` 双通道；文件路径统一走 `pathlib`，配置里的路径允许 `~` 展开。

---

## 2. 对 mini-pi 的复用与改动清单

### 2.1 原样复用（不改一行）

这些接缝是 pi 架构的骨架，改动它们会引发连锁重构：

1. `AgentMessage` / `Message` 分离 + `convert_to_llm` 唯一转换边界 —— **记忆注入的 `<memory>` 提示条、工具进度卡片都是 UI-only 消息，全靠这条**
2. `system_prompt` 只读，从 transcript 重放 —— **人设热更新全靠这条**
3. 错误编码进流，异常只表编程错误 —— QQ 场景网络抖动频繁，两套错误路径会失控
4. `EventStream` 的 `is_complete` / `extract_result` 参数化 —— WebUI 多订阅者、中途挂载都靠它
5. `finish_turn` 的 `continue` / `end` / `None` 语义
6. session entry 带 `id` + `parent_id` —— **QQ 场景天然多线程（一个群多个话题），`/tree` `/fork` 迟早要用，格式必须第一天带上**
7. 投影是派生视图，原始 entry 永不删除
8. 边界草稿 + 预览 + 原子提交
9. `SessionEvent` 独立于 `AgentEvent`，`will_retry` 是派生字段
10. wire 格式剥离累积快照 `partial`
11. 背压作为独立订阅
12. 上下文原语（重放 / 估算 / 溢出）放 `ai` 层
13. 压缩 / 投影靠钩子挂载，不塞进 loop
14. 工具分两层（机制在 `agent`，实现在 `app`）
15. 事件类型逐层增加，不合并
16. 钩子按返回值语义分四类（含 fail-closed）
17. 快照式派发
18. `AbortToken` 替代 `asyncio.CancelledError`

### 2.2 必须改动

#### 改动 A：循环退出条件参数化（对应决定 1）

见 §4。

#### 改动 B：`Agent.continue_()` 的返回契约

mini-pi：队列空且末条是 assistant → `raise ValueError`。
本方案：返回 `bool`（是否真的跑了一轮），并在「末条是 assistant」时按参数决定：

```python
async def continue_(
    self,
    *,
    allow_context_only: bool = False,
) -> bool:
    """从当前 transcript 继续。

    返回 True 表示确实发起了一轮请求。

    末条是 assistant 时：
      - 先尝试 steering 批次，再尝试 user 批次，再尝试 follow-up 批次
      - 都空且 allow_context_only=True → 发一次 context-only 请求
        （这是 finish_turn 返回 "continue" 时的语义）
      - 都空且 allow_context_only=False → 返回 False，不抛异常

    为什么不再抛异常：会话层需要「还能不能继续」是一个**可查询的状态**，
    而不是靠捕获异常来判断。抛异常会让 _run 的循环写成 try/except 套 try/except。
    """
```

配套新增只读属性：

```python
@property
def is_idle(self) -> bool:
    """当前没有活跃 run。"""


@property
def can_continue(self) -> bool:
    """transcript 的末条能转成 user / toolResult（provider 不会拒绝）。"""


@property
def has_pending_input(self) -> bool:
    """四个队列里任意一个有货。"""
```

#### 改动 C：中断三级

```python
class Agent:
    def interrupt(self) -> None:
        """软中断：中止当前正在流式的 assistant 响应与未开始的工具，
        丢弃部分输出，但保留已完成的 turn 与队列内容。

        用途：QQ 里用户说「算了」「别写了」「停」。
        结果：当前 assistant 消息 stop_reason="aborted"，随后循环**继续**
              处理队列里的下一条 —— 这才是聊天场景想要的行为。

        实现：给当前 run 的 AbortToken 打标记，但用一个独立的
              _interrupt_only 标志区分「只断这一轮」和「断整个 run」。
        """

    def abort(self) -> None:
        """硬中断：中止当前 run，不再继续队列。"""

    async def shutdown(self) -> None:
        """等待当前 run 结束（最多 grace_ms），然后释放所有资源。"""
```

`_handle_run_failure` 与循环的硬出口需要同时检查 `token.aborted` 与 `self._interrupt_only`：

```python
if message.stop_reason == "aborted":
    if self._interrupt_only:
        # 软中断：作为一次普通 turn_end 处理，继续外层循环
        self._interrupt_only = False
        last_turn = TurnContext(message, [], current, new_messages)
        await emit(TurnEnd(message, []))
        # 注意：不 return，不 emit AgentEnd
        continue
    else:
        # 硬中断：走 mini-pi 原路径
        ...
        await emit(AgentEnd(new_messages))
        return
```

**为什么必须分两级**：只有一个 `abort` 时，「用户中途插话」和「用户关掉机器人」走同一条路径。前者应该让 agent 接着回应新消息，后者应该停掉一切。用同一个信号表达两件事，迟早会出现「用户插了一句，整个 run 被终止，新消息没人处理」。

#### 改动 D：`AgentState` 增加运行时用量与成本

```python
@dataclass(slots=True)
class AgentState:
    # ... mini-pi 已有字段
    usage_total: Usage = field(default_factory=Usage)
    """本会话累计用量。每次 AssistantMessage 到达时累加。"""

    last_request_tokens: int = 0
    """最近一次请求的上下文 token 估算，用于 WebUI 的上下文占用条。"""
```

QQ 场景按消息计费敏感，用量必须可见。

#### 改动 E：`AgentInitialState` 增加 `session_id` 与 `metadata`

```python
@dataclass(slots=True)
class AgentInitialState:
    system_prompt: str = ""
    model: Model | None = None
    thinking_level: ThinkingLevel = "off"
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    # 新增
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    """surface / channel_key / persona_id 等，只读快照，供钩子与工具读取。"""
```

### 2.3 可以后补

- 多 provider 并行（先做 1 个真实 OpenAI 兼容 + 1 个 faux）
- 虚拟模型 / 模型路由
- 缓存预热
- 会话树 UI（存储层已支持）
- MCP、codemode
- 扩展系统完整四类语义（先做「钩子 + 工具注册」两件事）
- 自持压缩（`first_kept_entry_id: None`）

---

## 3. 包结构（落盘）

### 3.1 目录树

```
moonfox-agent/
  pyproject.toml
  README.md
  docs/
    mini-pi/                     # 已有：agent 核心骨架方案
    moonfox/DESIGN.md            # 本文
  config/
    default.toml                 # 随仓库分发，只读基线
    moonfox.toml                 # 用户手写配置（不入版本控制）
    overrides.toml               # WebUI 写入（不入版本控制）
  src/moonfox/
    core/                        # L0 基础设施
      __init__.py
      ids.py                     # ULID / entry_id 生成
      clock.py                   # now_ms()，测试可注入
      logging.py                 # 结构化日志，与事件通道隔离
      errors.py                  # MoonfoxError 层级
      json.py                    # 可序列化 dataclass <-> dict（含判别字段）
      paths.py                   # 应用目录解析（~/.moonfox）

    config/                      # L0.5 配置
      __init__.py
      schema.py                  # Pydantic 模型：AppConfig 及子配置
      loader.py                  # 分层加载 + 覆盖 + 环境变量
      secrets.py                 # 密钥解析：env > keyfile > config
      watcher.py                 # 文件变更 -> 热重载信号

    ai/                          # L1 供应商层（照 mini-pi 实现）
      __init__.py
      types.py
      stream.py
      transcript.py
      estimate.py
      overflow.py
      catalog.py                 # 模型目录：TOML + 运行时覆盖
      auth.py
      providers/
        base.py
        openai.py                # OpenAI 兼容（覆盖 DeepSeek / 通义 / 月之暗面 / 智谱 / SiliconFlow）
        faux.py
        registry.py              # 名字 -> StreamFn 工厂

    agent/                       # L2 核心原语
      __init__.py
      types.py
      hooks.py
      queue.py                   # 新增：MessageQueue（4 类队列的统一原语）
      abort.py
      stream.py
      tools.py
      loop.py                    # 改动：退出条件 + 中断分级 + 轮数上限
      agent.py

    session/                     # L3 会话与编排
      __init__.py
      entries.py                 # + PersonaChangeEntry / MemoryRefEntry / ChannelEntry
      store.py
      projection.py
      manager.py
      compaction.py
      events.py
      boundary.py
      orchestration.py
      hub.py                     # 新增：SessionHub，channel_key -> AgentSession

    persona/                     # L4a 人设（纯数据 + 渲染）
      __init__.py
      types.py
      store.py                   # ~/.moonfox/personas/*.toml
      render.py                  # Persona -> prompt section 文本
      resolver.py                # channel_key -> persona_id 规则匹配

    memory/                      # L4b 记忆
      __init__.py
      types.py
      db.py                      # SQLite 连接 / 迁移 / 单 writer
      schema.sql
      store.py                   # CRUD
      retrieval.py               # 混合检索：FTS5 + 向量（可插拔）+ 时间衰减
      embed.py                   # Embedder 协议 + NullEmbedder + HttpEmbedder + LocalEmbedder
      extractor.py               # 会话 -> 记忆候选（LLM）
      writer.py                  # MemoryWorker：异步、去重、冲突合并
      tools.py                   # memory_* AgentTool 工厂

    app/                         # L5 应用层
      __init__.py
      prompt.py                  # build_prompt_sections（+ persona / memory section）
      wire.py                    # SessionEvent -> JSON（WebUI 与外部消费者共用）
      output_guard.py
      tools/
        __init__.py
        fs.py                    # read / write / edit / ls / find / grep
        shell.py                 # bash / powershell
        web.py                   # fetch / search（可选）
        registry.py              # 按 surface 组装工具集
      scheduler.py               # 定时任务（主动搭话、记忆整理、日报）
      runtime.py                 # 装配根：读配置 -> 建 hub -> 起服务

    channels/                    # L6 边界层
      __init__.py
      types.py                   # InboundMessage / OutboundSegment / ChannelCapabilities
      base.py                    # ChannelAdapter 协议
      router.py                  # 出站队列、分段、限流、重试
      qq/
        __init__.py
        driver.py                # QQDriver 协议（协议无关）
        onebot11.py              # OneBot 11 反向 WS 驱动 + ★ action 白名单
        actions.py               # OneBot action 的参数 schema（校验 + 开发参考）
        codec.py                 # CQ 码 / 消息段 编解码
        segmenter.py             # 长文本 -> 多段
        policy.py                # TriggerPolicy：@ / 前缀 / 冷却 / 并发上限
        adapter.py               # QQChannel：事件归一化 + 会话绑定 + 投递

    webui/                       # L6 边界层
      __init__.py
      server.py                  # FastAPI 应用
      events.py                  # WS 事件推送（订阅 hub）
      commands.py                # 命令 RPC（prompt / interrupt / config / memory / persona）
      auth.py                    # 本地 token
      static/
        index.html
        app.js
        styles.css

  tests/
    test_layering.py
    test_loop_continuous.py
    test_queue.py
    test_interrupt.py
    test_projection.py
    test_compaction.py
    test_persona_render.py
    test_memory_retrieval.py
    test_memory_dedup.py
    test_qq_codec.py
    test_qq_segmenter.py
    test_channel_routing.py
    test_wire.py
    test_config_layers.py
    fixtures/
      faux_sessions/
```

### 3.2 各包职责边界（一句话版）

| 包 | 负责 | 明确不负责 |
|---|---|---|
| `core` | ID、时钟、日志、路径、JSON 互转 | 任何业务概念 |
| `config` | 分层配置、密钥解析、热重载信号 | 谁来响应热重载 |
| `ai` | 一次模型调用的统一流协议 | 会话、工具、prompt |
| `agent` | 「消息 → provider → 工具 → 消息」循环 | 持久化、UI、通道 |
| `session` | 持久化、投影、压缩、run 级编排 | 具体业务（人设/记忆） |
| `persona` | 配置 → 一段 prompt 文本 | 何时注入（session 决定） |
| `memory` | 记忆的存取与检索 | 何时召回/写入（session 决定） |
| `app` | prompt 组装、内置工具、装配、定时 | 传输（HTTP/WS/OneBot） |
| `channels` | 外部平台 ↔ InboundMessage/OutboundSegment | agent 内部结构 |
| `webui` | 人看的界面 | 任何业务逻辑 |

---

## 4. Agent 核心：循环改造

这一节是全文最关键的技术改动。其余部分都是「挂载」，只有这里是「改动核心」。

### 4.1 消息队列原语（`agent/queue.py`）

mini-pi 的 `PendingQueue` 只有 `enqueue` / `peek` / `drain` / `clear`。本方案把它泛化成一个带**来源标签**的队列，并给出四个实例。

```python
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, TypeAlias

from moonfox.core.clock import now_ms

QueueMode: TypeAlias = Literal["all", "one_at_a_time"]


@dataclass(slots=True)
class QueuedMessage:
    """队列里的一条消息，带来源信息。

    为什么要包一层而不是直接放 AgentMessage：
    聊天场景需要知道「这条消息从哪来、什么时候来、谁发的」，
    以便在注入 transcript 时加上归因前缀（见 4.3）。
    """

    message: "AgentMessage"
    source: Literal["user", "steering", "follow_up", "autonomous", "extension"]
    origin: str = ""
    """来源标识：channel_key / extension path / scheduler job id。"""
    enqueued_at: int = field(default_factory=now_ms)
    coalesce_key: str | None = None
    """相同 key 的消息在队列里合并（后一条覆盖前一条）。

    用途：QQ 里用户连发 5 条消息，不该产生 5 次独立请求。
    """

    def render(self) -> "AgentMessage":
        """渲染成真正注入 transcript 的消息。

        非 user 来源会加归因前缀，让模型知道这不是「用户对我说的」。
        """
        if self.source == "user" and not self.origin:
            return self.message
        prefix = _ATTRIBUTION_PREFIX.get(self.source, "")
        if not prefix:
            return self.message
        return _prefix_message(self.message, f"{prefix}{self.origin}")


_ATTRIBUTION_PREFIX = {
    "steering": "[插话]",
    "follow_up": "[追加]",
    "autonomous": "[自发起]",
    "extension": "[系统]",
}


class MessageQueue:
    """带合并与去重的消息队列。steering / follow_up / user / autonomous 各一个实例。

    one_at_a_time：每次只投递最早一条，保证 agent 一次只回应一个话题。
    all：一次投递全部，适合「用户连发的消息合并后一起给」。
    """

    def __init__(self, mode: QueueMode = "one_at_a_time") -> None:
        self._items: deque[QueuedMessage] = deque()
        self._by_key: dict[str, QueuedMessage] = {}
        self.mode: QueueMode = mode

    def enqueue(self, item: QueuedMessage) -> None:
        if item.coalesce_key is not None:
            existing = self._by_key.get(item.coalesce_key)
            if existing is not None:
                # 覆盖内容，但保留最早的入队时间（排队顺序不变）
                existing.message = item.message
                existing.enqueued_at = min(existing.enqueued_at, item.enqueued_at)
                return
            self._by_key[item.coalesce_key] = item
        self._items.append(item)

    def has_items(self) -> bool:
        return bool(self._items)

    def peek(self) -> list[QueuedMessage]:
        if self.mode == "all":
            return list(self._items)
        return list(self._items)[:1] if self._items else []

    def drain(self) -> list[QueuedMessage]:
        drained = self.peek()
        for item in drained:
            self._items.remove(item)
            if item.coalesce_key is not None:
                self._by_key.pop(item.coalesce_key, None)
        return drained

    def clear(self) -> None:
        self._items.clear()
        self._by_key.clear()

    def __len__(self) -> int:
        return len(self._items)
```

**`coalesce_key` 的必要性**：QQ 用户习惯「我」「想问」「一个问题」分三条发。不做合并的话，agent 会看到三条独立消息，回复三次。合并键用 `f"{channel_key}:{user_id}"`，配合 `debounce_ms`（默认 800ms）在通道层等待后再入队。

### 4.2 循环改造：从「批处理」到「持续会话」

```python
@dataclass(slots=True)
class AgentLoopConfig:
    # ... mini-pi 已有字段

    # ── 循环闸门（全部来自配置，不是常量）──
    turn_budget: TurnBudget = field(default_factory=TurnBudget)
    """单次 run 的内层轮数预算与超限行为。见下方 TurnBudget。"""

    enable_autonomous: bool = False
    """是否允许 agent 自己发起新一轮。默认关闭。"""

    autonomous_budget: int = 0
    """本次 run 允许的自主轮数。每消费一次减一，减到 0 就停。

    硬约束，不是建议。所有「agent 主动说话」都必须经过它。
    同样来自配置，可被单次 run 覆盖。
    """

    get_autonomous_messages: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    """仅在 user / steering / follow_up 三类队列都空时被调用。"""
```

#### 轮数闸门：`TurnBudget` 是一个变量，不是常量

初版把 24 写死在字段默认值里，这是错的 —— 它会随场景变化（QQ 短对话 vs WebUI 长任务）、
随模型变化（慢模型要给更少轮）、随调试需要变化（排查循环问题时要能临时调到 1）。

```python
@dataclass(slots=True)
class TurnBudget:
    """单次 run 的内层轮数预算。

    三级来源，后者覆盖前者（都通过配置系统，不是硬编码）：

      1. [agent].max_turns_per_run        全局默认
      2. [agent.surface.<surface>]        按 surface 覆盖（qq / webui / cli）
      3. prompt(..., max_turns=N)         单次调用覆盖（调试用）

    为什么默认是 24：一个 turn 包含「模型响应 + 一批工具」，工具批次并行执行。
    实测典型任务 5~15 轮，24 给了 60% 余量，又能在异常时一分钟内收敛。
    但这是**默认值**，不是设计常量 —— 长任务场景调到 100 完全合理。
    """

    max_turns_per_run: int = 24
    """内层循环的硬上限。无条件生效，不受 finish_turn 影响。"""

    on_limit: Literal["end", "notify", "compact"] = "end"
    """达到上限时的行为：

      "end"     正常 emit AgentEnd，由外层 AgentSession 重入（默认）。
                对用户透明，适合长任务自然分段。
      "notify"  同上，但额外 emit 一个 Notice 事件，
                让 WebUI / QQ 知道「这轮被截断了」。
      "compact" 先跑一次压缩再结束，适合「上下文快满了又还没做完」的场景。

    三种都不会 emit 错误 —— 到达预算上限是**正常调度**，不是失败。
    """

    soft_limit_ratio: float = 0.8
    """软阈值比例。达到 max_turns_per_run * soft_limit_ratio 时
    emit 一次 Notice（只发一次），让 UI 提前显示「这一轮有点长」。

    为什么要软阈值：硬上限是保护机制，不应该让用户第一次意识到
    「轮数好多」是在它被触发的那一刻。
    """

    @classmethod
    def from_config(cls, config: "AgentConfig", surface: str) -> "TurnBudget":
        """从配置构造。surface 覆盖优先于全局默认。

        配置热重载时重新构造即可生效 —— 不需要重启会话。
        正在跑的 run 用的是构造时的快照，下一个 run 用新值。
        这是刻意的：run 中途换预算会让行为不可复现。
        """
        surface_cfg = config.surface.get(surface, {})
        return cls(
            max_turns_per_run=surface_cfg.get("max_turns_per_run", config.max_turns_per_run),
            on_limit=surface_cfg.get("on_limit", config.on_limit),
            soft_limit_ratio=surface_cfg.get("soft_limit_ratio", config.soft_limit_ratio),
        )
```

对应的配置（§9.2 的 `[agent]` 节）：

```toml
[agent]
# 内层循环轮数预算。改这里即可，不需要动代码。
max_turns_per_run = 24
on_limit = "end"                 # end | notify | compact
soft_limit_ratio = 0.8

# 按 surface 覆盖 —— 聊天短对话收紧，WebUI 长任务放宽
[agent.surface.qq]
max_turns_per_run = 12           # QQ 场景：用户等不了太久
on_limit = "notify"              # 且要告诉用户「还在想」

[agent.surface.webui]
max_turns_per_run = 100          # 长任务：允许充分展开
on_limit = "compact"             # 且先压缩，避免撑爆上下文

[agent.surface.cli]
max_turns_per_run = 1            # 调试：一轮一停，方便逐步观察
on_limit = "end"
```

**运行时也可改**（不必改文件）：

| 途径 | 作用范围 | 说明 |
|---|---|---|
| WebUI `config.set agent.max_turns_per_run 50` | 全局，下一个 run 起效 | 写入 `overrides.toml` |
| WebUI `session.set_turns <key> 5` | 单会话，下一个 run 起效 | 存在会话内存，不持久化 |
| `session.prompt(text, max_turns=1)` | 单次 run | 调试用，不落盘 |
| 配置热重载 | 所有会话的下一个 run | 正在跑的 run 用旧值（保证可复现） |

改造后的主循环（**只展示与 mini-pi 不同的部分**，其余结构保持一致）：

```python
async def _run_loop(current, new_messages, config, emit, signal, stream_fn) -> None:
    last_turn: TurnContext | None = None
    explicit_continue = False
    turns_this_run = 0
    budget = config.autonomous_budget if config.enable_autonomous else 0
    tb = config.turn_budget  # 轮数预算（来自配置）
    soft_notified = False

    pending = await _drain_steering(config)

    while True:  # 外层：follow-up / autonomous
        has_more_tools = True

        while has_more_tools or pending:  # 内层：tools + steering
            # ── 轮数闸门（全部来自配置）──
            if not soft_notified and turns_this_run >= tb.soft_limit_ratio * tb.max_turns_per_run:
                soft_notified = True
                await emit(Notice(level="info", message=f"这一轮已经跑了 {turns_this_run} 轮"))
            turns_this_run += 1
            if turns_this_run > tb.max_turns_per_run:
                # 到达预算上限是正常调度，不是失败 —— 不 emit 错误
                if tb.on_limit == "notify":
                    await emit(Notice(level="info", message="这轮先到这里，我接着处理"))
                elif tb.on_limit == "compact":
                    await _run_auto_compaction(reason="turn_limit")
                await emit(AgentEnd(new_messages))  # 外层 AgentSession 会重入
                return

            prepared: list[AgentMessage] = []
            if last_turn is not None:
                update = (
                    await config.prepare_next_turn(last_turn) if config.prepare_next_turn else None
                )
                if update is not None:
                    current = update.context or current
                    prepared = update.messages or []
                    config = config.with_model_thinking(update)
                if not pending:
                    pending = await _drain_steering(config)
                await emit(TurnStart())

            # ... 声明工具增量 / prepare_request / 流式响应（同 mini-pi）

            # ── 硬出口：错误 ──
            if message.stop_reason == "error":
                last_turn = TurnContext(message, [], current, new_messages)
                if config.finish_turn:
                    await config.finish_turn(last_turn, signal)
                await emit(TurnEnd(message, []))
                await emit(AgentEnd(new_messages))
                return

            # ── 改动：中断分级 ──
            if message.stop_reason == "aborted":
                if signal is not None and signal.hard_aborted:
                    last_turn = TurnContext(message, [], current, new_messages)
                    await emit(TurnEnd(message, []))
                    await emit(AgentEnd(new_messages))
                    return
                # 软中断：当作普通 turn 结束，继续循环消费队列
                last_turn = TurnContext(message, [], current, new_messages)
                if config.finish_turn:
                    await config.finish_turn(last_turn, signal)
                await emit(TurnEnd(message, []))
                pending = await _drain_steering(config)
                continue

            # ... 工具执行（同 mini-pi）
            # ... finish_turn / TurnEnd（同 mini-pi）

        # ── 内层退出：查 follow-up ──
        follow_ups = await _drain_follow_up(config)
        if follow_ups:
            explicit_continue = False
            pending = follow_ups
            continue

        # ── 改动：查 user 队列（持续会话的关键）──
        user_messages = await _drain_user(config)
        if user_messages:
            explicit_continue = False
            pending = user_messages
            continue

        # ── 新增：查自主队列（有预算才查）──
        if budget > 0 and config.get_autonomous_messages is not None:
            autonomous = await config.get_autonomous_messages()
            if autonomous:
                budget -= 1
                explicit_continue = False
                pending = autonomous
                continue

        # 没有自然请求满足 continue 决策，补一次 context-only 请求
        if explicit_continue:
            explicit_continue = False
            continue

        break

    await emit(AgentEnd(new_messages))
```

#### 为什么用「内层闸门 + 外层重入」而不是「内层无限循环」

| 方案 | 问题 |
|---|---|
| 内层无限循环，队列空才退出 | 压缩永远没机会跑（压缩挂在 `prepare_next_turn`，但阈值检查需要 run 边界）；`finish_turn` 写错会无限循环；一个 run 的时长不可控，WebUI 无法显示「这一轮结束了」 |
| 内层固定 1 轮，全靠外层重入 | `prepare_next_turn` 每轮都跑，压缩检查过频；每次重入都要重建 `AbortToken` 与订阅，开销大；`explicit_continue` 的语义会被拆散到两处 |
| **内层有界（默认 24）+ 外层重入** | 兼顾：正常对话（1~3 轮）在单次 run 内完成，不重入；长任务（> 24 轮）自然分段，每段之间压缩/重试/边界有机会跑 |

24 这个数不是拍脑袋：一个 turn 包含「模型响应 + 一批工具」，工具批次并行执行，实测（pi 的 coding agent）典型任务是 5~15 轮。24 给了 60% 余量，又能在异常时 1 分钟内收敛。

### 4.3 归因前缀：让模型分得清「谁在说话」

聊天机器人与编程 agent 最大的上下文差异是**多说话人**。QQ 群里可能是 10 个人在同一个会话里说话。

不做处理的话，模型会把所有人的话当成「用户对我说的」，回复时人称会乱。

```python
def _prefix_message(message: AgentMessage, prefix: str) -> AgentMessage:
    """给消息加归因前缀。

    只改 content，不改 role。用 user role 而不是 system：
    system message 在 transcript 里是「prompt 的一部分」，会被重放机制影响；
    而说话人归因是**对话内容**，必须留在 user 消息里。
    """
    if isinstance(message, ai.UserMessage):
        content = message.content
        if isinstance(content, str):
            return replace(message, content=f"{prefix} {content}")
        return replace(message, content=[ai.TextContent(f"{prefix} "), *content])
    return message


# 实际渲染效果（注入 transcript 的文本）：
#   [QQ 群 123456 · 张三(10001)] 今天几点开会
#   [插话 · 张三(10001)] 算了别查了
#   [自发起] 上次你说要提醒我周三交报告，今天就是周三
```

**为什么不用 `role="system"` 承载归因**：system message 会进入 prompt 重放链（接缝 2），会破坏 `diff_prompt_sections` 的语义，也会让 provider 的缓存前缀每次说话人都变。归因是**内容**，必须留在内容里。

### 4.4 中断三级：完整契约

```python
class AbortToken:
    """取消令牌。在 mini-pi 基础上区分「软」与「硬」。

    为什么不做成两个 token：钩子和工具只接收一个 signal 参数，
    改成两个会污染所有签名。用内部标志位区分语义，外部只看到 aborted。
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._hard = False

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    @property
    def hard_aborted(self) -> bool:
        """True 表示整个 run 要停；False 表示只是打断当前这一轮。"""
        return self._hard

    def interrupt(self) -> None:
        """软中断。"""
        self._hard = False
        self._event.set()

    def abort(self) -> None:
        """硬中断。"""
        self._hard = True
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def reset(self) -> None:
        """软中断后复用：清掉标记，但保留 hard 语义不变。"""
        self._event.clear()
```

| 触发 | 效果 | 用于 |
|---|---|---|
| `interrupt()` | 当前流式响应 `stop_reason="aborted"`；未开始的工具不执行；**循环继续**消费队列 | QQ 里「算了」「停」「别说了」；WebUI 的停止按钮（软） |
| `abort()` | 当前 run 立即结束，`agent_end` 后不再重入 | WebUI 的「中止任务」；关闭某个会话 |
| `shutdown()` | 所有会话 abort + 等 grace_ms + 关闭 SQLite/WS | 进程退出 |

**软中断必须保留已完成的工具结果**。用户在 agent 执行了 3 个工具后说「算了」，这 3 个结果仍然有效，不该回滚。

### 4.5 `finish_turn` 与持续会话的相互作用

这是最容易写出死循环的地方。三个规则必须同时成立：

1. `finish_turn` 返回 `"continue"` 时，若队列已有内容，**不额外发请求**（mini-pi 已有此语义）
2. `turn_budget.max_turns_per_run` 是**无条件**闸门，不受 `finish_turn` 影响。
   它是配置项（§4.2），但**一旦本次 run 开始就不再变化** —— 运行中途改配置
   会让行为不可复现，所以 run 用的是构造时的快照，新值从下一个 run 起效
3. `finish_turn` 返回 `"end"` 时立即结束，**队列内容保留**到下一次 run

规则 3 补充说明：QQ 场景下「结束」通常意味着「这件事做完了」，但用户可能已经在打字。保留队列而不是清空，避免消息丢失。下一次 `prompt()` 时会先 drain 队列。

```python
async def prompt(self, message, images=None) -> None:
    """开始一次新 prompt。运行中调用会抛错（同 mini-pi）。"""
    ...
    # 新增：如果队列里还有上次残留的 user 消息，先合并进来
    leftover = self._user_queue.drain()
    ...
```

---

## 5. 会话层：多会话与通道绑定

### 5.1 会话粒度决定

**`channel_key` → 一个 `AgentSession`。**

```python
def channel_key(event: InboundMessage, self_id: str) -> str:
    """channel_key 必须包含机器人自己的账号（self_id）。

    ★ 为什么：SnowLuma 支持多账号并行运行（各账号独立维护会话、
      身份映射、消息存储和网络配置）。如果 key 里没有 self_id，
      机器人 A 和机器人 B 各自与同一用户的私聊会被路由到**同一个会话** ——
      人设、记忆、上下文全部串号。

    单账号时 self_id 是个常量，多出来的几个字符没有成本；
    多账号时它是正确性的必要条件。这是一个「不加会后悔」的字段，
    和 session entry 的 id + parent_id 是同一类决定。
    """
    match event.conversation:
        case PrivateConversation(user_id):
            return f"qq:{self_id}:private:{user_id}"
        case GroupConversation(group_id, user_id):
            if config.qq.group_mode == "shared":
                return f"qq:{self_id}:group:{group_id}"
            return f"qq:{self_id}:group:{group_id}:user:{user_id}"
    return f"{event.channel}:{self_id}:{event.conversation.key()}"
```

**`self_id` 从哪来**：OneBot 反向 WS 的连接请求头 `X-Self-ID`（机器人 QQ 号）。驱动层在建立连接时就拿到，缓存在 `QQDriver.self_id`；若为空则回退到 `get_login_info` 的返回值。多账号时每个连接一个 self_id，各自对应一个 `QQChannel` 实例。

| 模式 | 粒度 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| `shared` | 一个群一个会话 | 群里所有人共享上下文，像「群里有个 AI」 | 10 个话题会互相污染；上下文膨胀快 | 小群、熟人群 |
| `per_user` | 群里每人一个会话 | 话题隔离；记忆归属清晰 | 看不到群聊上下文；群里两人同时问，AI 回复会串（需要用「回复谁」的引用来区分） | 大群、公开群 |
| `threaded`（后补） | 按回复链 / @ 分线程 | 最接近人类群聊 | 实现复杂，需要消息引用链分析 | 未来 |

默认 `per_user`（更安全，不会把 A 的私事说给 B 听），配置可改。

**群聊发言触发**：群里每条消息都响应会很吵。触发条件可配置：

```toml
[qq.group]
mode = "per_user"
trigger = ["at", "reply_to_bot", "prefix"]   # 默认：@机器人 / 回复机器人 / 以触发词开头
prefixes = ["小狐狸", "moonfox", "狐狸"]
cooldown_ms = 3000          # 同一用户 3 秒内只响应一次
max_concurrent_turns = 2    # 同一群最多 2 个并发 run
ignore_bots = true
```

### 5.2 `SessionHub`：会话生命周期管理

```python
class SessionHub:
    """channel_key -> AgentSession 的注册表与生命周期管理。

    这是 channels 层唯一接触 session 层的入口。
    channels 不 import AgentSession 的内部结构，只调 hub 的四个方法。
    """

    def __init__(
        self,
        *,
        runtime: "Runtime",  # 提供 model / stream_fn / tools / persona / memory
        sessions_dir: Path,
        max_idle_sessions: int = 64,
    ) -> None: ...

    async def get_or_create(self, key: str, *, persona_id: str | None = None) -> "AgentSession":
        """按 key 取会话；不存在则创建（新建 JSONL + 建 Agent + 装配钩子）。

        并发安全：同一个 key 的并发创建会共享同一个 future，
        不会产生两个 AgentSession 写同一个 JSONL。
        """

    async def deliver(self, key: str, item: QueuedMessage) -> None:
        """向会话投递消息。会话空闲则触发 prompt；运行中则按 source 入对应队列。"""

    async def interrupt(self, key: str) -> bool: ...
    async def abort(self, key: str) -> bool: ...

    def get(self, key: str) -> "AgentSession | None": ...
    def list_active(self) -> list[SessionInfo]: ...
    def subscribe(self, listener: Callable[[str, SessionEvent], None]) -> Callable[[], None]:
        """聚合所有会话的事件，带 key 前缀。WebUI 与日志用这个。"""

    async def evict_idle(self) -> int:
        """把空闲超过阈值的会话从内存卸载（JSONL 保留，下次按需恢复）。

        为什么需要：QQ 群里可能有 500 个用户各自有会话，
        全部常驻内存会撑爆。卸载后恢复成本 = 读 JSONL + 重建投影，毫秒级。
        """
```

```python
@dataclass(slots=True)
class SessionInfo:
    """给 WebUI 用的会话摘要（不含消息内容）。"""
    key: str
    session_id: str
    surface: Literal["webui", "qq"]
    display_name: str
    persona_id: str
    model: str
    is_streaming: bool
autonomous_enabled = false
autonomous_budget_per_run = 0
autonomous_min_interval_ms = 300000

# 按 surface 覆盖轮数预算
[agent.surface.qq]
max_turns_per_run = 12           # 聊天场景：用户等不了太久
on_limit = "notify"

[agent.surface.webui]
max_turns_per_run = 100          # 长任务：允许充分展开
on_limit = "compact"

[agent.surface.cli]
max_turns_per_run = 1            # 调试：一轮一停

[agent.retry]
enabled = true
max_retries = 3
base_delay_ms = 1000
max_delay_ms = 30000

# ── 记忆 ──
[memory]
enabled = true
extract_on_settled = true
extract_debounce_ms = 5000
top_k = 8
min_score = 0.25
max_section_chars = 2000
half_life_days = 30
embedder = "none"                # none | http | local
[memory.weights]
fts = 0.45
vector = 0.20
importance = 0.15
recency = 0.15
scope = 0.05

# ── 人设 ──
[persona]
default = "moonfox"
[persona.routing.rules]
"qq:private:*" = "moonfox-intimate"

# ── WebUI ──
[webui]
enabled = true
host = "127.0.0.1"
port = 8787
auth_token_env = "MOONFOX_WEBUI_TOKEN"
allow_remote = false             # 非本机访问需要显式开启

# ── QQ ──
[qq]
enabled = true
driver = "onebot11"
protocol_endpoint = "snowluma"   # snowluma | napcat | llonebot | lagrange | auto
                                 # 仅用于 WebUI 展示与故障排查提示，不参与逻辑

[qq.onebot11]
mode = "reverse_ws"              # reverse_ws | forward_ws | http
ws_host = "127.0.0.1"
ws_port = 6700
ws_path = "/onebot/v11/ws"
access_token_env = "ONEBOT_ACCESS_TOKEN"
call_timeout_s = 30              # 单次 action 调用的超时（协议端可能吞请求）
offline_queue_ttl_ms = 300000    # 断线期间出站队列的保留时长，超时丢弃
heartbeat_miss_limit = 3         # 连续丢失多少个心跳视为断开
allow_dangerous = false          # ★ 是否放开 set_group_ban / kick 等危险 action

# 多账号：每个账号一个 self_id，各自独立会话/身份映射/记忆
[[qq.accounts]]
uin = "10001"
enabled = true
persona = "moonfox"

# [[qq.accounts]]
# uin = "10002"
# enabled = false
# persona = "moonfox-alt"

[qq.group]
mode = "per_user"
trigger = ["at", "reply_to_bot", "prefix"]
prefixes = ["小狐狸", "moonfox"]
cooldown_ms = 3000
max_concurrent_turns = 2
ignore_bots = true
debounce_ms = 800                # 合并连发消息

[qq.private]
enabled = true
cooldown_ms = 1000

[qq.outbound]
max_segment_chars = 1200         # 单条消息上限，超长自动分段
segment_delay_ms = 400           # 分段之间延迟，模拟打字
max_segments = 5                 # 超过则截断并提示
typing_indicator = true
retry_on_fail = 2

# ── 工具 ──
[tools]
enabled = ["memory_search", "memory_remember", "fetch"]
disabled = ["bash", "write", "edit"]   # 聊天场景默认禁掉危险工具
```

### 9.3 密钥解析

优先级：`api_key_env` 指定的环境变量 > `~/.moonfox/secrets.toml` > 配置内联（不推荐）。

**绝不允许**：密钥出现在 `overrides.toml`（WebUI 写入的文件）里，也绝不出现在 `config.get` 的响应里（脱敏成 `sk-***abc`）。

### 9.4 热重载

| 配置项 | 热重载行为 |
|---|---|
| `log_level` | 立即生效 |
| `model.*` | 立即生效（下一个 turn 用新模型） |
| `persona.*` | 立即生效（下一个 turn 的 prompt section 变化） |
| `memory.*` | 权重/阈值立即生效；`enabled` 切换需要重载会话 |
| `qq.*` | 需要重连：先建新连接，成功后再关旧连接 |
| `webui.host/port` | 需要重启进程，WebUI 提示用户 |
| `agent.*`（含 `max_turns_per_run` / `on_limit` / `agent.surface.*`） | 立即生效（**下一个 run**；正在跑的 run 用构造时快照，保证可复现） |

实现：`config/watcher.py` 监听文件 mtime，变化时重新加载 + 计算 diff + 发布 `ConfigChanged` 事件。`runtime` 订阅该事件并应用。

**`qq.*` 的重连策略**：先建新连接，验证可用后再关旧连接（避免「重连期间消息丢失」）。如果新连接 5 秒内没建立，回滚并保留旧连接，报告错误。

---

## 10. QQ 通道

### 10.1 接入方案选型：三条路线的对比与结论

QQ 接入有三条候选路线。先把它们画清楚，因为三者的**架构位置完全不同**，不是「同一件事的三种做法」。

```
路线 A：直接对接协议端
  QQ ──NTQQ──► 协议端(NapCat/SnowLuma) ──OneBot 11 WS──► moonfox 自己写的 OneBot 客户端
                                                          └─ QQChannel → SessionHub

路线 B：协议端 + SDK / MCP
  QQ ──NTQQ──► 协议端 ──OneBot HTTP──► @snowluma/mcp ──MCP──► moonfox 作为 MCP client
                       └─ @snowluma/sdk (TypeScript)  ← 另一条路，需 Node 进程

路线 C：协议端 + NoneBot2
  QQ ──NTQQ──► 协议端 ──OneBot 11──► NoneBot2(nonebot-adapter-onebot)
                                        ├─ 插件/匹配器/权限
                                        └─ 某个 matcher 里调用 moonfox AgentSession
```

#### 结论先行

> **选路线 A：直接用协议端 + 自己写一个薄的 OneBot 11 客户端。**
>
> 但要从 B 借两样东西（MCP 的 action 目录作为**开发期文档**、可选地给外部 agent 用），
> 从 C 借一样东西（权限模型的**概念**，不是代码）。

理由是三条路线在架构上的位置不同：

| | 路线 A | 路线 B | 路线 C |
|---|---|---|---|
| 谁拥有 asyncio 事件循环 | **moonfox** | moonfox | **NoneBot2** |
| 事件到达路径 | WS 推送 → 直接入队 | **不通**（MCP 是模型发起的请求，无推送原语） | WS 推送 → NoneBot 匹配器 |
| 出站消息路径 | 直接 call API | 模型调 tool → MCP → HTTP → OneBot | matcher 里调 API |
| 会话状态归属 | **moonfox** | moonfox | **NoneBot2 + moonfox 两份** |
| 新增进程/语言 | 无 | Node（SDK 路线）或 Node（MCP 路线） | 无（纯 Python） |
| 新增依赖 | websockets（已有） | @snowluma/mcp（Node） | nonebot2 + adapter-onebot + yarl + anyio + loguru + pygtrie + python-dotenv |
| 与 moonfox 编排的关系 | 原生 | 原生 | **两套编排打架** |

#### 为什么路线 B（SDK / MCP）不合适

**B1：TypeScript SDK —— 语言不匹配。**

`@snowluma/sdk` 是 TypeScript 包。moonfox 是 Python。要用它只有两条路：

1. 起一个 Node.js sidecar 进程，Python 与它 IPC —— 这直接违背 §1.4「单进程」的决定，而且引入一个**只为转发消息**的进程
2. 放弃，自己写 OneBot 客户端

我们真正需要 SDK 里的什么？看它的能力清单：HTTP client、WS client、事件中间件、命令匹配、Message Builder。其中 **Message Builder（CQ 码/消息段互转）我们本来就要自己写**（§10.2 的 `codec.py`，因为内部类型是 `OutboundSegment` 不是它的 `message.text()`）。而事件中间件和命令匹配，我们的 `QQChannel` + `SessionHub` 已经覆盖且更贴合自己的模型。

**结论**：SDK 的价值在「TypeScript 项目里写 QQ 机器人」。我们不是。

**B2：MCP —— 方向反了。**

这是最容易被「看起来很先进」误导的一条。`@snowluma/mcp` 的设计目标是：**让 LLM 客户端（Claude Desktop、Cline）通过 MCP 调用 OneBot 动作**。它的两个执行工具是 `query_action`（只读）和 `invoke_action`（任意写操作）。

问题在于 **MCP 没有「事件推送」原语**。MCP 的 tools 是**模型主动调用**的；QQ 消息进来是**外部事件驱动**的。这两件事的方向相反：

```
MCP 能做的：  模型 ──调用──► invoke_action("send_group_msg")  ──► QQ
我们需要做的： QQ ──消息──► 唤醒 agent ──► 生成回复 ──► 发回 QQ
              ↑ 这一段 MCP 表达不了
```

就算硬用 MCP 发送，还有三个具体问题：

1. **爆炸半径**：`invoke_action` 能调 OneBot 的**任意** action，包括 `set_group_ban`（禁言）、`set_group_kick`（踢人）、`delete_friend`、`set_group_card`。模型一旦拿到这个工具，一次幻觉就能把群主禁言了。而我们的 `before_tool_call` fail-closed 钩子（§11.2）是为**明确的、枚举的工具**设计的，对「一个能调任意 action 的万能工具」无从下手。
2. **prompt 污染**：SnowLuma 的 action 目录有几十个。全塞进工具列表，每次请求都要付 token，且会诱导模型去用。
3. **多一跳**：tool call → MCP server → HTTP → OneBot。而路线 A 是直接 call。对聊天机器人，延迟是体验的一部分。

**MCP 真正有价值的地方**（保留，但不作主路径）：

| 用法 | 价值 | 采用 |
|---|---|---|
| `@snowluma/mcp` **docs 模式**（只读）作为**开发期参考** | 查 action 的参数与跨字段约束，不用翻文档 | ✓ 开发时用，不进运行时 |
| 把 moonfox 的 QQ 能力**暴露成 MCP server**，让其他 agent 用 | 反向：别人调我们 | 可选，P12 之后 |
| moonfox 作为 MCP **client** 消费外部工具（文件系统、搜索） | 与 QQ 无关，是 agent 工具面的扩展 | ✓ 独立课题，见 §11.3 |

注意第三条与 QQ 无关 —— 那是「moonfox 的工具生态」，不该和 QQ 接入混在一起谈。

#### 为什么路线 C（NoneBot2）不合适

NoneBot2 是好框架（7.7k stars、MIT、纯 Python、活跃）。它提供的三件事与我们的重叠度需要逐条看：

| NoneBot2 提供 | 我们已有的 | 结论 |
|---|---|---|
| **事件监听** | `QQChannel` 订阅 OneBot 事件 → `SessionHub` | 重叠。且我们的下游是 `AgentSession`，不是 matcher |
| **会话管理** | `SessionHub` + `SessionStore` + 投影 + 压缩 | **完全不重叠**。NoneBot 的 session 是 per-user/per-group 的键值存储；我们的是带压缩/分支/记忆的完整 agent 会话。两者层级不同 |
| **权限控制** | `before_tool_call` fail-closed 钩子 + `PermissionPolicy` | 部分重叠。**NoneBot 的 Permission/Rule 模型值得借鉴概念**，但不需要它的代码 |
| 插件生态（几百个插件） | 不需要 | 我们是单体应用，不是插件平台 |
| 多平台适配器（TG/Discord/Kook…） | 暂不需要 | 见下方「什么时候 C 才对」 |
| 依赖注入 / `Depends` | 不需要 | |

真正的阻碍是**控制权**：

```python
# NoneBot2 的启动方式是它拥有事件循环：
nonebot.init()
nonebot.load_adapters("nonebot.adapters.onebot.v11")
nonebot.load_plugins("plugins")
nonebot.run()  # ← 它跑了 loop，我们的 Runtime.start() 无处安放


# 要用 moonfox，只能反过来：在某个 matcher 里驱动 AgentSession
@on_message()
async def _(bot: Bot, event: GroupMessageEvent):
    session = await hub.get_or_create(key)
    await session.prompt(event.get_plaintext())  # ← 但 session.prompt 会跑一整个 run
```

这个写法的问题：

1. **两套编排打架**。NoneBot 的 matcher 生命周期（`got`/`receive`/`finish`）与我们 `AgentSession` 的 run 循环（`_handle_post_run` / 边界 / 压缩 / 重试）是两套独立的控制流。谁管「这个会话现在忙不忙」？谁管「消息排队」？我们的 `MessageQueue` 四类队列是为持续会话设计的，NoneBot 的 session 完全不知道它们存在。
2. **配置双轨**。NoneBot 用 `.env` + `pyproject.toml`，我们用分层 TOML。两套配置系统意味着「WebUI 改配置」要改两处。
3. **依赖树膨胀**。`nonebot2` 拖进 `yarl`、`anyio`、`loguru`、`pygtrie`、`python-dotenv`、`exceptiongroup`。其中 `anyio` 会引入第二套异步抽象（anyio 的 task group / cancel scope 与 asyncio 原生语义有差异），而我们整个 `AbortToken` 设计（§4.4）是建立在「不用 `asyncio.CancelledError`」之上的。
4. **日志双轨**。NoneBot 用 loguru，我们用 `core/logging.py`。§8.5 已经明确「日志走独立 logger，不要写进事件通道」，两套 logger 会让这条规则难以维持。
5. **收益是负的**。我们需要的三件事里，一件重叠、一件层级不同、一件只要概念。为了这三件事付出「放弃事件循环控制权」的代价，不划算。

**什么时候 C 才是对的**：如果你打算做的是**多平台**机器人（QQ + Telegram + Discord + 飞书），且核心逻辑是「命令-响应」而非「持续对话的 agent」，那 NoneBot2 的适配器生态是巨大的杠杆。但本项目的定位是「个人 agent 的 QQ 入口」，不是「多平台机器人框架」。

#### 三条路线的成本估算

| | 路线 A | 路线 B（SDK） | 路线 B（MCP） | 路线 C |
|---|---|---|---|---|
| 首次可用工作量 | **3~5 天** | 5~8 天 + Node 进程 | 4~6 天 | 6~10 天 + 重构编排 |
| 长期维护面 | OneBot 11 规范（稳定） | 规范 + npm 包 + Node 运行时 | 规范 + MCP 规范 + npm 包 | 规范 + NoneBot2 版本 + 适配器版本 + 插件兼容 |
| 出错时的调试深度 | 全链路自己写的，可打断点 | 跨语言，只能看日志 | 跨语言 + 跨进程 | 需要在 NoneBot 生命周期里找位置 |
| 换协议端的成本 | **0**（都讲 OneBot 11） | 0 | 0 | 0（adapter 层） |

#### 协议端选谁：NapCat 还是 SnowLuma？

既然选了路线 A，**这个选择对我们零成本且可随时切换** —— 因为两者都讲 OneBot 11，我们的 `QQDriver` 只认协议不认实现（§10.1 末的驱动抽象）。

| | NapCat | SnowLuma |
|---|---|---|
| stars / 活跃度 | 10900 / 每日提交 | 1533 / 每日提交 |
| 语言 | TypeScript | TypeScript |
| 许可 | 混合协议；**禁止未经授权基于其代码开发** | **源码可见非商业许可**；商用/公开衍生需书面授权 |
| 自带 WebUI | 有 | 有（:5099） |
| 多账号 | 支持 | 支持（各账号独立会话/身份映射/存储） |
| 额外能力 | — | 自带 TypeScript SDK + MCP server |
| 社区 | 大 | 小（2025-06 建仓） |
| 引用消息预览 | — | **会把本地缓存的原文附入 QQ 引用预览**（细节体验更好） |

**选定：SnowLuma**（对接 QQ 稳定性好）。NapCat 保留为可切换备选。

**注意许可**：两者都不是 OSI 开源（SnowLuma 是源码可见非商业许可）。但因为我们**只通过网络讲 OneBot 协议、不派生它们的代码**，许可约束不传染到 moonfox。这是「选协议而非选 SDK」的另一个隐性收益 —— **OneBot 协议边界同时是一道许可防火墙**。如果将来要商用 moonfox，不需要为协议端许可买单（但仍需自行部署协议端并遵守其条款）。

#### 选定 SnowLuma 后必须处理的四件事

**S1：Node.js 运行时成为部署依赖。**

SnowLuma 是 TypeScript 项目，Lite 版需要 **Node.js 22.13+（23 系需 23.4+）**。本机 node v24.18.1 ✓。

部署形态因此变成两个进程：

```
┌─ 进程 1：SnowLuma（Node.js）───────────────┐
│  · QQ 登录与 NTQQ 协议                     │
│  · 自带 WebUI（http://localhost:5099）     │
│  · config/onebot_<uin>.json 定义 OneBot 端口与 token │
└───────────────┬────────────────────────────┘
                │ OneBot 11 反向 WS（我们做服务端）
┌───────────────▼────────────────────────────┐
│  进程 2：moonfox（Python）                  │
│  · OneBot11Driver 监听 ws_port             │
│  · AgentSession / WebUI / 记忆             │
└────────────────────────────────────────────┘
```

**为什么 SnowLuma 用反向 WS 连我们**：SnowLuma 作为客户端主动连 moonfox 的 `ws_port`，我们不需要暴露端口给外部，也不需要知道 SnowLuma 在哪。启动顺序也因此无所谓 —— SnowLuma 先起还是 moonfox 先起都行，谁后起谁重连。

**S2：两个 WebUI 必须区分清楚。**

SnowLuma 自带 WebUI（:5099，管账号状态、连接配置、动作调试、存储）与 moonfox 的 WebUI（管会话、人设、记忆、配置）职责完全不同。不区分清楚用户会困惑「我该在哪改配置」。

| 界面 | 端口 | 管什么 | 什么时候用 |
|---|---|---|---|
| SnowLuma WebUI | 5099 | QQ 登录、协议端连接、OneBot 端口/token、动作调试 | 首次接入 QQ、QQ 掉线、调试 action |
| moonfox WebUI | 8787 | 会话、人设、记忆、模型、配置、用量 | 日常使用 |

moonfox WebUI 的「通道状态」页放一个指向 SnowLuma WebUI 的链接 + 一份「连不上 QQ 时按这个顺序排查」的清单（§10.7）。

**S3：SnowLuma 支持多账号 —— 这暴露了一个必须现在修的缺陷。**

SnowLuma README 明确写「多账号运行：各账号独立维护会话、身份映射、消息存储和网络配置」。而我们原来的 `channel_key` **没有包含机器人自己的账号**：

```python
# ✗ 原设计：两个 QQ 号收到同一个人的私聊 → 路由到同一个会话
def channel_key(event):
    return f"qq:private:{user_id}"
```

这会串号：机器人 A 和机器人 B 各自与同一用户的对话会混成一个会话，人设、记忆、上下文全串。**必须修**，见 §5.1。

**S4：SnowLuma 的引用消息预览可以白用。**

SnowLuma 在发送 `reply` 段时，会把**本地消息缓存中的原文**附入 QQ 引用预览（保留文字、换行与引用关系；图片语音等以类型提示显示）。这意味着我们的 `OutboundSegment(type="reply", data={"message_id": ...})` 在 QQ 侧会显示成完整的引用卡片，**不需要我们自己做任何事**。

设计上利用这一点：出站时如果这一轮是对某条消息的直接回应，带上 `reply` 段，引用关系在 QQ 里就自然成立了。

```toml
[qq.onebot11]
# 协议端无关：NapCat / SnowLuma / LLOneBot / Lagrange 都讲这个协议
mode = "reverse_ws"
ws_host = "127.0.0.1"
ws_port = 6700
ws_path = "/onebot/v11/ws"
access_token_env = "ONEBOT_ACCESS_TOKEN"

# 仅用于 WebUI 展示与故障排查提示，不参与逻辑
driver_hint = "napcat"          # napcat | snowluma | llonebot | lagrange | auto
```

#### 从 B / C 借什么（具体清单）

不是全盘否定，而是**取其概念、弃其实现**：

| 来源 | 借什么 | 怎么借 |
|---|---|---|
| B（SnowLuma MCP docs 模式） | OneBot action 的参数与跨字段约束 | 开发时作为参考资料；把常用 action 的 schema 抄进 `channels/qq/actions.py` 做参数校验 |
| B（MCP 思路） | 「能力目录」的组织方式 | 我们的 `QQDriver.call()` 加一个 action 白名单，只有枚举内的 action 可被调用（见下） |
| C（NoneBot2 Permission/Rule） | 权限模型的概念：**规则可组合、可复用、按事件类型挂载** | 把它映射成 `before_tool_call` 的 `PermissionPolicy`（§11.2）；QQ 侧加一个 `TriggerPolicy` |
| C（NoneBot2 多适配器） | 「通道适配器」的接口设计 | 我们的 `ChannelAdapter` 协议（§10.1 末）已经是这个形状 |

**从 B 借来的 action 白名单**（这是对 MCP 爆炸半径问题的正面回应）：

```python
class OneBot11Driver:
    """OneBot 11 驱动。

    ★ 关键安全设计：只暴露白名单内的 action。
    即使模型通过某种方式构造出任意 action 名，也调不出去。
    这直接回应了「MCP invoke_action 能调任意 action」的爆炸半径问题。
    """

    ALLOWED_ACTIONS: frozenset[str] = frozenset(
        {
            # 发送
            "send_private_msg",
            "send_group_msg",
            "send_msg",
            "delete_msg",  # 撤回自己的消息
            # 查询
            "get_login_info",
            "get_status",
            "get_version_info",
            "get_friend_list",
            "get_group_list",
            "get_group_member_info",
            "get_group_member_list",
            "get_msg",
            # 媒体
            "get_image",
            "get_record",
            # 交互
            "set_msg_emoji_like",  # 贴表情（拟人化）
            "mark_msg_as_read",
        }
    )

    # 明确禁止，需要时在配置里显式放开（默认全部关闭）
    DANGEROUS_ACTIONS: frozenset[str] = frozenset(
        {
            "set_group_ban",
            "set_group_kick",
            "set_group_whole_ban",
            "set_group_leave",
            "set_group_card",
            "set_group_admin",
            "delete_friend",
            "set_friend_add_request",
            "set_group_add_request",
            "set_restart",
            "clean_cache",
        }
    )

    async def call(self, action: str, params: dict) -> dict:
        if action not in self.ALLOWED_ACTIONS:
            if action in self.DANGEROUS_ACTIONS and not self._config.allow_dangerous:
                raise PermissionError(
                    f"Action {action} is dangerous and disabled. "
                    f"Enable qq.onebot11.allow_dangerous to use it."
                )
            raise PermissionError(f"Action {action} is not in the allowlist")
        return await self._call_raw(action, params)
```

**为什么白名单比「让模型自己决定」好**：聊天机器人的正常需求是「发消息、查信息、撤回自己的消息」，这个集合是**封闭的、可枚举的**。而 OneBot 的完整 action 集包含管理操作。用白名单把「能力面」收窄到实际需要，是比任何权限提示词都可靠的约束 —— 这是**结构性安全**，不是**提示词安全**。

#### 最终架构（路线 A + 借用）

```
QQ 客户端
  │ NTQQ 协议（协议端自己处理逆向与风控）
  ▼
协议端：NapCat（默认）/ SnowLuma / LLOneBot     ← 可替换，零成本
  │ OneBot 11 反向 WebSocket（JSON）
  │ 请求头：X-Self-ID / X-Client-Role / Authorization
  ▼
moonfox.channels.qq.OneBot11Driver              ← 我们写的薄客户端
  ├─ 鉴权（access_token）
  ├─ 事件分发（无 echo 的包 → on_event）
  ├─ API 调用（带 echo 关联响应）
  └─ ★ action 白名单（结构性安全边界）
  ▼
moonfox.channels.qq.codec                       ← 消息段 ↔ InboundMessage/OutboundSegment
  ▼
moonfox.channels.qq.adapter.QQChannel           ← 触发条件、限流、分段、投递
  ▼
moonfox.session.hub.SessionHub                  ← 会话绑定
  ▼
moonfox.session.orchestration.AgentSession      ← 我们的编排，无人竞争
```

**驱动层抽象**：`QQDriver` 协议把「协议」与「实现」解耦。今天用 OneBot 11 + NapCat，明天换 SnowLuma 或 Satori，写一个新 driver 即可，`QQChannel` 与上层不变。

```python
class QQDriver(Protocol):
    """QQ 协议驱动。只做「收发原始事件与动作」，不做归一化。"""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def on_event(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        """注册原始事件回调（OneBot 11 的 post_type 事件）。"""

    async def call(self, action: str, params: dict) -> dict:
        """调用 API。返回 OneBot 的 {status, retcode, data} 结构。"""

    @property
    def connected(self) -> bool: ...

    @property
    def self_id(self) -> str | None:
        """机器人自己的 QQ 号。"""
```

### 10.2 消息编解码

OneBot 11 支持两种消息格式：`string`（CQ 码）和 `array`（消息段数组）。

**统一用 `array` 格式**。理由：CQ 码需要转义处理（`&`、`[`、`]`、`,` 都要转义），字符串拼接容易出 bug；array 是结构化数据，与内部 `OutboundSegment` 一一对应。

**为什么不用 `@snowluma/sdk` 的 Message Builder**：它的 `text()/at()/image()` 链式构造器与 `fromCQString/toCQString` 确实好用，但输出的是它自己的 `message` 类型，而我们的出站类型是 `OutboundSegment`（要同时服务 QQ 与 WebUI 两个通道，且要参与 `segment_outbound` 的分段逻辑）。用它意味着每次出站都要做一层「`OutboundSegment` → SDK message → CQ 码」的双向映射。**自己写 codec 是 ~150 行**，比维护这个映射便宜。

```python
@dataclass(slots=True)
class InboundMessage:
    """归一化后的入站消息。所有通道共用。"""

    channel: str  # "qq"
    conversation: Conversation
    sender: Sender
    text: str  # 纯文本（已剥离 CQ 码 / at 段）
    segments: list[InboundSegment]  # 完整结构（图片、语音、表情、回复引用）
    external_id: str  # message_id
    timestamp: int
    raw: Any = None  # 原始事件，调试用


@dataclass(slots=True)
class GroupConversation:
    group_id: str
    user_id: str

    @property
    def key(self) -> str:
        return f"group:{self.group_id}:user:{self.user_id}"


@dataclass(slots=True)
class Sender:
    user_id: str
    nickname: str = ""
    card: str = ""  # 群名片
    role: str = "member"  # owner | admin | member
    is_bot: bool = False

    @property
    def display(self) -> str:
        """显示名，优先群名片。"""
        return self.card or self.nickname or self.user_id


@dataclass(slots=True)
class OutboundSegment:
    type: Literal["text", "image", "at", "reply", "face"]
    data: dict[str, Any]
```

**文本提取规则**（`text` 字段的构造）：

| 段类型 | 处理 |
|---|---|
| `text` | 原样保留 |
| `at`（at 机器人自己） | 移除（已由触发条件处理） |
| `at`（at 别人） | 转成 `@昵称` |
| `image` | 转成 `[图片]`，同时把图片 URL 存进 segments 供多模态模型使用 |
| `face` / `mface` | 转成 `[表情:名称]` |
| `reply` | 记录被引用消息的 id，在文本前加 `[回复 xxx]` |
| `voice` | 转成 `[语音]`（不做 ASR，除非后续接入） |
| `forward` | 转成 `[合并转发]` |
| 其他 | 转成 `[类型]` |

### 10.3 出站：分段与限流

**这是聊天机器人体验的关键细节**。模型可能返回 2000 字，QQ 单条消息有长度限制，直接发会被截断或发送失败。

```python
def segment_outbound(
    text: str,
    *,
    max_chars: int = 1200,
    max_segments: int = 5,
) -> list[str]:
    """把长文本切成多段。

    切分优先级（从高到低）：
      1. 段落边界（\n\n）
      2. 换行
      3. 句号 / 问号 / 感叹号 / 分号
      4. 逗号 / 顿号
      5. 硬切（兜底）

    为什么不是简单按长度切：
    在句子中间断开会让每条消息都读不通，体验很差。
    优先在语义边界切，即使每段长度不均。

    max_segments 超限时：前 max_segments-1 段正常发，
    最后一段追加 "……（内容过长，已截断）"。
    """
```

**分段发送的延迟**：段之间加 `segment_delay_ms`（默认 400ms），让消息「一条条出现」而不是瞬间刷屏。更拟人，也降低风控概率。

**限流**：

```python
class ChannelRateLimiter:
    """按会话的令牌桶。

    为什么要限流：QQ 对高频发送有风控，触发后会静默丢消息甚至封号。
    个人机器人不需要压榨吞吐，宁可慢一点。
    """

    def __init__(self, *, rate_per_sec: float = 1.0, burst: int = 3) -> None: ...

    async def acquire(self, key: str) -> None:
        """获取发送许可。超限时 await 等待。"""
```

### 10.4 出站消息的来源

```python
class QQChannel:
    """订阅 AgentSession 事件，把 assistant 的回复投递到 QQ。

    关键：只在 message_end 时投递，不在 message_update 时投递。
    流式增量是给 WebUI 看的，QQ 没有流式概念。
    """

    async def on_session_event(self, key: str, event: SessionEvent) -> None:
        if not key.startswith("qq:"):
            return

        match event.type:
            case "message_end":
                if event.message.role != "assistant":
                    return
                if event.message.stop_reason == "aborted":
                    return  # 被中断的消息不发
                text = extract_text(event.message)
                if not text.strip():
                    return
                await self._deliver(key, text, reply_to=self._pending_reply_to.get(key))

            case "tool_execution_start":
                # 可选：工具执行太久时发个「让我想想」
                ...

            case "agent_end":
                if event.will_retry:
                    await self._deliver(key, "（网络有点问题，我重试一下）")
                elif self._last_outcome == "error":
                    await self._deliver(key, "（我这边出了点问题，稍后再试）")

            case "compaction_start":
                # 不通知用户，这是内部机制
                pass
```

**错误对用户可见**：QQ 用户看不到日志。网络错误、provider 错误必须转成自然的说法发给用户，否则用户会以为机器人死了。

**`stop_reason == "aborted"` 不发送**：软中断时已经生成了部分内容，发给用户会是半句话。

### 10.5 QQ 特有的边界情况

| 情况 | 处理 |
|---|---|
| 消息被撤回 | 监听 `notice/friend_recall`、`notice/group_recall`，在会话里追加一条 UI-only 消息（用户能看到「[消息已撤回]」） |
| 群成员变动 | 监听 `notice/group_increase`，可选：更新 `sender` 缓存；不注入上下文（噪音） |
| 被禁言 | 监听 `notice/group_ban`，暂停该会话的出站队列，恢复后补发（或丢弃，配置决定） |
| 戳一戳 | `notice/notify/poke`，可选：作为触发词（「戳我 = 找我聊天」） |
| 好友请求 / 群邀请 | `request/friend`、`request/group`，默认拒绝（除非配置白名单）；记录到日志 |
| 图片消息 | 存 URL，多模态模型可用；纯文本模型转成 `[图片]` |
| 消息过长 | 分段（见 10.3） |
| 发送失败 | 重试 2 次，仍失败则记录 `ChannelEntry`（`delivery="failed"`）并在 WebUI 显示 |
| 收到自己的消息 | 过滤（`user_id == self_id`） |
| 收到其他机器人 | 按 `ignore_bots` 配置过滤 |
| 空消息 / 纯表情 | 按触发条件决定；私聊时仍然响应（避免用户觉得被无视） |

### 10.6 反向 WS 连接管理

```python
class OneBot11ReverseWSDriver:
    """反向 WS：我们做服务端，SnowLuma 做客户端连过来。

    为什么用反向 WS 而不是正向：
    SnowLuma 跑在本地/同机（它要连 QQ 客户端），我们做服务端。
    反向 WS 让 SnowLuma 主动连我们，我们不需要暴露端口给外部，
    也不需要知道 SnowLuma 的地址。部署更简单、更安全，
    而且**启动顺序无所谓** —— 谁后起谁重连。

    鉴权：检查 Authorization: Bearer <token> 请求头（OneBot 11 标准）。
    token 与端口来自 SnowLuma 的 config/onebot_<uin>.json。
    """

    async def _handle_connection(self, ws) -> None:
        # ── 1. 鉴权（先做，未通过直接关闭）──
        if not self._check_auth(ws):
            await ws.close(code=1008, reason="Unauthorized")
            return

        # ── 2. 识别是哪个机器人账号 ──
        self_id = ws.request.headers.get("X-Self-ID") or ""
        role = ws.request.headers.get("X-Client-Role", "Universal")

        if not self_id:
            # 回退：主动问一次（X-Self-ID 是规范要求，但不保证所有实现都给）
            try:
                info = await self._call_on(ws, "get_login_info", {})
                self_id = str(info.get("user_id", ""))
            except Exception:
                logger.warning("无法确定 self_id，该连接的事件将被丢弃")
                await ws.close(code=1008, reason="Missing X-Self-ID")
                return

        # ── 3. 按 role 分流 ──
        # Universal：同一条连接既推事件又收 API 调用（SnowLuma 默认）
        # Event：只推事件；API 走另一条 API 连接
        match role:
            case "Universal":
                self._sessions[self_id] = _Connection(ws, universal=True)
            case "Event":
                self._event_conns[self_id] = ws
            case "API":
                self._api_conns[self_id] = ws
            case _:
                logger.warning("未知 X-Client-Role: %s", role)
                await ws.close(code=1008, reason="Unknown role")
                return

        logger.info("协议端已连接 self_id=%s role=%s", self_id, role)
        await self._emit_status_changed()

    async def _dispatch(self, self_id: str, payload: dict) -> None:
        """分发一帧。

        ★ 关键：用 echo 区分响应与事件。
        带 echo 的包是我们发出的 API 调用的响应，交给等待中的 future；
        不带 echo 的是事件，推给 on_event 回调。

        这与 @snowluma/sdk 的做法一致（「用 echo 关联 API 响应，
        把无 echo 的包作为事件分发」），是 OneBot 11 的通用约定。
        """
        echo = payload.get("echo")
        if echo is not None:
            waiter = self._pending.pop(echo, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(payload)
            else:
                logger.warning("收到无主的 echo 响应: %s", echo)
            return

        if payload.get("post_type"):
            for handler in self._event_handlers:
                await handler(self_id, payload)

    async def call(self, action: str, params: dict, *, self_id: str | None = None) -> dict:
        """调用 API。走 action 白名单（见 §10.1）。

        多账号时 self_id 决定用哪条连接；单账号可省略。
        """
        if action not in self.ALLOWED_ACTIONS:
            if action in self.DANGEROUS_ACTIONS and not self._config.allow_dangerous:
                raise PermissionError(
                    f"Action {action} is dangerous and disabled. "
                    f"Enable qq.onebot11.allow_dangerous to use it."
                )
            raise PermissionError(f"Action {action} is not in the allowlist")

        sid = self_id or self._default_self_id
        conn = self._sessions.get(sid) or self._api_conns.get(sid)
        if conn is None:
            raise ConnectionError(f"协议端未连接: self_id={sid}")

        echo = str(next(self._echo_counter))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await self._send(conn, {"action": action, "params": params, "echo": echo})
            # 超时保护：协议端可能吞掉请求（例如 QQ 掉线）
            response = await asyncio.wait_for(future, timeout=self._config.call_timeout_s)
        finally:
            self._pending.pop(echo, None)

        if response.get("status") == "failed" or response.get("retcode", 0) != 0:
            raise OneBotApiError(
                action=action,
                retcode=response.get("retcode", -1),
                wording=response.get("wording", ""),
                data=response.get("data"),
            )
        return response.get("data") or {}
```

#### 连接状态与重连

| 事件 | 处理 |
|---|---|
| 连接建立 | 记录 `self_id` → role；emit `ChannelStatusChanged`；WebUI 绿点 |
| 连接断开 | 标记该 self_id 不可用；emit `ChannelStatusChanged`；**不主动重连**（协议端会自己重连，见其配置的间隔） |
| 断线期间有待发消息 | 出站队列保留（有界），重连后按序补发；超过 `offline_queue_ttl_ms` 则丢弃并 emit `ChannelOutbound(delivery="dropped")` |
| 长时间无心跳 | OneBot 11 的 `meta_event`/`heartbeat` 事件（`post_type="meta_event"`、`meta_event_type="heartbeat"`）用作存活检测；超过 3 个周期未收到 → 视为断开 |
| QQ 掉线（协议端还在但 QQ 断了） | 协议端会推 `meta_event`/`lifecycle` 或直接不推心跳；我们的检测同上。**但 QQ 登录状态由 SnowLuma 的 WebUI 管**，我们只能报告「收不到心跳」 |

**为什么我们不主动重连**：反向 WS 的客户端是协议端，重连是它的职责（SnowLuma/NapCat 都有 `reconnect_ms` 配置）。我们主动重连反而会造成「两条连接」的混乱。

#### 多账号的连接管理

```python
# 每个 self_id 一个 QQChannel 实例，共享一个 OneBot11ReverseWSDriver
self._sessions: dict[str, _Connection]     # self_id -> Universal 连接
self._event_conns: dict[str, WebSocket]    # self_id -> Event 连接
self._api_conns: dict[str, WebSocket]      # self_id -> API 连接

# Runtime 启动时按配置为每个启用的账号建一个 channel
for account in config.qq.accounts:
    channel = QQChannel(self_id=account.uin, driver=driver, hub=hub, ...)
    await channel.start()
```

单账号时这套结构退化成「一个 dict 里一个元素」，没有额外复杂度。

**连接状态对 WebUI 可见**：`channel.status` 命令返回 `{connected, self_id, driver, last_heartbeat, pending_calls}`。WebUI 顶部显示一个绿点/红点。

---

### 10.7 故障排查清单（写进 WebUI 的「通道状态」页）

QQ 接入的故障模式比其他部分多（涉及 QQ 客户端、协议端、网络三层）。把排查顺序固化下来，
避免每次出问题都从零开始。

| 症状 | 检查顺序 |
|---|---|
| moonfox 显示「协议端未连接」 | ① SnowLuma 进程在跑吗（`:5099` 能打开吗）② `config/onebot_<uin>.json` 里的 WS 地址/端口与 `[qq.onebot11]` 一致吗 ③ access_token 两边一致吗 ④ 防火墙挡了 `ws_port` 吗 |
| 连上了但收不到消息 | ① SnowLuma WebUI 里 QQ 账号是「在线」吗 ② 触发条件是否把它过滤了（`trigger` / `ignore_bots`）③ 冷却期（`cooldown_ms`）④ `debounce_ms` 还没到 |
| 能收不能发 | ① `send_*_msg` 是否在 `ALLOWED_ACTIONS` 里 ② 看 `ChannelOutbound.delivery` 与 `error` ③ SnowLuma 的动作调试面板直接试一次 ④ QQ 是否被禁言/风控 |
| 回复被截断 | `max_segment_chars` 是否小于协议端/QT 的限制；`max_segments` 是否太小 |
| 消息重复 | `debounce_ms` 是否生效；协议端是否重连后重放（看 `message_id` 是否重复，用 `channel_messages` 表去重） |
| 多账号串号 | `channel_key` 里的 `self_id` 是否正确（§5.1） |

**去重**：协议端重连时可能重放最近的事件。用 `(self_id, message_id)` 在 `channel_messages` 表里做幂等检查，重复的直接丢弃。

---

## 11. 内置工具集

### 11.1 按 surface 组装

```python
def create_tools_for_surface(
    surface: Literal["webui", "qq"],
    *,
    workspace: Path,
    memory: MemoryRuntime | None,
    persona: PersonaRuntime | None,
    settings: ToolsSettings,
) -> list[AgentTool]:
    """按场景组装工具集。

    设计原则：**能力越强，越晚给**。
    聊天场景默认不给 bash/write/edit ——
    用户不会期望聊天机器人有权限改自己电脑上的文件，
    而模型一旦拿到这些工具，就可能因为理解偏差去用。
    """
    tools: list[AgentTool] = []

    if memory is not None and memory.settings.enabled:
        tools.extend(create_memory_tools(memory))

    if persona is not None:
        tools.append(create_persona_switch_tool(persona))

    if surface == "webui":
        tools.extend(create_fs_tools(workspace))
        tools.extend(create_shell_tools(workspace))

    tools.append(create_time_tool())  # 获取当前时间，聊天必需
    tools.append(create_recall_tool())  # 主动查历史消息

    # 用户配置的启用/禁用覆盖
    enabled = set(settings.enabled) if settings.enabled else {t.name for t in tools}
    disabled = set(settings.disabled)
    return [t for t in tools if t.name in enabled and t.name not in disabled]
```

| 工具 | webui | qq | 说明 |
|---|---|---|---|
| `read` / `ls` / `find` / `grep` | ✓ | ✗ | 文件系统 |
| `write` / `edit` | ✓ | ✗ | `execution_mode="sequential"` |
| `bash` / `powershell` | ✓ | ✗ | fail-closed 权限钩子 |
| `memory_search` | ✓ | ✓ | |
| `memory_remember` | ✓ | ✓ | |
| `memory_forget` | ✓ | ✓ | QQ 需二次确认 |
| `memory_list` | ✓ | ✓ | |
| `persona_switch` | ✓ | ✗ | 换人设 |
| `recall_history` | ✓ | ✓ | 主动检索本会话历史消息 |
| `get_time` | ✓ | ✓ | 模型不知道现在几点 |
| `fetch_url` | ✓ | ✓ | 联网（可选，需配置） |

**`recall_history` 的必要性**：压缩会丢掉细节。用户问「我们上周聊了什么」，投影里可能已经只剩摘要。这个工具直接查 JSONL 的原始 entry，绕过投影。

```python
class RecallHistoryParams(BaseModel):
    query: str | None = Field(default=None, description="关键词，留空则按时间返回")
    days_ago: int = Field(default=7, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=100)
    model_config = ConfigDict(extra="forbid")
```

### 11.2 危险工具的权限钩子

```python
def install_permission_hook(session: AgentSession, policy: PermissionPolicy) -> None:
    """fail-closed 的 tool_call 钩子。

    对应接缝 11：权限检查失败必须拒绝执行，不能放行。
    """
    previous = session.agent.before_tool_call

    async def before_tool_call(ctx: BeforeToolCallContext, signal) -> BeforeToolCallResult | None:
        tool = ctx.tool_call.name
        args = ctx.args

        if tool in ("bash", "powershell"):
            command = args.get("command", "")
            verdict = policy.check_command(command)
            if verdict == "deny":
                return BeforeToolCallResult(block=True, reason="该命令不在白名单内")
            if verdict == "ask":
                approved = await session.request_approval(
                    kind="command",
                    payload=command,
                    timeout_ms=policy.approval_timeout_ms,
                )
                if not approved:
                    return BeforeToolCallResult(block=True, reason="用户拒绝了该操作")

        if tool in ("write", "edit"):
            path = args.get("path", "")
            if not policy.is_within_workspace(path):
                return BeforeToolCallResult(block=True, reason="只能操作工作目录内的文件")

        if tool == "memory_forget":
            approved = await session.request_approval(
                kind="forget_memory",
                payload=args,
                timeout_ms=policy.approval_timeout_ms,
            )
            if not approved:
                return BeforeToolCallResult(block=True, reason="用户取消了")

        if previous is not None:
            return await previous(ctx, signal)
        return None

    session.agent.before_tool_call = before_tool_call
```

**审批通道**：WebUI 弹出确认框（`ui_prompt_start` 事件）；QQ 场景发一条「确认要执行 xxx 吗？回复『确认』」（需要实现一个短期的「待确认」状态机）。

**QQ 场景默认禁用危险工具**，所以审批路径主要用于 WebUI。

---

## 12. 事件与协议

### 12.1 三层事件回顾

| 层 | 事件类型 | 终止事件 | 消费者 |
|---|---|---|---|
| `ai` | `AssistantMessageEvent` | `done` / `error` | `agent` 层 |
| `agent` | `AgentEvent` | `agent_end` | `session` 层 |
| `session` | `SessionEvent` | `agent_settled` | `channels` + `webui` |

### 12.2 `SessionEvent` 完整清单（在 mini-pi 基础上扩展）

```python
SessionEvent: TypeAlias = (
    # ── Agent 事件原样透传 ──
    AgentStart
    | TurnStart
    | TurnEnd
    | MessageStart
    | MessageUpdate
    | MessageEnd
    | ToolExecutionStart
    | ToolExecutionUpdate
    | ToolExecutionEnd
    # ── 包装 ──
    | AgentEndWrapped  # + will_retry
    | AgentSettled  # 终点
    # ── 会话级 ──
    | QueueUpdate
    | CompactionStart
    | CompactionEnd
    | EntryAppended
    | SessionInfoChanged
    | ThinkingLevelChanged
    | ModelChanged
    | PersonaChanged
    # ── 恢复级 ──
    | AutoRetryStart
    | AutoRetryEnd
    # ── 新增：通道与记忆 ──
    | ChannelInbound
    | ChannelOutbound
    | ChannelStatusChanged
    | MemoryExtracted
    | MemoryRetrieved
    | MemoryConflict
    # ── 新增：UI 交互 ──
    | UiPromptStart
    | UiPromptEnd
    | Notice
)
```

新增事件的字段：

```python
@dataclass(slots=True)
class ChannelInbound:
    channel: str
    conversation_key: str
    sender_display: str
    text: str
    external_id: str
    type: Literal["channel_inbound"] = "channel_inbound"


@dataclass(slots=True)
class ChannelOutbound:
    channel: str
    conversation_key: str
    segments: list[OutboundSegment]
    delivery: Literal["sent", "failed", "dropped", "partial"]
    external_ids: list[str] = field(default_factory=list)
    error: str | None = None
    type: Literal["channel_outbound"] = "channel_outbound"


@dataclass(slots=True)
class MemoryRetrieved:
    """这一轮召回了什么。WebUI 用它显示「它想起了这些」。"""

    conversation_key: str
    items: list[dict]  # {id, text, kind, score}
    query: str
    type: Literal["memory_retrieved"] = "memory_retrieved"


@dataclass(slots=True)
class MemoryExtracted:
    conversation_key: str
    added: list[dict]
    merged: list[dict]
    superseded: list[dict]
    skipped: int
    type: Literal["memory_extracted"] = "memory_extracted"


@dataclass(slots=True)
class Notice:
    """给用户看的提示（非模型输出）。"""

    level: Literal["info", "warn", "error"]
    message: str
    type: Literal["notice"] = "notice"
```

### 12.3 wire 格式（WebUI 与外部消费者共用）

```python
def to_wire(event: SessionEvent) -> dict[str, Any]:
    """SessionEvent -> 可 JSON 序列化的 dict。

    三条规则（在 mini-pi 基础上补充）：
    1. 剥离累积快照 partial（带宽 O(n^2)）
    2. toolcall_start 的 id / tool_name 提升到顶层（它们只在 partial 里）
    3. 大字段截断：content 超过 8KB 时截断并加 "_truncated": true
       （WebUI 不需要完整的 100KB 文件内容，需要时用 session.history 拉）
    """
```

**消息的 `id` 字段**：`MessageStart` / `MessageEnd` 携带的消息必须带稳定 id，前端才能正确归约。给 `ai.Message` 加一个 `id` 字段（或在 wire 层合成，用 `(session_id, timestamp, role, index)` 哈希）。

**选择后者**：不改 `ai` 层的类型。wire 层用 `f"{session_id}:{timestamp}:{role}:{content_hash[:8]}"` 合成。理由：`ai` 层的消息类型是 pi 骨架的一部分，加字段会传播到所有 provider 实现。

### 12.4 事件顺序保证

| 保证 | 说明 |
|---|---|
| 同一会话内事件严格有序 | 单事件循环 + 单 `asyncio.Queue` |
| 不同会话事件无全局序 | WebUI 按 `key` 分组处理，不做跨会话排序假设 |
| `seq` 单调递增 | 每个会话一个计数器，WS 帧携带，前端可检测丢帧 |
| 断线重连可补齐 | 客户端带 `last_seq` 重连，服务端从 ring buffer（最近 1000 条）补发；超出则返回 `{"type":"resync"}` 让前端重新拉全量状态 |

---

## 13. 运行时装配

```python
class Runtime:
    """装配根。所有单例在这里创建、连接、启动、关闭。

    为什么不用依赖注入框架：这个应用的依赖图是固定的、
    有明确启动顺序的（配置 -> 存储 -> 记忆 -> 人设 -> 模型 -> 会话 -> 通道 -> UI）。
    手写装配比任何框架都清晰，且启动顺序本身就是文档。
    """

    async def start(self) -> None:
        # ── 1. 基础设施 ──
        self.config = load_config()
        self.paths = resolve_paths(self.config)
        setup_logging(self.config.app)

        # ── 2. 存储 ──
        self.db = await Database.open(self.paths.db)
        await self.db.migrate()
        self.session_store_factory = JsonlStoreFactory(self.paths.sessions)

        # ── 3. 记忆 ──
        self.memory = MemoryRuntime(
            store=MemoryStore(self.db),
            embedder=create_embedder(self.config.memory),
            settings=self.config.memory,
        )
        self.memory_worker = MemoryWorker(
            store=self.memory.store,
            extractor=MemoryExtractor(stream_fn=self.stream_fn, model=self.model),
        )

        # ── 4. 人设 ──
        self.personas = PersonaStore(self.paths.personas)
        self.persona_resolver = PersonaResolver(self.config.persona)

        # ── 5. 模型 ──
        self.catalog = ModelCatalog.load(self.paths, self.config.model)
        self.stream_fn = build_stream_fn(self.catalog, self.config.model)

        # ── 6. 会话 ──
        self.hub = SessionHub(runtime=self, sessions_dir=self.paths.sessions)

        # ── 7. 记忆写入订阅 ──
        self.hub.subscribe_events(self.memory_worker.on_session_event)

        # ── 8. 调度 ──
        self.scheduler = Scheduler(self.db, self.hub)
        await self.scheduler.start()

        # ── 9. 通道 ──
        if self.config.qq.enabled:
            # 一个 driver 服务多个账号（多账号共享一条 driver，各自一条连接）
            self.qq_driver = create_qq_driver(self.config.qq)
            await self.qq_driver.start()

            # 每个启用的账号一个 QQChannel，各自绑定 persona
            self.qq_channels = []
            for account in self.config.qq.accounts:
                if not account.enabled:
                    continue
                channel = QQChannel(
                    self_id=account.uin,
                    driver=self.qq_driver,
                    hub=self.hub,
                    config=self.config.qq,
                    persona_id=account.persona,
                )
                await channel.start()
                self.qq_channels.append(channel)

            # 启动后做一次连通性探测（不阻塞启动，失败只警告）
            asyncio.create_task(self._probe_qq_endpoint())

        # ── 10. WebUI ──
        if self.config.webui.enabled:
            self.webui = WebUI(self.hub, self.config.webui, runtime=self)
            await self.webui.start()

        logger.info("moonfox 已启动")

    async def shutdown(self, grace_ms: int = 10000) -> None:
        """优雅退出。顺序与启动相反。

        每一步都要 try/except：关闭过程中一个组件报错，
        不能让后面的组件失去关闭机会（否则 SQLite 不 checkpoint，WS 不关闭）。
        """
        for step in (
            self.webui.stop,       # 1. 先停 UI（用户看到「正在关闭」）
            self._stop_qq,         # 2. 停通道（不再接收新消息）
            self.scheduler.stop,   # 3. 停调度
            self.hub.shutdown,     # 4. 所有会话 abort + 等待 settle
            self.memory_worker.stop,  # 5. 冲刷记忆写入队列
✓ SQLite 3.50.4                      (需要 >= 3.40)
✓ SQLite FTS5                        (中文 trigram 检索可用)
✓ 数据目录 ~/.moonfox                (可写)
✓ 依赖完整                            (fastapi/uvicorn/aiosqlite/websockets...)
✓ Node.js v24.18.1                   (SnowLuma 需要 >= 22.13)
⚠ API key DEEPSEEK_API_KEY           未设置 → 对话功能不可用
✗ 协议端连接                          未连接
    → SnowLuma 在运行吗？(http://localhost:5099)
    → config/onebot_10001.json 的 WS 地址与 [qq.onebot11] 一致吗？
─────────────────────────────────────────────
3 项通过 · 1 项警告 · 1 项失败
```

**② WebUI 首次引导 —— 四步向导**

```
步骤 1/4  模型          [选 provider] [填 API key] [测试连接]  ← 测通了才能下一步
步骤 2/4  人设          [选预设 / 自己写] [预览渲染结果]
步骤 3/4  QQ 接入       [检测协议端] [填 token] [发一条测试消息给自己]
步骤 4/4  记忆与隐私    [开启记忆] [检索范围] [数据保留策略]
```

**设计要点**：
- 每一步都有「测试」按钮，测通了才允许下一步 —— 不要让人走到最后才发现第一步错了
- 允许跳过（可以先只用 WebUI 聊天，之后再接 QQ）
- 步骤 3 的「发一条测试消息给自己」是最有价值的检查 —— 它同时验证了驱动、编解码、鉴权、分段四件事

---

## 19. 待你决定的产品问题

上面 §18 都是**技术问题，答案相对客观**。下面这些是**产品决策，需要你拍板**，因为它们取决于你想要什么样的机器人。

### 19.1 agent 的自我认知边界 —— ✅ 已决策：人设内回答

> **决策（2026-10）**：默认 `honesty_mode = "roleplay"`（人设内回答）。
> 用户问「你是 AI 吗」时，以人设身份回应（「我是月狐啊」），不承认也不否认「AI」框架。
> 配置仍保留三档开关，切换是配置级操作，不改代码。

人设里写了「不主动暴露自己是机器人」。用户直接问「你是 AI 吗」时的处理：

| 选项 | 效果 | 风险 |
|---|---|---|
| **承认** | 诚实，符合主流价值观 | 破坏沉浸感 |
| **回避** | 「哼，问这个干嘛」 | 反复追问会尴尬 |
| **人设内回答** | 「我是月狐啊」 | 严格说是在撒谎 |
| **可配置** | 每个 persona 一个开关 | 默认值仍需你定 |

**落地**：`honesty_mode` 是 `Persona` 的字段（见 §7.1），渲染进 `principles` section：

```python
# persona/render.py —— honesty_mode 渲染规则
_HONESTY_CLAUSE = {
    "roleplay": (
        "你始终以人设身份存在。被问及是否是 AI / 机器人 / 程序时，"
        "以你的身份自然回应（例如「我是月狐啊」），不承认也不否认这些框架，"
        "不跳出角色解释自己的实现。"
    ),
    "honest": ("你是由 LLM 驱动的虚拟角色。被直接问及时如实承认，然后回到角色。"),
    "deflect": ("被问及是否是 AI 时，用符合性格的方式转移话题，不正面回答。"),
}
```

**注意**：`roleplay` 是 prompt 层约束，不是硬保证 —— 模型仍可能在极端追问下破防。
这是可接受的：个人自用场景下它只是体验偏好，不是安全边界。
如果这个 QQ 号会和**不知情的人**聊天，建议对该会话用 `honest`（人设可按会话覆盖，§7.4）。

### 19.2 群聊 `shared` 模式的说话人识别

`shared` 模式（一个群一个会话）下，多个人在同一个上下文里说话。归因前缀（§4.3）解决了「模型知道谁在说」，但没解决：

- 用户 A 问的问题，模型回答了，用户 B 接着说「那我也问一个」—— 模型会不会把 B 的上下文当成 A 的延续？
- 记忆归属谁？`subject_id` 该填群还是发言人？

**建议**：`shared` 模式只在**小群/熟人群**用（配置里写明），且记忆的 `subject_id` 填**发言人**而不是群。这样「群里 A 说过的话」在 A 的私聊里理论上可召回 —— 但这又牵扯到 §19.3。

### 19.3 记忆的跨会话边界（设计文档 §16.3 已列，但需要你定）

群聊里 A 说的「我最近在找工作」，A 私聊时该不该被召回？

| 选项 | 隐私 | 体验 |
|---|---|---|
| **硬隔离**（当前设计） | 最安全 | 用户会觉得「你怎么不记得我在群里说过」 |
| 同 `subject_id` 共享 | 有泄露风险（群友可能看到 A 的私事被提及） | 更连贯 |
| 共享但打标签 | 折中 | 实现复杂 |

**建议**：先做**硬隔离**，观察真实使用中「该记得却没记得」的频率。如果很频繁，再加「同 subject_id 共享**事实类**记忆，不共享**事件/偏好类**」的中间档。

### 19.4 主动搭话的边界

`autonomous` 队列（§4.2）现在默认关闭、预算为 0。要开启的话需要定：

- 什么情况下可以主动说话？（定时任务？沉默 N 小时后？有未完成的承诺？）
- 频率上限？（每天最多几次）
- 时段限制？（半夜不打扰）
- 什么内容算「值得主动说」？

**建议**：**先不开**。主动搭话是体验最容易做坏的功能 —— 做得好是惊喜，做不好是骚扰。等基础功能跑顺了、有真实使用数据了再设计。配置项已经预留好了。

### 19.5 多用户场景下的权限模型

如果这个机器人会服务多个人（比如你有几个朋友也在用），需要定：

- 谁能改配置、换人设、看记忆？（当前 WebUI 只有一个 token，等于全权限）
- 别人能不能看到「我的记忆」？（当前 `conversation_key` 隔离，但 WebUI 是全局视图）
- 危险工具的审批，谁批？

**建议**：**先按单用户设计**（你自己 + 一个 QQ 号）。多用户是个大课题，会牵动 WebUI 权限、记忆隔离、配置作用域三处，值得单独一轮设计。如果只是「朋友也能聊」，用 `per_user` 会话粒度 + 不给 WebUI 访问就够了。

---

## 20. 补充后的实施顺序调整

§18 的内容不是「以后再补」，其中两项必须进早期阶段：

| 阶段 | 新增内容 | 为什么不能延后 |
|---|---|---|
| **P0** | `moonfox doctor` 骨架 + 时区配置 | doctor 是后面所有联调的抓手；时区影响 `scheduled_jobs` 的表结构 |
| **P1** | `wrap_with_concurrency` + `QueueWait` 事件 | 它是 `stream_fn` 的包装层，晚加要改所有调用点 |
| **P2** | `turn_budget` 的 `on_limit="compact"` 分支 | 与压缩联动，压缩在 P4 |
| **P4** | 成本记账（`usage_events` 的 kind 区分）+ 价格表 | 压缩/记忆抽取的用量必须从第一天就分开记，否则事后无法归因 |
| **P5** | `retry_after` 解析 + 退避抖动 | 与重试逻辑同期做 |
| **P8** | `send_idempotent` + `channel_messages` 去重 | 与出站路径同期做，事后加要改所有发送点 |
| **P9** | 成本硬上限的 `_handle_post_run` 钩子 | 记忆抽取是第二大成本源，要在它上线前就有闸门 |
| **P11** | `Scheduler` 承载：清理任务、自动备份 | 与调度器同期做 |
| **P12** | WebUI 首次引导向导 | 最后一个阶段，因为要等前面全部可用 |

---

## 附录 A：与 pi 上游的对应关系

pi 上游仓库（`earendil-works/pi`）的实际结构与本文的对应：

| pi 上游 | 本文对应 | 说明 |
|---|---|---|
| `packages/ai` | `src/moonfox/ai` | 供应商层 |
| `packages/agent` | `src/moonfox/agent` | 核心循环（本文对其循环退出条件做了改造） |
| `packages/coding-agent` | `src/moonfox/session` + `app` | pi 这里是 160KB 的单文件 `agent-session.ts`，本文拆成三个文件 |
| `packages/tui` | — | 不做 |
| `packages/server` / `packages/client` / `packages/protocol` | `src/moonfox/webui` | pi 的服务端是路由/附件/多 presentation 的通用抽象；本方案是单进程内的直接订阅，复杂度低两个数量级 |
| `packages/durable` | `src/moonfox/session/store.py` | pi 的 durable 支持 JSONL 与 SQLite 两种存储；本文采用 JSONL 会话 + SQLite 记忆的双轨 |
| `packages/codemode` / `packages/mcp` | — | 不做 |

**结论**：本文的 `ai` + `agent` 两层与 pi 上游一一对应，`session` 层是 pi `coding-agent` 核心的拆分，其余（persona / memory / channels / webui）是 pi 没有的领域扩展。

## 附录 B：决策记录（ADR 摘要）

| # | 决定 | 日期 | 状态 |
|---|---|---|---|
| 1 | agent 循环采用「有界内层 + 外层重入」，而非无限内层或每消息一次 run | — | 采纳 |
| 2 | 队列扩展为 4 类，`user` 与 `autonomous` 分离 | — | 采纳 |
| 3 | 中断分三级（interrupt / abort / shutdown） | — | 采纳 |
| 4 | 人设与记忆都是 prompt section，不是字段 | — | 采纳 |
| 5 | 记忆检索用 FTS5 trigram 而非向量库，但保留 Embedder 协议 | — | 采纳 |
| 6 | 记忆写入挂在 `agent_settled` 而非 `agent_end` | — | 采纳 |
| 7 | QQ 协议用 OneBot 11，驱动层抽象以便将来换协议 | — | 采纳 |
| 8 | 群聊默认 `per_user` 会话粒度 | — | 采纳，可配置 |
| 9 | 前端无构建（原生 ES Module） | — | 采纳，接口稳定可替换 |
| 10 | 会话存 JSONL、记忆存 SQLite 的双轨存储 | — | 采纳 |
| 11 | 扩展系统延后到阶段 6 之后 | — | 采纳 |
| 12 | 危险工具在 QQ 场景默认禁用 | — | 采纳 |
| 13 | **QQ 接入走路线 A（直连协议端 + 自写薄 OneBot 客户端）**，而非 TypeScript SDK 或 MCP 或 NoneBot2 | — | 采纳，理由见 §10.1 |
| 14 | **QQ 出站只允许白名单内的 OneBot action**，危险 action 默认全关 | — | 采纳，结构性安全 |
| 15 | **协议端默认 SnowLuma**（对接 QQ 稳定），NapCat 作为可切换备选 | — | 采纳，协议层已解耦 |
| 18 | `channel_key` 必须包含 `self_id`（支持多账号隔离） | — | 采纳，见 §5.1 |
| 19 | QQ 出站不主动重连（重连是协议端职责） | — | 采纳，见 §10.6 |
| 20 | **QQ 聊天记忆与 Agent（WebUI）对话记忆硬隔离**：`memories.domain` 显式列 + 检索无跨域路径 + 画像复合主键 | — | 采纳（用户决策），见 §6.3 |
| 21 | **自我认知默认「人设内回答」**（`honesty_mode="roleplay"`），保留 honest/deflect 配置档 | — | 采纳（用户决策），见 §19.1 |
| 16 | **轮数预算是配置项（TurnBudget），不是常量** | — | 采纳，见 §4.2 |
| 17 | run 中途改配置不影响正在跑的 run（预算快照） | — | 采纳，保证可复现 |

### 待定（需要真实使用数据后决策）

| # | 议题 | 当前状态 | 决策依据 |
|---|---|---|---|
| D1 | 群聊是否需要 `threaded` 会话粒度 | 只有 `shared` / `per_user` | 观察真实群聊中「多话题串线」的发生频率 |
| D2 | 是否启用向量检索（`memory.embedder`） | 默认 `none` | 观察纯 FTS5 的召回失败率 |
| D3 | QQ 域**内部**是否跨会话共享（同 `subject_id`） | 当前 `conversation_key` 硬隔离；**跨域隔离已定死**（§6.3） | 观察「群里说过的话，私聊该不该记得」 |
| D4 | 是否给外部 agent 暴露 moonfox 的 MCP server | 不做 | 有实际需求再说 |
| D5 | 人设漂移的补偿机制（定期重发人设 section？） | 不做 | 需要实测漂移速度 |