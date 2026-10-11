# mini pi 设计文档

> 目标：用 Python 复刻 pi agent 的**架构骨架**（不是功能全集），使后续能持续扩展 WebUI / TUI / 扩展系统 / 多 provider，而不需要回头改核心。
>
> 本文是落盘方案，包含：分层、模块、类型、契约、实施顺序、Python 工程约定。

---

## 0. 文档范围

### 0.1 必须第一天就位的（接缝）

这些是「缺了它后面加功能就要重构」的结构性决定：

1. `AgentMessage` 与 `Message` 分离，转换边界唯一
2. `system_prompt` 只读，从 transcript 重放
3. 错误编码在流里，异常只表示编程错误
4. `EventStream` 的 `is_complete` / `extract_result` 参数化
5. 双队列（steering / follow-up）+ 两种投递模式
6. 4 个核心钩子的精确位置
7. `finish_turn` 的 continue / end 语义
8. session entry 带 `id` + `parent_id`
9. 投影是派生视图，原始 entry 永不删除
10. 扩展注册表 + 快照式派发
11. 钩子按返回值语义分四类（含 fail-closed 那一类）
12. 边界草稿 + 预览 + 原子提交
13. `SessionEvent` 独立于 `AgentEvent`
14. wire 格式剥离累积快照
15. 背压作为独立订阅

### 0.2 可以后补的（功能）

- 多 provider（先做 1 个真实 + 1 个 faux）
- 虚拟模型 / 模型路由
- 缓存预热
- 会话树 UI（`/tree` `/fork` `/clone`）
- MCP、codemode
- 供应商归因、遥测

---

## 1. 设计原则

### 1.1 原语在下，策略在上

同一件事的能力（原语）放底层，具体做法（策略）放上层。

例：
- `ai` 层提供 `estimate_context_tokens()`、`is_context_overflow()` —— 任何程序都需要
- `session` 层决定「超过 `context_window - reserve` 就压缩，保留最近 20k」 —— 产品决策

违反这条的后果：策略换不掉。比如把压缩逻辑写进 loop，就再也无法为不同项目用不同压缩算法。

### 1.2 接缝优先于功能

宁可功能少，也要把层边界和钩子位置定死。功能是加法，接缝是乘法——接缝错了，后面每个功能都要付利息。

### 1.3 错误编码在流里

provider 请求失败、鉴权失败、网络断流、用户中断，全部编码成 `stop_reason` + `error_message`，走正常事件序列。

异常只用于**编程错误**，例如「从 assistant 消息继续」这类契约违反。

违反这条的后果：错误处理分叉成两套路径（try/except 分支 + 流事件分支），每个调用点都要处理两边。

### 1.4 transcript 是单一事实源

prompt 和工具声明不是可变字段，而是 transcript 中 system message 的**重放结果**。

三个收益：
- 增量更新（只发 section 差量，保住 provider 缓存前缀）
- 可回放（从任意 transcript 前缀重建当时的 prompt）
- 不可漂移（`state.system_prompt` 永远等于 provider 实际收到的）

---

## 2. 包结构

依赖严格单向：`ai <- agent <- session <- app <- ui`

```
mini_pi/
  ai/                        # 供应商层：不认识 agent
    __init__.py
    types.py                 # Message, ContentBlock, Usage, StopReason, Model
    stream.py                # EventStream, AssistantMessageEventStream
    catalog.py               # 模型目录（静态数据 + 运行时覆盖）
    auth.py                  # 凭据解析：env / 文件 / OAuth 回调
    text.py                  # get_system_message_text, content_text
    transcript.py            # 重放原语
    estimate.py              # token 估算
    overflow.py              # 溢出识别（各 provider 错误正则）
    providers/
      __init__.py
      base.py                # StreamFn Protocol
      openai.py              # 第一个真实 provider
      faux.py                # 测试用假 provider（第一周就要有）

  agent/                     # 核心原语：不认识会话 / UI
    __init__.py
    types.py                 # AgentMessage, AgentEvent, AgentTool, AgentContext
    stream.py                # create_agent_stream
    hooks.py                 # 4 个钩子的类型
    loop.py                  # agent_loop / agent_loop_continue（无状态纯函数）
    tools.py                 # 工具执行：preflight / parallel / finalize
    agent.py                 # Agent 类：状态 + 双队列 + subscribe
    abort.py                 # AbortToken

  session/                   # 会话与编排
    __init__.py
    entries.py               # SessionEntry 联合类型
    store.py                 # JSONL 追加读写
    projection.py            # 树 -> 线性投影
    manager.py               # SessionManager
    compaction.py            # 压缩
    orchestration.py         # AgentSession：外层 run 循环、重试、边界
    events.py                # SessionEvent
    boundary.py              # 草稿 + 预览 + 提交

  app/                       # 应用层
    __init__.py
    prompt.py                # system prompt 分段组装 + diff
    wire.py                  # SessionEvent -> JSON（剥离 partial）
    tools/                   # 内置工具
      __init__.py
      read.py  write.py  edit.py  bash.py  grep.py  find.py  ls.py
    extensions/
      __init__.py
      types.py               # 扩展 API 与事件类型
      registry.py            # 扩展注册表
      dispatch.py            # 事件派发
      loader.py              # 从目录 / 包加载
    modes/
      __init__.py
      interactive.py         # TUI 入口
      print_mode.py
      json_mode.py
      rpc.py

  ui/                        # 只订阅 SessionEvent
    tui/
    web/

  tests/
    test_loop.py
    test_tools.py
    test_projection.py
    test_dispatch.py
```

### 2.1 依赖规则

| 包 | 可以 import | 不可以 import |
|---|---|---|
| `ai` | 标准库、pydantic | 任何 `mini_pi` 内部包 |
| `agent` | `ai` | `session`、`app`、`ui` |
| `session` | `ai`、`agent` | `app`、`ui` |
| `app` | `ai`、`agent`、`session` | `ui` |
| `ui` | 全部 | - |

用 `import-linter` 或一个简单的测试断言强制这条规则：

```python
def test_layering():
    """ai 不得 import agent/session/app/ui。"""
    for path in (ROOT / "mini_pi" / "ai").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(
                    ("mini_pi.agent", "mini_pi.session", "mini_pi.app", "mini_pi.ui")
                )
```

---

## 3. Layer 1: ai（供应商层）

职责：把「一次模型调用」封装成统一的流协议。不认识 agent、不认识会话。

### 3.1 基础类型（ai/types.py）

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias

StopReason: TypeAlias = Literal[
    "pending", "stop", "length", "tool_use", "error", "aborted", "deferred"
]


@dataclass(slots=True)
class Cost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


@dataclass(slots=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0
    # output 的子集。仅部分 provider 报告，不报告的留 None
    reasoning: int | None = None
    cost: Cost = field(default_factory=Cost)

    @property
    def effective_total(self) -> int:
        """某些 provider 的 total_tokens 为 0，需要自己算。"""
        return self.total_tokens or (self.input + self.output + self.cache_read + self.cache_write)
```

#### 内容块

```python
@dataclass(slots=True)
class TextContent:
    text: str
    # 某些 provider（OpenAI Responses）需要回放签名
    text_signature: str | None = None
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ThinkingContent:
    thinking: str
    # provider 私有或序列化的 reasoning 回放数据，必须原样回传
    thinking_signature: str | None = None
    # 被安全策略脱敏时，加密载荷存在 thinking_signature 里
    redacted: bool = False
    type: Literal["thinking"] = "thinking"


@dataclass(slots=True)
class ImageContent:
    data: str  # base64
    mime_type: str
    type: Literal["image"] = "image"


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Google 专用：复用 thought context 的签名
    thought_signature: str | None = None
    type: Literal["toolCall"] = "toolCall"


ContentBlock: TypeAlias = TextContent | ThinkingContent | ImageContent | ToolCall
```

**为什么内容块不能拍平成字符串**：thinking 的签名回放、图片、tool call 的 partial JSON 都需要结构化。拍平后每加一种能力都要改消息结构。

#### 消息

```python
@dataclass(slots=True)
class SystemMessage:
    """
    前导 system message 是系统提示，后续的修改它：
      - content  追加指令
      - sections 按名替换（None 表示删除）
      - tools_added / tools_removed  改工具集
    按顺序重放所有 system message 得到当前 prompt 与工具集。
    """

    content: str | list[TextContent]
    timestamp: int
    sections: dict[str, str | None] | None = None
    tools_added: list[ToolDeclaration] | None = None
    tools_removed: list[str] | None = None
    role: Literal["system"] = "system"


@dataclass(slots=True)
class UserMessage:
    content: str | list[TextContent | ImageContent]
    timestamp: int
    role: Literal["user"] = "user"


@dataclass(slots=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: list[TextContent | ImageContent]
    is_error: bool
    timestamp: int
    details: Any = None
    structured_content: Any = None
    usage: Usage | None = None
    # 该工具通过 ctx.execute_tool() 发起的嵌套调用记录（仅供审计，不发给模型）
    nested_calls: list[NestedToolCall] | None = None
    duration_ms: int | None = None
    role: Literal["toolResult"] = "toolResult"


@dataclass(slots=True)
class AssistantMessage:
    content: list[ContentBlock]
    api: str
    provider: str
    model: str
    usage: Usage
    stop_reason: StopReason
    timestamp: int
    # 以下全部留位，第一天就定义，避免后期迁移
    response_model: str | None = None
    response_id: str | None = None
    provider_thinking_level: str | None = None
    thinking_level: str | None = None
    error_message: str | None = None
    raw_stop_reason: str | None = None
    end_turn: bool | None = None
    duration_ms: int | None = None
    diagnostics: list[dict[str, Any]] | None = None
    role: Literal["assistant"] = "assistant"


Message: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
```

#### 工具声明与请求上下文

```python
@dataclass(slots=True)
class ToolDeclaration:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(slots=True)
class Context:
    """公开入口。system_prompt / tools 是前导 system message 的简写。"""

    messages: list[Message]
    system_prompt: str | None = None
    tools: list[ToolDeclaration] | None = None


class TranscriptContext:
    """
    规范化后的请求上下文，交给 provider。

    用带 brand 的类而非 dict：只有 normalize_context() 能产出它，
    防止裸 Context 意外到达 provider（那样 prompt 和工具声明就丢了）。
    """

    __slots__ = ("messages",)

    def __init__(self, messages: list[Message], _token: object = None) -> None:
        if _token is not _BRAND:
            raise TypeError("TranscriptContext 只能由 normalize_context() 创建")
        self.messages = messages


_BRAND = object()
```

### 3.2 流事件（ai/types.py）

```python
@dataclass(slots=True)
class Start:
    partial: AssistantMessage
    type: Literal["start"] = "start"


@dataclass(slots=True)
class TextStart:
    content_index: int
    partial: AssistantMessage
    type: Literal["text_start"] = "text_start"


@dataclass(slots=True)
class TextDelta:
    content_index: int
    delta: str
    # 共享的 live 对象：随每个 delta 增长，不是事件时刻的快照。
    # 需要稳定快照的消费者必须自行 copy.copy()。
    partial: AssistantMessage
    type: Literal["text_delta"] = "text_delta"


@dataclass(slots=True)
class TextEnd:
    content_index: int
    content: str
    partial: AssistantMessage
    type: Literal["text_end"] = "text_end"


# thinking_start / thinking_delta / thinking_end 同构
# toolcall_start / toolcall_delta / toolcall_end 同构


@dataclass(slots=True)
class Done:
    reason: Literal["stop", "length", "tool_use", "deferred"]
    message: AssistantMessage
    type: Literal["done"] = "done"


@dataclass(slots=True)
class Error:
    reason: Literal["aborted", "error"]
    error: AssistantMessage
    type: Literal["error"] = "error"


AssistantMessageEvent: TypeAlias = (
    Start
    | TextStart
    | TextDelta
    | TextEnd
    | ThinkingStart
    | ThinkingDelta
    | ThinkingEnd
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | Done
    | Error
)
```

协议约定（写进 docstring，测试要覆盖）：

1. 成功流先 `Start`，再增量事件，最后 `Done`
2. 生成前失败可直接 `Error`；`Start` 之后失败也以 `Error` 终止
3. 增量事件和 `Done` 绝不早于 `Start`
4. `partial` 是共享可变对象，不是快照

### 3.3 EventStream（ai/stream.py）

**用 `asyncio.Queue`，不要用 async generator。** 三个理由：

1. generator 只能被消费一次，而 TUI / WebUI / 持久化可能同时订阅
2. 需要 `result()` 与迭代分离——可以只 `await stream.result()` 不消费事件
3. 订阅者可能中途挂载 / 卸载

```python
import asyncio
import time
from typing import AsyncIterator, Callable, Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class EventStream(Generic[T, R]):
    """
    单生产者、多消费者安全的异步事件流。

    is_complete / extract_result 参数化，使同一个类同时服务两层：
      - provider 层：done/error 终止，携带 AssistantMessage
      - agent  层：agent_end 终止，携带 list[AgentMessage]
    """

    def __init__(
        self,
        is_complete: Callable[[T], bool],
        extract_result: Callable[[T], R],
    ) -> None:
        self._queue: asyncio.Queue[T | None] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._result: asyncio.Future[R] = self._loop.create_future()
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def push(self, event: T) -> None:
        if self._done:
            return  # 终止后静默丢弃
        if self._is_complete(event) and not self._result.done():
            self._done = True
            self._result.set_result(self._extract_result(event))
        self._queue.put_nowait(event)

    def end(self, result: R | None = None) -> None:
        self._done = True
        if result is not None and not self._result.done():
            self._result.set_result(result)
        self._queue.put_nowait(None)  # 哨兵

    async def __aiter__(self) -> AsyncIterator[T]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def result(self) -> R:
        return await asyncio.shield(self._result)
```

#### AssistantMessageEventStream：计时

```python
class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """
    provider 层流。额外负责计时：终止消息带上 duration_ms。

    从别处开始的响应（如延迟取回的 deferred 结果）不打时间戳——
    它的 timestamp 早于本流创建时刻，打上就是错的。
    """

    def __init__(self) -> None:
        super().__init__(
            is_complete=lambda e: e.type in ("done", "error"),
            extract_result=lambda e: e.message if e.type == "done" else e.error,
        )
        self._started_at = now_ms()
        self._started_monotonic = time.perf_counter()

    def push(self, event: AssistantMessageEvent) -> None:
        if event.type == "done":
            self._time(event.message)
        elif event.type == "error":
            self._time(event.error)
        super().push(event)

    def end(self, result: AssistantMessage | None = None) -> None:
        if result is not None:
            self._time(result)
        super().end(result)

    def _time(self, message: AssistantMessage) -> None:
        if self._done or message.duration_ms is not None:
            return
        if message.timestamp < self._started_at:
            return  # 转发别处开始的响应，不计时
        elapsed = (time.perf_counter() - self._started_monotonic) * 1000
        message.duration_ms = max(0, round(elapsed))
```

### 3.4 StreamFn 契约（ai/providers/base.py）

```python
class StreamFn(Protocol):
    """
    契约（违反会导致上层错误处理分叉，测试必须覆盖）：

    1. 不得抛异常。请求失败、鉴权失败、网络断流、中断，
       全部编码成 Error 事件。
    2. 必须返回流。即使立刻失败，也返回一个只含 Error 事件的流。
    3. 必须接受规范化上下文：prompt 和工具声明在 transcript 的
       system message 里，不在 Context.system_prompt / Context.tools。
    """

    def __call__(
        self,
        model: Model,
        context: TranscriptContext,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream: ...


@dataclass(slots=True)
class StreamOptions:
    signal: AbortToken | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning: str | None = None  # thinking level
    session_id: str | None = None
    cache_retention: Literal["short", "long"] = "short"
    headers: dict[str, str] | None = None
    # 观测钩子
    on_payload: Callable[[Any, Model], Any | None] | None = None
    on_response: Callable[[ProviderResponse, Model], None] | None = None
```

### 3.5 重放原语（ai/transcript.py）

这一层最容易被忽略，但**任何**用 provider 层的程序都需要它。

```python
def create_initial_system_message(
    system_prompt: str | None,
    tools: list[ToolDeclaration] | None,
) -> SystemMessage | None:
    """两者都空时返回 None，保持空 transcript 为空。"""

def normalize_context(context: Context) -> TranscriptContext:
    """把 Context.system_prompt / Context.tools 折进前导 system message。
    唯一能产出 TranscriptContext 的入口。"""

def get_current_system_message(messages: Sequence[Any]) -> SystemMessage | None:
    """重放所有 system message，得到当前 prompt + 工具声明。"""

def get_current_system_prompt(messages: Sequence[Any]) -> str:

def get_current_tools(messages: Sequence[Any]) -> list[ToolDeclaration]:

def get_tool_state_changes(
    declared: list[ToolDeclaration],
    executable: list[ToolDeclaration],
) -> ToolStateChanges:
    """diff 出 tools_added / tools_removed。"""

def collapse_system_messages(messages: Sequence[Message]) -> list[Message]:
    """把多个 system message 折叠成一个。给不支持 mid-conversation
    system message 的 provider 用。"""

def to_tool_declaration(tool: AgentTool) -> ToolDeclaration:
```

注意 `get_current_*` 的签名接受 `Sequence[Any]`——因为 agent transcript 里可能有自定义 role 的消息，重放只读 `role == "system"` 的条目。

### 3.6 token 估算（ai/estimate.py）

```python
CHARS_PER_TOKEN = 3.5
ESTIMATED_IMAGE_CHARS = 4800

@dataclass(slots=True)
class ContextUsageEstimate:
    tokens: int
    """估算的总上下文 token。"""
    usage_tokens: int
    """最近一个可用 assistant usage 报告的真实 token。"""
    trailing_tokens: int
    """该 usage 之后新增消息的估算 token。"""
    last_usage_index: int | None

def estimate_context_tokens(messages: Sequence[Message]) -> ContextUsageEstimate:
    """
    混合估算：
      1. 找到最后一条带 usage 的 assistant 消息，取真实 token
      2. 其后的消息按字符数 / CHARS_PER_TOKEN 估算
    比纯字符估算准得多，因为 provider 报告的数字是真实的。
    """

def estimate_text_tokens(text: str) -> int:
def estimate_message_tokens(message: Message) -> int:
def calculate_context_tokens(usage: Usage) -> int:
```

### 3.7 溢出识别（ai/overflow.py）

各 provider 的溢出错误消息格式完全不同，需要一张正则表：

```python
OVERFLOW_PATTERNS: list[re.Pattern[str]] = [
    # Anthropic: "prompt is too long: 213462 tokens > 200000 maximum"
    # Anthropic: 413 {"error":{"type":"request_too_large",...}}
    # OpenAI: "Your input exceeds the context window of this model"
    # OpenAI: "Requested token count exceeds the model maximum context length of 131072 tokens"
    # OpenAI-compatible: "Input length (265330) exceeds model maximum context length (262144)."
    # Google: "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575)"
    # xAI: "This model maximum prompt length is 131072 but the request contains 537812 tokens"
    # Groq: "Please reduce the length of the messages or completion"
    # OpenRouter: "This endpoint maximum context length is X tokens. However, you requested about Y tokens"
    # Together: "The input (X tokens) is longer than the model context length (Y tokens)."
    # llama.cpp: "the request exceeds the available context size, try increasing it"
    ...
]


def is_context_overflow(message: AssistantMessage) -> bool:
    """从 assistant 错误消息中识别上下文溢出。"""


def is_recoverable_length(message: AssistantMessage) -> bool:
    """输出被 token 上限截断（stop_reason == "length"）且可恢复。"""
```

**为什么放 ai 层**：溢出识别依赖 provider 的错误格式知识，这是 provider 层的信息。session 层的压缩策略只需要调用 `is_context_overflow()`。

### 3.8 faux provider（ai/providers/faux.py）

**第一周就要有**。这不是「以后补测试」，而是让 loop 可以在无网络、无密钥的情况下被完整测试。

```python
@dataclass
class FauxResponse:
    """一段预置的 provider 响应。"""

    content: list[ContentBlock]
    stop_reason: StopReason = "stop"
    usage: Usage = field(default_factory=Usage)
    error_message: str | None = None
    delay_ms: int = 0

    @classmethod
    def text(cls, text: str, **kw) -> FauxResponse: ...

    @classmethod
    def tool_call(
        cls, name: str, arguments: dict[str, Any], call_id: str = "call_1", **kw
    ) -> FauxResponse: ...

    @classmethod
    def error(cls, message: str, stop_reason: StopReason = "error") -> FauxResponse: ...

    def to_stream(self, model: Model) -> AssistantMessageEventStream:
        """产出符合协议的事件序列：start -> *_delta -> done|error。"""


class FauxProvider:
    """按脚本产出事件序列，并记录收到的请求供断言。"""

    def __init__(self, script: list[FauxResponse]) -> None:
        self._script = list(script)
        self.requests: list[tuple[Model, TranscriptContext, StreamOptions | None]] = []

    def stream(
        self, model: Model, context: TranscriptContext, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        self.requests.append((model, context, options))
        if not self._script:
            raise AssertionError("FauxProvider 脚本已耗尽")
        return self._script.pop(0).to_stream(model)
```

有了它，loop 的每个分支（工具调用、截断、错误、中断、steering、follow-up）都能写成确定性测试。

---

## 4. Layer 2: agent（核心原语）

职责：实现「消息 -> provider -> 工具 -> 消息」的循环。不认识会话、不认识 UI。

### 4.1 消息抽象（agent/types.py）

```python
from mini_pi.ai.types import (
    AssistantMessage,
    ContentBlock,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
)

# 自定义消息通过注册表扩展（Python 没有 declaration merging）
_CUSTOM_MESSAGE_TYPES: dict[str, type] = {}


def register_message_type(name: str):
    """注册自定义 AgentMessage 类型。

    用法：
        @register_message_type("notification")
        @dataclass(slots=True)
        class NotificationMessage:
            text: str
            timestamp: int
            role: Literal["notification"] = "notification"
    """

    def decorator(cls: type) -> type:
        _CUSTOM_MESSAGE_TYPES[name] = cls
        return cls

    return decorator


# transcript 视角：可以有 UI-only 消息
AgentMessage: TypeAlias = (
    ai.SystemMessage
    | ai.UserMessage
    | ai.AssistantMessage
    | ai.ToolResultMessage
    | Any  # 注册的自定义类型
)

# LLM 视角：只有四种 role
Message: TypeAlias = ai.Message

# 转换边界：唯一允许丢弃 / 转换消息的地方
ConvertToLlm = Callable[[list[AgentMessage]], Awaitable[list[Message]]]
TransformContext = Callable[
    [list[AgentMessage], "AbortToken | None"], Awaitable[list[AgentMessage]]
]
```

**为什么这个分离是第一天必须的**：一旦应用层把 UI 状态塞进消息并直接送 provider，后期想加「压缩提示条」「工具执行进度卡片」「错误横幅」这类 UI-only 消息，就得重构整条消息管道。

#### 默认 convert_to_llm

```python
def default_convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """过滤掉非 LLM role 的消息。"""
    return [m for m in messages if m.role in ("system", "user", "assistant", "toolResult")]
```

### 4.2 工具定义（agent/types.py）

```python
ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]


@dataclass(slots=True)
class AgentToolResult:
    content: list[TextContent | ImageContent]
    """给模型看的结果。"""
    details: Any = None
    """给 UI / 日志看的结构化数据。"""
    structured_content: Any = None
    """给程序调用者的、符合 output_schema 的结果。不发给模型。"""
    usage: Usage | None = None
    is_error: bool = False
    """
    报告失败但不抛异常。模型看到的 content 是错误结果（同抛异常），
    但 details / structured_content 保留给 UI 和程序调用者。
    """
    terminate: bool = False
    """提示本批次后应停止。仅当批次内每个最终结果都为 True 才生效。"""


@dataclass(slots=True)
class AgentTool:
    name: str
    description: str
    label: str
    """UI 显示名。"""
    parameters: type[BaseModel]
    """Pydantic 模型，用 model_json_schema() 产出 provider 需要的 JSON Schema。"""
    execute: Callable[..., Awaitable[AgentToolResult]]
    output_schema: dict[str, Any] | None = None
    prepare_arguments: Callable[[Any], Any] | None = None
    """schema 校验前的参数兼容垫片（例如把字符串化的 JSON 还原）。"""
    execution_mode: ToolExecutionMode | None = None
    """覆盖全局执行模式。任一工具声明 sequential 会让整批串行。"""
    replay: Literal["never", "safe"] = "never"
    """持久化意图存在但结果未知时的恢复策略。"""

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(
            name=self.name,
            description=self.description,
            parameters=self.parameters.model_json_schema(),
        )
```

**错误处理契约**（写进 docstring）：

```python
# 对：工具失败抛异常，agent 捕获后转成 is_error=True 的工具结果
async def execute(self, call_id, params, signal=None, on_update=None) -> AgentToolResult:
    if not params.path.exists():
        raise FileNotFoundError(f"File not found: {params.path}")
    return AgentToolResult(content=[TextContent(content)], details={"size": len(content)})


# 错：模型无法区分「成功但内容说失败」和真失败
return AgentToolResult(content=[TextContent("File not found")], details={})
```

`is_error=True` 是给「不想抛异常但确实失败」的场景留的出口，两者映射到同一个错误通道。

### 4.3 事件类型（agent/types.py）

```python
AgentEvent: TypeAlias = (
    # Agent 生命周期
    AgentStart
    | AgentEnd
    # Turn 生命周期：一次 assistant 响应 + 其工具调用/结果
    | TurnStart
    | TurnEnd
    # 消息生命周期
    | MessageStart
    | MessageUpdate  # 仅 assistant
    | MessageEnd
    # 工具执行生命周期
    | ToolExecutionStart
    | ToolExecutionUpdate
    | ToolExecutionEnd
)


@dataclass(slots=True)
class AgentStart:
    type: Literal["agent_start"] = "agent_start"


@dataclass(slots=True)
class AgentEnd:
    """本次 run 的最后一个事件。

    注意：awaited 的 subscribe 监听器仍计入 run 的 settle。
    真正 idle 要等监听器完成。
    """

    messages: list[AgentMessage]
    type: Literal["agent_end"] = "agent_end"


@dataclass(slots=True)
class TurnEnd:
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    type: Literal["turn_end"] = "turn_end"


@dataclass(slots=True)
class MessageUpdate:
    message: AgentMessage
    """浅拷贝的快照，供状态使用。"""
    assistant_message_event: ai.AssistantMessageEvent
    """内含共享的 live partial，供实时渲染使用。"""
    type: Literal["message_update"] = "message_update"


@dataclass(slots=True)
class ToolExecutionEnd:
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool
    duration_ms: int | None = None
    type: Literal["tool_execution_end"] = "tool_execution_end"
```

### 4.4 AbortToken（agent/abort.py）

```python
import asyncio


class AbortToken:
    """
    自定义取消令牌，替代 asyncio.CancelledError。

    原因：CancelledError 继承 BaseException 且是 task 作用域的——
    在子任务里捕获它可能吞掉真正的取消信号。
    钩子需要一个能同步检查（token.aborted）并往下传的东西。
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def abort(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()
```

### 4.5 钩子（agent/hooks.py）

```python
@dataclass(slots=True)
class BeforeToolCallResult:
    block: bool = False
    reason: str = ""
    terminate: bool = False


@dataclass(slots=True)
class AfterToolCallResult:
    """
    逐字段替换，无深合并。

    - content 提供时替换整个内容数组
    - 提供 content 但未提供 structured_content 时，structured_content 被丢弃
      （它可能已不再匹配）
    - 省略的字段保持原值
    """

    content: list[TextContent | ImageContent] | None = None
    details: Any = None
    structured_content: Any = None
    is_error: bool | None = None
    usage: Usage | None = None
    terminate: bool | None = None


@dataclass(slots=True)
class BeforeToolCallContext:
    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: Any
    context: AgentContext


@dataclass(slots=True)
class AfterToolCallContext:
    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: Any
    result: AgentToolResult
    is_error: bool
    context: AgentContext


@dataclass(slots=True)
class TurnContext:
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    context: AgentContext
    new_messages: list[AgentMessage]


TurnDecision: TypeAlias = Literal["continue", "end"]

BeforeToolCall = Callable[
    [BeforeToolCallContext, "AbortToken | None"], Awaitable[BeforeToolCallResult | None]
]
AfterToolCall = Callable[
    [AfterToolCallContext, "AbortToken | None"], Awaitable[AfterToolCallResult | None]
]
FinishTurn = Callable[[TurnContext, "AbortToken | None"], Awaitable[TurnDecision | None]]
PrepareRequest = Callable[
    ["PrepareRequestContext", "AbortToken | None"], Awaitable["AgentRequestUpdate | None"]
]
PrepareNextTurn = Callable[[TurnContext], Awaitable["AgentLoopTurnUpdate | None"]]
GetSteeringMessages = Callable[[], Awaitable[list[AgentMessage]]]
GetFollowUpMessages = Callable[[], Awaitable[list[AgentMessage]]]
```

#### finish_turn 的语义（最易用错的钩子）

```
None        保持正常调度
"end"       立即结束 run，不轮询队列，不准备下一次请求
"continue"  保证还有一次 provider 请求。如果工具结果 / steering / follow-up
            已经会触发请求，就满足这个保证，不额外发；
            否则补一次 context-only 请求。

错误和中断是硬出口，其决策被忽略。

⚠️ 无条件返回 "continue" 会死循环，因为下一轮它还会跑。
```

#### prepare_request 的语义

```
每次对话请求前调用，包括第一次。此时待发消息已经 append 并 emit。
返回的 context / model / thinking_level 会替换本 run 后续请求的运行时值。

⚠️ 注意「后续请求」——不是只影响本次。契约要在类型和测试里表达。
该钩子不轮询队列。
```

### 4.6 Agent 状态（agent/agent.py）

```python
@dataclass
class AgentInitialState:
    system_prompt: str = ""
    model: Model | None = None
    thinking_level: ThinkingLevel = "off"
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)


class AgentState:
    """
    system_prompt 只读，从 transcript 重放。

    要改 prompt，追加一条 SystemMessage：
      - content  追加指令
      - sections 按名替换（None 删除）
    这样 prompt 有历史、可回放、支持增量更新。
    """

    def __init__(self, initial: AgentInitialState) -> None:
        self._messages: list[AgentMessage] = list(initial.messages)
        self._tools: list[AgentTool] = list(initial.tools)
        self.model = initial.model
        self.thinking_level = initial.thinking_level

        head = create_initial_system_message(
            initial.system_prompt,
            [t.declaration() for t in self._tools],
        )
        if head is not None and (not self._messages or self._messages[0].role != "system"):
            self._messages.insert(0, head)

        self.is_streaming = False
        self.streaming_message: AgentMessage | None = None
        self.pending_tool_calls: frozenset[str] = frozenset()
        self.error_message: str | None = None

    @property
    def system_prompt(self) -> str:
        """只读：重放 transcript。"""
        return get_current_system_prompt(self._messages)

    @property
    def messages(self) -> list[AgentMessage]:
        return self._messages

    @messages.setter
    def messages(self, value: list[AgentMessage]) -> None:
        self._messages = list(value)  # 拷贝顶层，防止外部持引用

    @property
    def tools(self) -> list[AgentTool]:
        return self._tools

    @tools.setter
    def tools(self, value: list[AgentTool]) -> None:
        self._tools = list(value)
```

**注意 `tools` 与 transcript 的关系**：

`state.tools` 是**可执行的**工具集，transcript 里的 system message 声明的是**模型可见的**工具集。每次请求前 diff 两者，有差异就追加一条 system message 告知模型。这样「声明」和「可执行」永远一致。

### 4.7 双队列（agent/agent.py）

```python
QueueMode: TypeAlias = Literal["all", "one_at_a_time"]


class PendingQueue:
    """
    steering 和 follow-up 各一个实例。

    steering：工具执行期间注入，用于「停，换个方向」
    follow-up：agent 本应停止后才注入，用于「顺便再做个 X」

    one_at_a_time 每次只投递最早一条，all 一次投递全部。
    模式在 run 期间可改。
    """

    def __init__(self, mode: QueueMode = "one_at_a_time") -> None:
        self._items: list[AgentMessage] = []
        self.mode = mode

    def enqueue(self, message: AgentMessage) -> None:
        self._items.append(message)

    def has_items(self) -> bool:
        return bool(self._items)

    def peek(self) -> list[AgentMessage]:
        return list(self._items) if self.mode == "all" else self._items[:1]

    def drain(self) -> list[AgentMessage]:
        drained = self.peek()
        del self._items[: len(drained)]
        return drained

    def clear(self) -> None:
        self._items.clear()
```

### 4.8 Agent 类（agent/agent.py）

```python
class Agent:
    """
    低层 loop 的有状态封装。

    拥有 transcript、发生命周期事件、执行工具、暴露队列 API。
    """

    def __init__(
        self,
        *,
        initial_state: AgentInitialState,
        stream_fn: StreamFn,
        convert_to_llm: ConvertToLlm | None = None,
        transform_context: TransformContext | None = None,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        finish_turn: FinishTurn | None = None,
        prepare_request: PrepareRequest | None = None,
        prepare_next_turn: PrepareNextTurn | None = None,
        get_api_key: Callable[[str], Awaitable[str | None]] | None = None,
        steering_mode: QueueMode = "one_at_a_time",
        follow_up_mode: QueueMode = "one_at_a_time",
        session_id: str | None = None,
        thinking_budgets: dict[str, int] | None = None,
        tool_execution: ToolExecutionMode = "parallel",
        max_retry_delay_ms: int | None = None,
    ) -> None: ...

    # ── 状态 ──
    @property
    def state(self) -> AgentState: ...

    # ── 队列控制 ──
    @property
    def steering_mode(self) -> QueueMode: ...
    @steering_mode.setter
    def steering_mode(self, mode: QueueMode) -> None: ...

    @property
    def follow_up_mode(self) -> QueueMode: ...

    def steer(self, message: AgentMessage) -> None:
        """在当前 assistant turn 结束后注入。"""

    def follow_up(self, message: AgentMessage) -> None:
        """在 agent 本应停止后注入。"""

    def clear_steering_queue(self) -> None: ...
    def clear_follow_up_queue(self) -> None: ...
    def clear_all_queues(self) -> None: ...
    def has_queued_messages(self) -> bool: ...
    def peek_queued_messages(self) -> list[AgentMessage]:
        """预览下一轮会取到的消息，不消费。"""

    # ── 生命周期 ──
    def subscribe(
        self,
        listener: Callable[[AgentEvent, AbortToken], Awaitable[None] | None],
    ) -> Callable[[], None]:
        """
        订阅生命周期事件。

        监听器按注册顺序 await，计入本次 run 的 settle。
        agent_end 是最后一个 emit 的事件，但 agent 要等所有
        agent_end 监听器完成才 idle。
        """

    async def prompt(
        self,
        message: str | AgentMessage | list[AgentMessage],
        images: list[ImageContent] | None = None,
    ) -> None:
        """开始一次新 prompt。运行中调用会抛错。"""

    async def continue_(self) -> None:
        """
        从当前 transcript 继续。最后一条必须是 user 或 toolResult。

        最后一条是 assistant 时：先尝试一个 steering 批次，
        再尝试一个 follow-up 批次，都空则抛错。
        """

    def abort(self) -> None: ...
    async def wait_for_idle(self) -> None:
        """等当前 run 及所有 awaited 监听器完成。"""

    def reset(self) -> None:
        """清空会话状态和队列，保留重放出的 prompt / 工具基线。"""
```

#### 内部：run 生命周期

```python
async def _run_with_lifecycle(self, executor: Callable[[AbortToken], Awaitable[None]]) -> None:
    if self._active_run is not None:
        raise RuntimeError("Agent is already processing.")

    token = AbortToken()
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    self._active_run = _ActiveRun(done, token)

    self._state.is_streaming = True
    self._state.streaming_message = None
    self._state.error_message = None
    try:
        await executor(token)
    except Exception as exc:
        await self._handle_run_failure(exc, token.aborted)
    finally:
        self._finish_run()


async def _handle_run_failure(self, error: Exception, aborted: bool) -> None:
    """把异常转成一个失败的 assistant 消息，走正常事件序列。

    这就是「错误编码在流里」在 Agent 层的体现——
    调用者不需要 try/except，只需检查 stop_reason。
    """
    failure = AssistantMessage(
        content=[TextContent("")],
        api=self._state.model.api if self._state.model else "unknown",
        provider=self._state.model.provider if self._state.model else "unknown",
        model=self._state.model.id if self._state.model else "unknown",
        usage=Usage(),
        stop_reason="aborted" if aborted else "error",
        error_message=str(error),
        timestamp=now_ms(),
    )
    await self._process_event(MessageStart(failure))
    await self._process_event(MessageEnd(failure))
    await self._process_event(TurnEnd(failure, []))
    await self._process_event(AgentEnd([failure]))
```

#### 内部：事件归约 + 监听器

```python
async def _process_event(self, event: AgentEvent) -> None:
    """先归约内部状态，再按顺序 await 监听器。"""
    match event:
        case MessageStart(message) | MessageUpdate(message=message):
            self._state.streaming_message = message
        case MessageEnd(message):
            self._state.streaming_message = None
            self._state.messages.append(message)
        case ToolExecutionStart(tool_call_id):
            self._state.pending_tool_calls = self._state.pending_tool_calls | {tool_call_id}
        case ToolExecutionEnd(tool_call_id):
            self._state.pending_tool_calls = self._state.pending_tool_calls - {tool_call_id}
        case TurnEnd(message=message):
            if message.role == "assistant" and message.error_message:
                self._state.error_message = message.error_message
        case AgentEnd():
            self._state.streaming_message = None

    token = self._active_run.token if self._active_run else None
    if token is None:
        raise RuntimeError("Agent listener invoked outside active run")

    for listener in self._listeners:  # 注册顺序，顺序 await
        result = listener(event, token)
        if inspect.isawaitable(result):
            await result
```

### 4.9 循环（agent/loop.py）

**双层循环**：外层处理 follow-up，内层处理 tool calls + steering。

```python
async def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: Callable[[AgentEvent], Awaitable[None]],
    signal: AbortToken | None,
    stream_fn: StreamFn,
) -> list[AgentMessage]:
    """带新 prompt 启动 loop。prompt 加入上下文并 emit 其事件。"""
    initial = declare_tool_changes(context, prompts)
    new_messages = list(initial)
    current = replace(context, messages=[*context.messages, *initial])

    await emit(AgentStart())
    await emit(TurnStart())
    for message in initial:
        await emit(MessageStart(message))
        await emit(MessageEnd(message))

    await _run_loop(current, new_messages, config, emit, signal, stream_fn)
    return new_messages


async def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: Callable[[AgentEvent], Awaitable[None]],
    signal: AbortToken | None,
    stream_fn: StreamFn,
) -> list[AgentMessage]:
    """从现有上下文继续，不添加新消息。用于重试。

    重要：context 最后一条必须能通过 convert_to_llm 转成 user 或
    toolResult，否则 provider 会拒绝请求。
    """
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if context.messages[-1].role == "assistant":
        raise ValueError("Cannot continue from message role: assistant")
    ...
```

#### 主循环

```python
async def _run_loop(current, new_messages, config, emit, signal, stream_fn) -> None:
    last_turn: TurnContext | None = None
    explicit_continue = False

    # 启动时先查一次 steering：用户可能在等待期间就输入了
    pending = await config.get_steering_messages() if config.get_steering_messages else []

    while True:  # 外层：follow-up
        has_more_tools = True

        while has_more_tools or pending:  # 内层：tools + steering
            prepared: list[AgentMessage] = []

            if last_turn is not None:
                update = (
                    await config.prepare_next_turn(last_turn) if config.prepare_next_turn else None
                )
                if update is not None:
                    current = update.context or current
                    prepared = update.messages or []
                    config = config.with_model_thinking(update)
                # prepare_next_turn 可能很慢（例如压缩）。补一次 steering 轮询，
                # 但仅当上一轮没取到——否则 one_at_a_time 会一轮投递两条。
                if not pending and config.get_steering_messages:
                    pending = await config.get_steering_messages()
                await emit(TurnStart())

            # 处理待发消息：注入工具声明增量
            for message in declare_tool_changes(current, [*prepared, *pending]):
                await emit(MessageStart(message))
                await emit(MessageEnd(message))
                current.messages.append(message)
                new_messages.append(message)
            pending = []

            request_update = await config.prepare_request(...) if config.prepare_request else None
            if request_update is not None:
                current = request_update.context or current
                config = config.with_model_thinking(request_update)

            message = await _stream_assistant_response(current, config, emit, signal, stream_fn)
            new_messages.append(message)

            # ── 硬出口：错误 / 中断 ──
            if message.stop_reason in ("error", "aborted"):
                last_turn = TurnContext(message, [], current, new_messages)
                if config.finish_turn:
                    await config.finish_turn(last_turn, signal)
                await emit(TurnEnd(message, []))
                await emit(AgentEnd(new_messages))
                return

            tool_calls = [c for c in message.content if c.type == "toolCall"]
            tool_results: list[ToolResultMessage] = []
            has_more_tools = False

            if tool_calls:
                # stop_reason == "length"：输出被 token 上限截断。
                # 流式 JSON 抢救解析可能产出「能过校验但内容被截断」的参数，
                # 全部判失败让模型重发。
                if message.stop_reason == "length":
                    batch = await _fail_truncated_tool_calls(tool_calls, emit)
                else:
                    batch = await execute_tool_calls(current, message, config, signal, emit)

                tool_results.extend(batch.messages)
                has_more_tools = not batch.terminate
                for result in tool_results:
                    current.messages.append(result)
                    new_messages.append(result)

            last_turn = TurnContext(message, tool_results, current, new_messages)
            decision = await config.finish_turn(last_turn, signal) if config.finish_turn else None
            await emit(TurnEnd(message, tool_results))

            if decision == "end":
                await emit(AgentEnd(new_messages))
                return

            explicit_continue = decision == "continue"
            pending = await config.get_steering_messages() if config.get_steering_messages else []
            if has_more_tools or pending:
                explicit_continue = False

        # ── agent 本应停止，检查 follow-up ──
        follow_ups = await config.get_follow_up_messages() if config.get_follow_up_messages else []
        if follow_ups:
            explicit_continue = False
            pending = follow_ups
            continue

        # 没有自然请求满足 continue 决策，补一次 context-only 请求
        if explicit_continue:
            explicit_continue = False
            continue

        break

    await emit(AgentEnd(new_messages))
```

#### 工具声明增量

```python
def declare_tool_changes(
    context: AgentContext,
    pending_messages: list[AgentMessage],
) -> list[AgentMessage]:
    """
    把「可执行工具集」与「transcript 声明的工具集」的差异，
    变成一条 system message 的 tools_added / tools_removed。

    有 pending system message 时，把它的工具字段当作意图，
    替换为「已提交 transcript」到「可执行集」的 delta，
    这样重放永远精确得到 context.tools。

    否则把新 system message 插到第一条非 system 消息之前。
    """
    pending = _last_system_in(pending_messages)
    baseline = _with_tool_changes(pending, NO_CHANGES) if pending else pending_messages
    changes = get_tool_state_changes(
        get_current_tools([*context.messages, *baseline]),
        [t.declaration() for t in context.tools or []],
    )
    unchanged = not changes.tools_added and not changes.tools_removed

    if pending:
        if unchanged and not pending.tools_added and not pending.tools_removed:
            return pending_messages
        return _replace_system(pending_messages, pending, changes)

    if unchanged:
        return pending_messages

    update = _with_tool_changes(SystemMessage(content="", timestamp=now_ms()), changes)
    index = _first_non_system_index(pending_messages)
    return [*pending_messages[:index], update, *pending_messages[index:]]
```

#### 流式 assistant 响应

```python
async def _stream_assistant_response(current, config, emit, signal, stream_fn) -> AssistantMessage:
    # 1. 上下文变换（AgentMessage[] -> AgentMessage[]）
    messages = current.messages
    if config.transform_context is not None:
        messages = await config.transform_context(messages, signal)

    # 2. 转成 LLM 消息（AgentMessage[] -> Message[]）
    llm_messages = await config.convert_to_llm(messages)

    # 3. 规范化上下文
    llm_context = normalize_context(Context(messages=llm_messages))

    # 4. 解析 API key（重要：支持会过期的 OAuth token）
    resolved_key = None
    if config.get_api_key and config.model:
        resolved_key = await config.get_api_key(config.model.provider)
    resolved_key = resolved_key or config.api_key

    response = stream_fn(
        config.model,
        llm_context,
        StreamOptions(
            signal=signal,
            api_key=resolved_key,
            reasoning=config.reasoning,
        ),
    )

    async def final() -> AssistantMessage:
        message = await response.result()
        message.thinking_level = config.reasoning or "off"
        return message

    partial: AssistantMessage | None = None
    added_partial = False

    async for event in response:
        match event.type:
            case "start":
                partial = event.partial
                current.messages.append(partial)
                added_partial = True
                await emit(MessageStart(copy.copy(partial)))

            case (
                "text_start"
                | "text_delta"
                | "text_end"
                | "thinking_start"
                | "thinking_delta"
                | "thinking_end"
                | "toolcall_start"
                | "toolcall_delta"
                | "toolcall_end"
            ):
                if partial is not None:
                    partial = event.partial
                    current.messages[-1] = partial
                    # message 是浅拷贝（状态用稳定快照）
                    # assistant_message_event 保留原 partial（实时渲染用）
                    await emit(MessageUpdate(copy.copy(partial), event))

            case "done" | "error":
                final_message = await final()
                if added_partial:
                    current.messages[-1] = final_message
                else:
                    current.messages.append(final_message)
                    await emit(MessageStart(copy.copy(final_message)))
                await emit(MessageEnd(final_message))
                return final_message

    # 流意外结束（无终止事件）
    final_message = await final()
    if added_partial:
        current.messages[-1] = final_message
    else:
        current.messages.append(final_message)
        await emit(MessageStart(copy.copy(final_message)))
    await emit(MessageEnd(final_message))
    return final_message
```

**partial 的拷贝边界**：`partial` 是共享可变对象，随每个 delta 增长。下游需要稳定快照时必须浅拷贝，否则存下来的 message 会被后续 delta 改写。

### 4.10 工具执行（agent/tools.py）

```python
@dataclass(slots=True)
class ToolBatch:
    messages: list[ToolResultMessage]
    terminate: bool


async def execute_tool_calls(ctx, assistant_message, config, signal, emit) -> ToolBatch:
    tool_calls = [c for c in assistant_message.content if c.type == "toolCall"]

    # 任一工具声明 sequential -> 整批退化为串行
    has_sequential = any(
        (tool := _find_tool(ctx, tc.name)) is not None and tool.execution_mode == "sequential"
        for tc in tool_calls
    )
    if config.tool_execution == "sequential" or has_sequential:
        return await _execute_sequential(ctx, assistant_message, tool_calls, config, signal, emit)
    return await _execute_parallel(ctx, assistant_message, tool_calls, config, signal, emit)
```

#### 并行模式（默认）

```python
async def _execute_parallel(ctx, assistant_message, tool_calls, config, signal, emit) -> ToolBatch:
    """
    顺序 preflight，并发执行，两种排序：

    - tool_execution_end 按完成顺序 emit（UI 即时看到哪个先跑完）
    - toolResult 消息按 assistant 源顺序 emit 并持久化
      （provider 要求 tool_call 与 toolResult 一一对应）
    """
    thunks: list[Callable[[], Awaitable[FinalizedToolCall]]] = []

    # ── 阶段 1：顺序 preflight ──
    for tool_call in tool_calls:
        await emit(ToolExecutionStart(tool_call.id, tool_call.name, tool_call.arguments))

        prep = await prepare_tool_call(ctx, assistant_message, tool_call, config, signal)

        if prep.kind == "immediate":
            # 未知工具 / 参数校验失败 / before_tool_call 阻止
            finalized = FinalizedToolCall(tool_call, prep.result, is_error=True)
            await _emit_tool_execution_end(finalized, emit)
            thunks.append(_already_done(finalized))
            if signal is not None and signal.aborted:
                break
            continue

        thunks.append(
            partial(
                _run_and_finalize,
                prep,
                ctx,
                assistant_message,
                config,
                signal,
                emit,
            )
        )
        if signal is not None and signal.aborted:
            break

    # ── 阶段 2：并发执行 ──
    finalized_calls = await asyncio.gather(*(t() for t in thunks))

    # ── 阶段 3：按源顺序 emit 结果消息 ──
    messages: list[ToolResultMessage] = []
    for finalized in finalized_calls:
        message = create_tool_result_message(finalized)
        await emit(MessageStart(message))
        await emit(MessageEnd(message))
        messages.append(message)

    terminate = bool(finalized_calls) and all(f.result.terminate for f in finalized_calls)
    return ToolBatch(messages, terminate)
```

**为什么必须保留两种排序**：这是很容易在简化时丢掉、但后期无法补回的细节。`tool_execution_end` 按完成顺序是 UI 需求；`toolResult` 按源顺序是协议需求。

#### preflight

```python
async def prepare_tool_call(
    ctx, assistant_message, tool_call, config, signal
) -> Prepared | Immediate:
    """
    解析 -> 参数兼容转换 -> schema 校验 -> before_tool_call。

    任何一步失败都返回 immediate 错误结果，不抛异常。
    """
    tool = _find_tool(ctx, tool_call.name)
    if tool is None:
        return Immediate(error_result(f"Tool {tool_call.name} not found"))

    try:
        prepared_call = _prepare_arguments(tool, tool_call)
        validated = _validate_arguments(tool, prepared_call)

        if config.before_tool_call is not None:
            result = await config.before_tool_call(
                BeforeToolCallContext(assistant_message, tool_call, validated, ctx),
                signal,
            )
            if signal is not None and signal.aborted:
                return Immediate(error_result("Operation aborted"))
            if result is not None and result.block:
                blocked = error_result(result.reason or "Tool execution was blocked")
                if result.terminate:
                    blocked.terminate = True
                return Immediate(blocked)

        if signal is not None and signal.aborted:
            return Immediate(error_result("Operation aborted"))

        return Prepared(tool_call, tool, validated)
    except Exception as exc:
        return Immediate(error_result(str(exc)))
```

#### finalize

```python
async def _run_and_finalize(
    prep, ctx, assistant_message, config, signal, emit
) -> FinalizedToolCall:
    executed = await _execute_prepared(prep, signal, _update_sink(prep.tool_call, emit))
    return await _finalize(executed, prep, ctx, assistant_message, config, signal, emit)


async def _execute_prepared(prep, signal, on_update) -> ExecutedToolCall:
    """计时 + 忽略 settle 后的 on_update。"""
    updates: list[asyncio.Task] = []
    accepting = True
    started = time.perf_counter()

    def elapsed() -> int:
        return round((time.perf_counter() - started) * 1000)

    try:
        result = await prep.tool.execute(
            prep.tool_call.id,
            prep.args,
            signal,
            lambda partial_result: (
                updates.append(asyncio.create_task(_await_maybe(on_update(partial_result))))
                if accepting
                else None
            ),
        )
        duration_ms = elapsed()
        accepting = False
        await asyncio.gather(*updates, return_exceptions=True)
        return ExecutedToolCall(result, result.is_error, duration_ms)
    except Exception as exc:
        duration_ms = elapsed()
        accepting = False
        await asyncio.gather(*updates, return_exceptions=True)
        return ExecutedToolCall(error_result(str(exc)), True, duration_ms)


async def _finalize(
    executed, prep, ctx, assistant_message, config, signal, emit
) -> FinalizedToolCall:
    """
    应用 after_tool_call 覆盖。逐字段替换，无深合并。

    结构化内容未随内容一起替换时丢弃——它可能已不再匹配。
    """
    result = executed.result
    is_error = executed.is_error

    if config.after_tool_call is not None:
        try:
            override = await config.after_tool_call(
                AfterToolCallContext(
                    assistant_message, prep.tool_call, prep.args, result, is_error, ctx
                ),
                signal,
            )
            if override is not None:
                structured = (
                    override.structured_content
                    if override.structured_content is not None
                    else (None if override.content is not None else result.structured_content)
                )
                result = replace(
                    result,
                    content=override.content if override.content is not None else result.content,
                    details=override.details if override.details is not None else result.details,
                    usage=override.usage if override.usage is not None else result.usage,
                    terminate=override.terminate
                    if override.terminate is not None
                    else result.terminate,
                    structured_content=structured,
                )
                if override.is_error is not None:
                    is_error = override.is_error
        except Exception as exc:
            result = error_result(str(exc))
            is_error = True

    finalized = FinalizedToolCall(prep.tool_call, result, is_error, executed.duration_ms)
    await _emit_tool_execution_end(finalized, emit)
    return finalized
```

#### 截断消息的工具调用

```python
async def _fail_truncated_tool_calls(tool_calls, emit) -> ToolBatch:
    """
    stop_reason == "length" 时全部判失败。

    理由：流式 JSON 抢救解析可能产出「能通过校验但内容被截断」的参数，
    执行它们不安全。报告为错误让模型重发。
    """
    messages: list[ToolResultMessage] = []
    for tool_call in tool_calls:
        await emit(ToolExecutionStart(tool_call.id, tool_call.name, tool_call.arguments))
        finalized = FinalizedToolCall(
            tool_call,
            error_result(
                f'Tool call "{tool_call.name}" was not executed: the response hit the '
                "output token limit, so its arguments may be truncated. "
                "Re-issue the tool call with complete arguments."
            ),
            is_error=True,
        )
        await _emit_tool_execution_end(finalized, emit)
        message = create_tool_result_message(finalized)
        await emit(MessageStart(message))
        await emit(MessageEnd(message))
        messages.append(message)
    return ToolBatch(messages, terminate=False)
```

### 4.11 嵌套工具调用（agent/tools.py）

```python
async def run_tool_call(
    tool_call: ToolCall,
    *,
    tools: Sequence[AgentTool],
    assistant_message: AssistantMessage,
    context: AgentContext,
    signal: AbortToken | None = None,
    on_update: Callable | None = None,
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
) -> AgentToolCallOutcome:
    """
    走与模型发起调用完全相同的步骤：参数准备、schema 校验、
    before_tool_call、执行、after_tool_call。

    不发事件、不加消息。工具调用其他工具时用它，
    这样钩子（例如权限检查）对嵌套调用同样生效。

    永不因工具失败而 reject：未知工具、校验失败、被阻止、
    抛出的异常都返回 is_error=True。
    """
```

---

## 5. Layer 3: session（会话与编排）

职责：会话持久化、投影、压缩、run 级别的编排（重试、边界）。

### 5.1 条目类型（session/entries.py）

```python
@dataclass(frozen=True, slots=True)
class EntryBase:
    id: str
    parent_id: str | None
    timestamp: int


@dataclass(frozen=True, slots=True)
class HeaderEntry(EntryBase):
    """会话头。第一条 entry。"""

    cwd: str = ""
    version: int = 1
    type: Literal["header"] = "header"


@dataclass(frozen=True, slots=True)
class MessageEntry(EntryBase):
    message: AgentMessage = None
    type: Literal["message"] = "message"


@dataclass(frozen=True, slots=True)
class ModelChangeEntry(EntryBase):
    provider: str = ""
    model_id: str = ""
    type: Literal["model_change"] = "model_change"


@dataclass(frozen=True, slots=True)
class ThinkingLevelChangeEntry(EntryBase):
    level: str = "off"
    type: Literal["thinking_level_change"] = "thinking_level_change"


@dataclass(frozen=True, slots=True)
class CompactionEntry(EntryBase):
    """
    摘要替换更早的消息。

    first_kept_entry_id：从该 entry 起的消息保留。
                       None 表示自持压缩（不保留任何前置消息）。
    原始 entry 永不删除——这是 /tree 能回到任意历史点的前提。
    """

    summary: str = ""
    first_kept_entry_id: str | None = None
    details: Any = None
    usage: Usage | None = None
    type: Literal["compaction"] = "compaction"


@dataclass(frozen=True, slots=True)
class BranchSummaryEntry(EntryBase):
    """离开一个分支时，把该分支摘要附加到进入的分支。"""

    summary: str = ""
    from_entry_id: str = ""
    type: Literal["branch_summary"] = "branch_summary"


@dataclass(frozen=True, slots=True)
class CustomEntry(EntryBase):
    """不参与 LLM 上下文，仅供扩展持久化状态。"""

    custom_type: str = ""
    data: Any = None
    type: Literal["custom"] = "custom"


@dataclass(frozen=True, slots=True)
class CustomMessageEntry(EntryBase):
    """参与上下文，且可渲染进 UI。"""

    custom_type: str = ""
    content: str | list[ContentBlock] = ""
    display: bool = True
    details: Any = None
    type: Literal["custom_message"] = "custom_message"


@dataclass(frozen=True, slots=True)
class ContextEditEntry(EntryBase):
    """替换某条 entry 在投影中的呈现，不改原始 entry。"""

    target_id: str = ""
    replacement: Any = None
    type: Literal["context_edit"] = "context_edit"


SessionEntry: TypeAlias = (
    HeaderEntry
    | MessageEntry
    | ModelChangeEntry
    | ThinkingLevelChangeEntry
    | CompactionEntry
    | BranchSummaryEntry
    | CustomEntry
    | CustomMessageEntry
    | ContextEditEntry
)
```

**`id` + `parent_id` 从第一天就有**。即使当前只做线性会话，格式里带上这两个字段，以后加 `/tree`、`/fork`、`/clone` 不需要迁移已有会话文件。

### 5.2 存储（session/store.py）

JSONL，追加写：

```jsonl
{"type":"header","id":"s1","parent_id":null,"timestamp":1700000000000,"cwd":"/proj","version":1}
{"type":"message","id":"e1","parent_id":"s1","timestamp":1700000000001,"message":{"role":"user","content":"hi"}}
{"type":"message","id":"e2","parent_id":"e1","timestamp":1700000000002,"message":{"role":"assistant",...}}
{"type":"compaction","id":"e3","parent_id":"e2","timestamp":1700000000003,"summary":"...","first_kept_entry_id":"e2"}
```

```python
class SessionStore:
    """JSONL 追加写。追加是 O(1)，重写只在压缩 / 迁移时发生。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, SessionEntry] = {}
        self._order: list[str] = []
        self._leaf_id: str | None = None
        self._fp: TextIO | None = None

    @classmethod
    def create(cls, path: Path, cwd: str) -> SessionStore:
        """创建新会话，写入 header。"""

    @classmethod
    def open(cls, path: Path) -> SessionStore:
        """
        打开已有会话。

        容错：忽略解析失败的尾部行（进程被杀时的半行写入）。
        忽略未知 type 的 entry（前向兼容）。
        """

    def append(self, entry: SessionEntry) -> SessionEntry:
        """追加一条 entry，更新 leaf。"""

    def get(self, entry_id: str) -> SessionEntry | None: ...

    @property
    def leaf_id(self) -> str | None: ...

    def set_leaf(self, entry_id: str | None) -> None:
        """导航到某个 entry（/tree 的基础）。"""

    def all_entries(self) -> dict[str, SessionEntry]: ...
```

### 5.3 投影（session/projection.py）

```python
@dataclass(frozen=True, slots=True)
class SessionProjection:
    """
    active branch 的线性视图。

    entries  投影后参与的 entry（含 compaction 摘要的合成 entry）
    messages 转换后的 AgentMessage 列表
    """

    entries: tuple[SessionEntry, ...]
    messages: tuple[AgentMessage, ...]
    leaf_id: str | None


def build_projection(
    all_entries: dict[str, SessionEntry],
    leaf_id: str | None,
) -> SessionProjection:
    """
    1. 从 leaf 沿 parent 回溯到 root，反转得到分支顺序
    2. 应用最近的 compaction：摘要替换更早的消息，
       first_kept_entry_id 起的消息保留
    3. 应用 context_edit：替换目标 entry 的呈现

    原始 entry 永不删除。
    """
    branch = _walk_branch(all_entries, leaf_id)
    branch = _apply_context_edits(branch)
    compaction = _find_latest_compaction(branch)

    if compaction is None:
        return SessionProjection(tuple(branch), tuple(_to_messages(branch)), leaf_id)

    if compaction.first_kept_entry_id is None:
        kept: list[SessionEntry] = []
    else:
        kept_start = _index_of(branch, compaction.first_kept_entry_id)
        kept = branch[kept_start:] if kept_start is not None else []

    summary_message = _summary_to_message(compaction)
    entries = (compaction, *kept)
    messages = (summary_message, *_to_messages(kept))
    return SessionProjection(entries, messages, leaf_id)
```

#### 投影的合成消息

压缩摘要、分支摘要都需要变成一条模型可见的消息。约定：**用 user role 包裹**，避免伪造 assistant 消息破坏协议。

```python
def _summary_to_message(compaction: CompactionEntry) -> AgentMessage:
    """
    摘要变成一条 user 消息。

    不要用 assistant role：那会让 provider 认为模型说过这些话，
    且可能破坏 tool_call / toolResult 的配对。
    """
    return UserMessage(
        content=f"<compaction_summary>\n{compaction.summary}\n</compaction_summary>",
        timestamp=compaction.timestamp,
    )
```

### 5.4 SessionManager（session/manager.py）

```python
class SessionManager:
    """在 SessionStore 之上提供「追加消息 + 读投影」的领域接口。"""

    def __init__(self, store: SessionStore) -> None:
        self._store = store
        self._message_entry_ids: dict[int, str] = {}  # id(message) -> entry_id

    @property
    def session_id(self) -> str: ...

    def get_branch(self) -> list[SessionEntry]:
        """当前 active branch 的 entry 列表。"""

    def build_projection(self) -> SessionProjection:
        """构建当前投影。压缩和请求投影都调它。"""

    def append_message(self, message: AgentMessage) -> str:
        """追加一条消息 entry，返回 entry id。"""

    def append_custom_entry(self, custom_type: str, data: Any = None) -> str: ...
    def append_custom_message_entry(
        self,
        custom_type: str,
        content: str | list[ContentBlock],
        display: bool,
        details: Any = None,
    ) -> str: ...
    def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str | None,
        details: Any = None,
        usage: Usage | None = None,
    ) -> str: ...
    def append_branch_summary(self, summary: str, from_entry_id: str) -> str: ...
    def append_model_change(self, provider: str, model_id: str) -> str: ...

    def entry_id_for_message(self, message: AgentMessage) -> str | None:
        """
        反查消息对应的 entry id。
        先查缓存（按对象身份），再沿分支线性搜索，
        最后按投影索引回推。
        """
```

### 5.5 压缩（session/compaction.py）

#### 触发条件

```
context_tokens > context_window - reserve_tokens

默认 reserve_tokens = 16384（给模型响应留空间）
默认 keep_recent_tokens = 20000（保留的最近历史）
```

#### 触发时机

| 时机 | 位置 | 说明
|---|---|---|
| 阈值 | `prepare_next_turn` | 工具结果已追加、下次请求未开始。唯一稳定窗口 |
| 阈值 | 新用户 prompt 前 | 避免开局就溢出 |
| 溢出 | run 结束后 | provider 报溢出错误时的一次 compact-and-retry |
| 手动 | 命令 | `/compact [instructions]` |

#### 流程

```python
@dataclass(slots=True)
class CompactionResult:
    summary: str
    first_kept_entry_id: str | None
    tokens_before: int
    tokens_after: int
    usage: Usage | None = None


async def compact(
    *,
    projection: SessionProjection,
    model: Model,
    stream_fn: StreamFn,
    keep_recent_tokens: int,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
    signal: AbortToken | None = None,
) -> CompactionResult:
    """
    1. 反向遍历已 finalize 的投影，累积 token 估算直到 keep_recent_tokens，
       找到切点
    2. 提取上次保留边界（或会话开始）到切点的消息
    3. 用结构化格式调 LLM 摘要（有旧摘要时作为迭代上下文传入）
    4. 返回 CompactionResult，由调用者 append CompactionEntry

    摘要请求禁用 prompt-cache 写入：一次性 prompt 不太可能被复用。
    """
    cut_index = _find_cut_point(projection, keep_recent_tokens)
    to_summarize = projection.messages[:cut_index]
    first_kept_entry_id = _entry_id_at(projection, cut_index)

    summary = await _generate_summary(
        to_summarize=to_summarize,
        model=model,
        stream_fn=stream_fn,
        custom_instructions=custom_instructions,
        previous_summary=previous_summary,
        signal=signal,
    )
    return CompactionResult(summary, first_kept_entry_id, ...)
```

#### 摘要的结构化格式

用固定的 section 结构，而不是自由文本。这样多轮压缩（摘要的摘要）能保持信息密度：

```
## 目标
用户想要什么

## 已完成
- 具体改动（文件路径 + 做了什么）

## 进行中
当前状态、未完成的步骤

## 关键决策
- 决定了什么，为什么（避免后续推翻）

## 待办
- 明确的下一步

## 文件操作累积记录
- 创建：path/to/new.py
- 修改：path/to/existing.py（改了什么）
- 读取：path/to/reference.md
```

最后一项「文件操作累积记录」是迭代压缩的关键——它跨压缩轮次累积，避免「第三次压缩后忘了改过哪些文件」。

### 5.6 编排（session/orchestration.py）

**Agent 只负责一轮 turn；run 级别的恢复策略在这里。**

```python
class AgentSession:
    """
    Agent 之上的编排层。

    agent_end 不是终点：低层循环只管 turn，
    run 级别的重试、压缩、边界续跑都在这里。
    """

    def __init__(
        self,
        *,
        agent: Agent,
        manager: SessionManager,
        settings: Settings,
        extensions: ExtensionRegistry,
        model_runtime: ModelRuntime,
        cwd: str,
    ) -> None:
        self.agent = agent
        self._manager = manager
        self._settings = settings
        self._extensions = extensions

        # 内部监听器：持久化、扩展事件、压缩、重试
        self._unsubscribe_agent = agent.subscribe(self._handle_agent_event)

        # 五层适配器（顺序重要）
        self._install_tool_hooks()  # tool_call / tool_result
        self._install_request_projection()  # prepare_request
        self._install_next_turn_refresh()  # prepare_next_turn
        self._install_boundary_hooks()  # finish_turn -> turn_end
        self._install_forced_prompt_projection()

    # ── 公开事件订阅（UI 用） ──
    def subscribe(self, listener: Callable[[SessionEvent], None]) -> Callable[[], None]: ...

    async def prompt(self, text: str, *, images: list[ImageContent] | None = None, **kw) -> None:
        messages = await self._prepare_prompt(text, images, kw)
        await self._run(messages)
```

#### 外层 run 循环

```python
async def _run(self, messages: list[AgentMessage]) -> None:
    self._record_selection()
    self._pending_tool_names.clear()
    self._is_run_active = True
    try:
        await self.agent.prompt(messages)
        while not self._abort_requested:
            if await self._handle_post_run():
                if self._abort_requested:
                    break
                await self.agent.continue_()
                continue
            if self._abort_requested or not await self._run_before_settle_boundary():
                break
            if self._abort_requested:
                break
            await self.agent.continue_()
    finally:
        self._failed_response = None
        self._run_prompt_options = None
        self._flush_pending_custom_messages()
        await self._emit_settled()
```

#### agent_end 之后的恢复策略

```python
async def _handle_post_run(self) -> bool:
    """
    agent_end 之后、agent_before_settle 之前。

    返回 True 表示需要再跑一轮。
    """
    message = self._last_assistant_message
    tool_results = self._last_tool_results
    self._last_assistant_message = None
    self._last_tool_results = []

    if self._abort_requested:
        return False
    if message is None:
        return self.agent.has_queued_messages()

    # 1. 可重试错误 -> 重试
    if self._is_retryable_error(message) and await self._prepare_retry(message):
        return not self._abort_requested

    # 2. 上下文溢出 -> 压缩重试（只尝试一次）
    if await self._check_compaction(message, final=True, tool_results=tool_results):
        return not self._abort_requested

    # 3. agent_end 监听器新入队的消息需要新一轮
    return not self._abort_requested and self.agent.has_queued_messages()
```

#### 压缩挂在 prepare_next_turn

```python
def _install_next_turn_refresh(self) -> None:
    previous = self.agent.prepare_next_turn

    async def prepare_next_turn(turn: TurnContext) -> AgentLoopTurnUpdate | None:
        # 1. 压缩检查
        context = await self._compact_before_next_response(turn.context)
        # 2. 前一个钩子
        update = await previous(turn) if previous else None
        next_context = update.context if update and update.context else context
        # 3. 重建 system prompt（用当前 active tools）
        options = normalize_prompt_options(
            selected_tools=self.get_active_tool_names(),
            **self._base_prompt_options,
        )
        update_message = self._prepare_prompt_and_tool_loadout(options, next_context.messages)
        self._run_prompt_options = options
        return AgentLoopTurnUpdate(
            context=replace(next_context, tools=list(self.agent.state.tools)),
            messages=[
                *(update.messages if update else []),
                *([update_message] if update_message else []),
            ],
        )

    self.agent.prepare_next_turn = prepare_next_turn


async def _compact_before_next_response(self, context: AgentContext) -> AgentContext:
    projection = self._manager.build_projection()
    model = self.agent.state.model
    if model is None or not self._exceeds_compaction_threshold(model, projection):
        return replace(context, messages=list(projection.messages))
    await self._run_auto_compaction("threshold", final=False)
    return replace(context, messages=list(self._manager.build_projection().messages))
```

#### 会话投影挂在 prepare_request

```python
def _install_request_projection(self) -> None:
    previous = self.agent.prepare_request

    async def prepare_request(request: PrepareRequestContext, signal: AbortToken | None):
        failed = self._failed_response
        self._failed_response = None

        async def prepare() -> tuple[AgentRequestUpdate | None, AgentContext, SessionProjection]:
            projection = self._manager.build_projection()
            canonical = replace(
                request.context,
                messages=list(projection.messages),
                # 消息声明模型可见的 loadout；context.tools 保持可执行实现
                tools=list(self.agent.state.tools),
            )
            update = (
                await previous(
                    replace(
                        request,
                        context=canonical,
                        model=self.agent.state.model,
                        thinking_level=self.agent.state.thinking_level,
                    ),
                    signal,
                )
                if previous
                else None
            )
            return update, (update.context if update and update.context else canonical), projection

        update, context, projection = await prepare()
        model = update.model if update and update.model else self.agent.state.model
        thinking_level = (
            update.thinking_level
            if update and update.thinking_level
            else self.agent.state.thinking_level
        )

        # 虚拟模型路由（可选）
        if model is not None and is_virtual_model(model):
            route = await self._route_virtual_model(model, context, failed, signal)
            if route is not None:
                model, thinking_level = route.model, route.thinking_level
                if self._exceeds_compaction_threshold(model, projection):
                    await self._run_auto_compaction("threshold", final=False)
                    update, context, projection = await prepare()

        return AgentRequestUpdate(context=context, model=model, thinking_level=thinking_level)

    self.agent.prepare_request = prepare_request
```

### 5.7 边界（session/boundary.py）

**扩展不直接写会话，只提交草稿；由这里校验后原子提交。**

```python
@dataclass(slots=True)
class BoundaryContextPreview:
    """
    handler 能看到「如果我提交这些草稿，下个请求会长什么样」。
    """

    context_entries: tuple[SessionEntry, ...]
    context_messages: tuple[AgentMessage, ...]
    llm_messages: tuple[Message, ...]
    pending_messages: tuple[AgentMessage, ...]
    can_continue: bool


@dataclass(slots=True)
class BoundaryState:
    entries: list[SessionBoundaryDraft]
    continue_: bool
    context: BoundaryContextPreview
    outcome: Literal["completed", "aborted", "error"]


@dataclass(slots=True)
class BoundaryResult:
    entries: list[SessionBoundaryDraft] | None = None
    continue_: bool | None = None


# 草稿类型
SessionBoundaryDraft: TypeAlias = (
    CustomEntryDraft | CustomMessageEntryDraft | ContextEditEntryDraft | CompactionEntryDraft
)
```

#### 派发

```python
async def emit_boundary(
    *,
    base_event: BoundaryBaseEvent,
    build_context: Callable[[list[SessionBoundaryDraft]], Awaitable[BoundaryContextPreview]],
    extensions: ExtensionRegistry,
    on_error: Callable[[str, str, str], None],
) -> BoundaryDispatchResult:
    """
    三点语义：

    1. entries 和 continue 是累积的，每个 handler 看到前一个的草稿
    2. context 每步重建，handler 能看到提交后的投影
    3. 任一步重建失败 -> 整体回滚（空 entries + continue=False），
       一个坏 handler 不会污染会话
    """
    entries: list[SessionBoundaryDraft] = []
    should_continue = False
    context = await build_context(entries)
    valid = True

    for extension, handler in extensions.snapshot(base_event.type):
        event = replace(base_event, entries=entries, continue_=should_continue, context=context)
        try:
            result = await handler(event, extensions.create_context())
            if result is not None:
                if result.entries is not None:
                    entries = result.entries
                if result.continue_ is not None:
                    should_continue = result.continue_
        except Exception as exc:
            on_error(extension.path, base_event.type, str(exc))

        try:
            context = await build_context(entries)
            valid = True
        except Exception as exc:
            valid = False
            on_error(extension.path, base_event.type, f"Invalid boundary entries: {exc}")

    if valid:
        return BoundaryDispatchResult(entries, should_continue, context, valid=True)
    return BoundaryDispatchResult([], False, context, valid=False)
```

#### turn_end 挂在 finish_turn

```python
def _install_boundary_hooks(self) -> None:
    previous = self.agent.finish_turn

    async def finish_turn(turn: TurnContext, signal: AbortToken | None) -> TurnDecision | None:
        self._boundary_dispatched.add(id(turn.message))
        extension_continue = await self._dispatch_turn_end_boundary(turn.message, turn.tool_results)
        previous_decision = await previous(turn, signal) if previous else None

        # end 优先级最高：扩展无法用 continue 覆盖内层的 end，
        # 防止扩展把本该结束的 run 无限延长
        if previous_decision == "end":
            return previous_decision
        if extension_continue or previous_decision == "continue":
            return "continue"
        return None

    self.agent.finish_turn = finish_turn


async def _dispatch_turn_end_boundary(
    self,
    message: AssistantMessage,
    tool_results: list[ToolResultMessage],
) -> bool:
    self._last_outcome = (
        "aborted"
        if message.stop_reason == "aborted"
        else "error"
        if message.stop_reason == "error"
        else "completed"
    )

    if not self._extensions.has_handlers("turn_end"):
        return False

    message_entry_id = self._manager.entry_id_for_message(message)
    if message_entry_id is None:
        self._extensions.emit_error(
            "<boundary>", "turn_end", "turn_end could not resolve the persisted assistant entry ID"
        )
        return False

    tool_result_entry_ids = [
        entry_id
        for result in tool_results
        if (entry_id := self._manager.entry_id_for_message(result)) is not None
    ]

    boundary = await emit_boundary(
        base_event=TurnEndEvent(
            turn_index=self._turn_index,
            message=message,
            tool_results=tool_results,
            message_entry_id=message_entry_id,
            tool_result_entry_ids=tool_result_entry_ids,
            outcome=self._last_outcome,
        ),
        build_context=lambda entries: self._build_boundary_context(entries, "turn_end"),
        extensions=self._extensions,
        on_error=self._extensions.emit_error,
    )
    self._commit_boundary_drafts(boundary.entries)

    if boundary.continue_ and not (await self._build_boundary_context([], "turn_end")).can_continue:
        self._extensions.emit_error(
            "<boundary>",
            "turn_end",
            "turn_end requested continuation without runnable model context",
        )
        return False
    return boundary.continue_
```

#### agent_before_settle 是真正的终点线

```python
async def _run_before_settle_boundary(self) -> bool:
    if not self._extensions.has_handlers("agent_before_settle"):
        return self.agent.has_queued_messages()

    self._is_before_settle = True
    self._abort_during_before_settle = False
    try:
        result = await emit_boundary(
            base_event=AgentBeforeSettleEvent(outcome=self._last_outcome),
            build_context=lambda entries: self._build_boundary_context(
                entries, "agent_before_settle"
            ),
            extensions=self._extensions,
            on_error=self._extensions.emit_error,
        )
        self._commit_boundary_drafts(result.entries)
        self._flush_pending_custom_messages()

        final_context = await self._build_boundary_context([], "agent_before_settle")
        if self._abort_during_before_settle:
            return False

        should_continue = result.continue_ or self.agent.has_queued_messages()
        if should_continue and not final_context.can_continue:
            if result.continue_:
                self._extensions.emit_error(
                    "<boundary>",
                    "agent_before_settle",
                    "agent_before_settle requested continuation without runnable model context",
                )
            return False
        return should_continue
    finally:
        self._is_before_settle = False
```

### 5.8 会话事件（session/events.py）

```python
SessionEvent: TypeAlias = (
    # Agent 事件原样透传
    AgentEvent
    # 包装：加派生字段
    | AgentEndWrapped
    # 终点
    | AgentSettled
    # 会话级
    | QueueUpdate
    | CompactionStart
    | CompactionEnd
    | EntryAppended
    | SessionInfoChanged
    | ThinkingLevelChanged
    # 恢复级
    | AutoRetryStart
    | AutoRetryEnd
    | BashExecutionUpdate
)


@dataclass(slots=True)
class AgentEndWrapped:
    messages: list[AgentMessage]
    will_retry: bool
    """
    派生字段，不在 Agent 层。

    UI 靠它决定显示「错误」还是「重试中」——同一个 agent_end，语义不同。
    """
    type: Literal["agent_end"] = "agent_end"


@dataclass(slots=True)
class AgentSettled:
    """run 完全静止，不会再有自动重试 / 压缩 / 队列续跑。"""

    aborted: bool
    type: Literal["agent_settled"] = "agent_settled"


@dataclass(slots=True)
class QueueUpdate:
    steering: list[str]
    follow_up: list[str]
    type: Literal["queue_update"] = "queue_update"


@dataclass(slots=True)
class CompactionStart:
    reason: Literal["manual", "threshold", "overflow"]
    type: Literal["compaction_start"] = "compaction_start"


@dataclass(slots=True)
class CompactionEnd:
    reason: Literal["manual", "threshold", "overflow"]
    result: CompactionResult | None
    aborted: bool
    will_retry: bool
    error_message: str | None = None
    type: Literal["compaction_end"] = "compaction_end"
```

#### will_retry 的计算

```python
def _will_retry_after_agent_end(self, event: AgentEnd) -> bool:
    if self._abort_requested:
        return False
    settings = self._settings.get_retry_settings()
    if not settings.enabled or self._retry_attempt >= settings.max_retries:
        return False
    # 倒序找最后一条 assistant 消息
    for message in reversed(event.messages):
        if message.role == "assistant":
            return self._is_retryable_error(message)
    return False
```

---

## 6. Layer 4: app（应用层）

职责：system prompt 组装、内置工具、扩展系统、接口模式。

### 6.1 System prompt 组装（app/prompt.py）

**分 section，每节独立可替换。** 这样 prompt 可以增量更新，且扩展能只改一节。

```python
@dataclass
class BuildPromptOptions:
    custom_prompt: str | None = None
    """替换默认 preamble。"""
    force_system_prompt: str | None = None
    """完全替换整个 prompt（扩展用）。此时无 sections。"""
    selected_tools: list[str] = field(default_factory=lambda: ["read", "bash", "edit", "write"])
    hidden_tools: list[str] = field(default_factory=list)
    """声明被请求省略的工具（只能通过其他工具到达）。"""
    tool_snippets: dict[str, str] = field(default_factory=dict)
    tool_guidelines: dict[str, list[str]] = field(default_factory=dict)
    prompt_guidelines: list[str] = field(default_factory=list)
    append_system_prompt: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    context_files: list[ContextFile] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)


SECTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def build_prompt_sections(options: BuildPromptOptions) -> dict[str, str]:
    """
    返回有序 section。preamble 无 tag，其余包成 <name>...</name>。

    section 顺序：
      preamble, tools, rules, docs, addendum, project_context, skills, cwd, ...custom
    """
    for name in options.sections:
        if not SECTION_NAME_RE.match(name) or name == "preamble":
            raise ValueError(f"Invalid system prompt section name: {name}")

    sections: dict[str, str] = {}
    if options.custom_prompt:
        sections["preamble"] = options.custom_prompt
    else:
        sections["preamble"] = (
            "You are an expert coding assistant operating inside mini-pi, "
            "a coding agent harness. You help users by reading files, "
            "executing commands, editing code, and writing new files."
        )
        sections["tools"] = _render_tools_section(options)
        sections["rules"] = _build_rules(options)
        sections["docs"] = _render_docs_section()

    if options.append_system_prompt:
        sections["addendum"] = options.append_system_prompt
    if options.context_files:
        sections["project_context"] = _render_project_context(options.context_files)
    if options.skills:
        sections["skills"] = _format_skills(options.skills)
    sections["cwd"] = options.cwd.replace("\\", "/")

    for name, content in options.sections.items():
        if content:
            sections[name] = content

    # 除 preamble 外全部包 tag，让模型能把后续更新对应到原始节
    out = {"preamble": sections["preamble"]}
    for name, content in sections.items():
        if name != "preamble":
            out[name] = f"<{name}>\n{content}\n</{name}>"
    return out
```

#### 规则生成

```python
def _build_rules(options: BuildPromptOptions) -> str:
    """
    去重 + 固定顺序，保证 prompt 稳定（利于缓存）。
    """
    rules: list[str] = []
    seen: set[str] = set()

    def add(rule: str) -> None:
        normalized = rule.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            rules.append(normalized)

    selected = options.selected_tools
    has_shell = "bash" in selected or "powershell" in selected
    has_search = any(t in selected for t in ("grep", "find", "ls"))
    if has_shell and not has_search:
        add("Use bash for file operations like ls, rg, find")

    for name in selected:
        for rule in options.tool_guidelines.get(name, ()):
            add(rule)
    for rule in options.prompt_guidelines:
        add(rule)

    add("Be concise in your responses")
    add("Show file paths clearly when working with files")
    return "\n".join(f"- {r}" for r in rules)
```

#### section diff（增量更新）

```python
def diff_prompt_sections(
    previous: dict[str, str | None],
    current: dict[str, str],
) -> dict[str, str | None] | None:
    """
    返回 patch：新增 / 修改的 section 带新内容，删除的带 None。
    无变化返回 None。

    这是「prompt 增量更新」的核心——只发差量，
    支持 mid-conversation system message 的模型能保住缓存前缀。
    """
    patch: dict[str, str | None] = {}
    for name, text in current.items():
        if previous.get(name) != text:
            patch[name] = text
    for name in previous:
        if name not in current:
            patch[name] = None
    return patch or None
```

#### prompt 状态的两种形态

```python
def build_prompt_state(options: BuildPromptOptions) -> tuple[str, dict[str, str] | None]:
    """
    返回 (content, sections)。

    force_system_prompt 时：content 是不透明全文，sections 为 None。
    否则：content 为空，sections 携带结构化 prompt。
    """
    if options.force_system_prompt is not None:
        return options.force_system_prompt, None
    return "", build_prompt_sections(options)
```

### 6.2 内置工具（app/tools/）

每个工具有两种形态：可执行的 `AgentTool`，和带 prompt 元数据的 `ToolDefinition`。

```python
# app/tools/read.py
class ReadParams(BaseModel):
    path: str = Field(description="File path to read")
    offset: int | None = Field(default=None, description="Line offset to start from")
    limit: int | None = Field(default=None, description="Maximum number of lines")
    model_config = ConfigDict(extra="forbid")


DEFAULT_MAX_BYTES = 256 * 1024
DEFAULT_MAX_LINES = 2000


def create_read_tool(cwd: str, options: ReadToolOptions | None = None) -> AgentTool:
    opts = options or ReadToolOptions()

    async def execute(
        call_id: str,
        params: ReadParams,
        signal: AbortToken | None = None,
        on_update: Callable | None = None,
    ) -> AgentToolResult:
        path = resolve_path(cwd, params.path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if path.is_dir():
            raise IsADirectoryError(f"Path is a directory: {path}")

        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        start = max(0, (params.offset or 1) - 1)
        selected = lines[start:]
        if params.limit is not None:
            selected = selected[: params.limit]

        body = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(selected))
        truncated = truncate_head(body, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES)

        return AgentToolResult(
            content=[TextContent(truncated.text)],
            details={
                "path": str(path),
                "lines": len(selected),
                "total_lines": len(lines),
                "truncated": truncated.truncated,
            },
        )

    return AgentTool(
        name="read",
        label="Read File",
        description="Read a file from the filesystem",
        parameters=ReadParams,
        execute=execute,
        # read 是并行安全的
        execution_mode=None,
    )


def create_read_tool_definition(cwd: str, options: ReadToolOptions | None = None) -> ToolDefinition:
    """扩展用的形态：带 prompt snippet 和 guidelines。"""
    return ToolDefinition(
        tool=create_read_tool(cwd, options),
        prompt_snippet="Read file contents",
        prompt_guidelines=[
            "Always read a file before editing it",
            "Use offset/limit for large files instead of reading everything",
        ],
        exposure="direct",
    )
```

#### 工具注册表

```python
# app/tools/__init__.py
ToolName: TypeAlias = Literal["read", "write", "edit", "bash", "grep", "find", "ls"]

def create_coding_tools(cwd: str, options: ToolsOptions | None = None) -> list[AgentTool]:
    """read + bash + edit + write，最常用的四件套。"""
    opts = options or ToolsOptions()
    return [
        create_read_tool(cwd, opts.read),
        create_bash_tool(cwd, opts.bash),
        create_edit_tool(cwd, opts.edit),
        create_write_tool(cwd, opts.write),
    ]

def create_read_only_tools(cwd: str, options: ToolsOptions | None = None) -> list[AgentTool]:
    """read + grep + find + ls，用于只读场景（例如审查）。"""

def create_all_tools(cwd: str, options: ToolsOptions | None = None) -> dict[ToolName, AgentTool]:
```

#### write / edit 必须串行

```python
def create_write_tool(cwd: str, options: WriteToolOptions | None = None) -> AgentTool:
    return AgentTool(
        name="write",
        ...,
        # 写文件必须串行：并发写同一文件会损坏内容
        execution_mode="sequential",
    )
```

`execution_mode="sequential"` 会让**整个批次**串行，不只是这一个工具。这是刻意的：如果一批里有 write，其他工具的读取结果也可能失效，串行更安全。

### 6.3 扩展系统（app/extensions/）

#### 注册表

```python
# app/extensions/registry.py
@dataclass
class Extension:
    path: str
    handlers: dict[str, list[Callable]] = field(default_factory=dict)
    tools: dict[str, ToolDefinition] = field(default_factory=dict)
    commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    shortcuts: dict[str, ShortcutHandler] = field(default_factory=dict)
    flags: dict[str, FlagDefinition] = field(default_factory=dict)
    providers: dict[str, ProviderRegistration] = field(default_factory=dict)
    message_renderers: dict[str, MessageRenderer] = field(default_factory=dict)
    entry_renderers: dict[str, EntryRenderer] = field(default_factory=dict)
    tool_renderers: list[ToolRendererResolver] = field(default_factory=list)


class ExtensionRegistry:
    def __init__(self) -> None:
        self._extensions: list[Extension] = []
        self._active = True

    def snapshot(self, event_type: str) -> list[tuple[Extension, Callable]]:
        """
        快照式遍历：派发前拷贝 handler 列表，
        派发中的注册 / 注销不影响当前轮。
        """
        out: list[tuple[Extension, Callable]] = []
        for extension in self._extensions:
            out.extend((extension, h) for h in extension.handlers.get(event_type, ()))
        return out

    def has_handlers(self, event_type: str) -> bool:
        return any(extension.handlers.get(event_type) for extension in self._extensions)

    def emit_error(self, extension_path: str, event: str, error: str) -> None:
        """错误隔离出口。UI 决定如何呈现。"""
```

#### 派发：四类语义

```python
# app/extensions/dispatch.py
class Dispatcher:
    """
    按返回值语义分类，而非按触发时机。

    分类决定两件事：错误策略、handler 之间如何传递数据。
    """

    # ── 1. 通知：忽略返回值，错误隔离 ──
    async def emit(self, event: ExtensionEvent) -> None:
        for extension, handler in self._registry.snapshot(event.type):
            try:
                result = handler(event, self._registry.create_context())
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self._registry.emit_error(extension.path, event.type, str(exc))

    # ── 2. 链式转换 ──
    async def emit_context(self, messages: list[AgentMessage]) -> list[AgentMessage]:
        """
        两阶段：

        阶段 1（context）：handler 只看到对话，不含 system message。
          返回后 Pi 恢复 prompt 和工具声明——扩展不该也不能弄丢它们。

        阶段 2（context_with_system）：handler 看到完整 transcript，
          返回值原样发送。此时 handler 拥有 prompt 和工具声明，风险自负。
        """
        current = copy.deepcopy(messages)

        for extension, handler in self._registry.snapshot("context"):
            try:
                visible = [m for m in current if m.role != "system"]
                snapshot = list(visible)
                result = await _maybe_await(
                    handler(ContextEvent(messages=visible), self._registry.create_context())
                )

                returned = (
                    result.messages
                    if result and result.messages is not None
                    else (None if _same(visible, snapshot) else visible)
                )
                if returned is None:
                    continue
                current = _restore_system_messages(current, snapshot, returned)
            except Exception as exc:
                self._registry.emit_error(extension.path, "context", str(exc))

        for extension, handler in self._registry.snapshot("context_with_system"):
            try:
                had_leading = bool(current) and current[0].role == "system"
                result = await _maybe_await(
                    handler(
                        ContextWithSystemEvent(messages=current), self._registry.create_context()
                    )
                )
                current = result.messages if result and result.messages is not None else current
                if had_leading and (not current or current[0].role != "system"):
                    self._registry.emit_error(
                        extension.path,
                        "context_with_system",
                        "Handler removed the leading system message; the request has no "
                        "prompt or initial tool declarations. Keep it at index 0 or replace "
                        "a dropped prefix with get_current_system_message().",
                    )
            except Exception as exc:
                self._registry.emit_error(extension.path, "context_with_system", str(exc))

        return current

    async def emit_tool_result(self, event: ToolResultEvent) -> ToolResultOverride | None:
        """累积式：每个 handler 看到前一个的改动。"""
        current = replace(event)
        modified = False

        for extension, handler in self._registry.snapshot("tool_result"):
            try:
                result = await _maybe_await(handler(current, self._registry.create_context()))
                if result is None:
                    continue
                if result.content is not None:
                    current.content = result.content
                    # 替换了 content 却没给新的 structured_content，
                    # 说明旧的已不再匹配，必须丢弃
                    if result.structured_content is None:
                        current.structured_content = None
                    modified = True
                if result.details is not None:
                    current.details = result.details
                    modified = True
                if result.structured_content is not None:
                    current.structured_content = result.structured_content
                    modified = True
                if result.is_error is not None:
                    current.is_error = result.is_error
                    modified = True
            except Exception as exc:
                self._registry.emit_error(extension.path, "tool_result", str(exc))

        return _to_override(current) if modified else None

    # ── 3. 就地修改：返回值忽略 ──
    async def emit_before_provider_headers(
        self, headers: dict[str, str | None]
    ) -> dict[str, str | None]:
        for extension, handler in self._registry.snapshot("before_provider_headers"):
            try:
                # handler 直接改 headers；None 值表示删除该 header
                await _maybe_await(
                    handler(
                        BeforeProviderHeadersEvent(headers=headers), self._registry.create_context()
                    )
                )
            except Exception as exc:
                self._registry.emit_error(extension.path, "before_provider_headers", str(exc))
        return headers

    # ── 4. 短路 / 取消：fail-closed ──
    async def emit_tool_call(self, event: ToolCallEvent) -> ToolCallResult | None:
        """
        唯一不做 try/except 的派发。

        权限检查失败必须拒绝执行，不能放行。扩展抛错时由上层转成
        「Extension failed, blocking execution」。
        """
        result: ToolCallResult | None = None
        for _extension, handler in self._registry.snapshot("tool_call"):
            handler_result = await _maybe_await(handler(event, self._registry.create_context()))
            if handler_result is not None:
                result = handler_result
                if result.block:
                    return result  # 立即返回，后续 handler 不跑
        return result
```

#### system message 恢复

```python
def _restore_system_messages(
    current: list[AgentMessage],
    visible: list[AgentMessage],
    returned: list[AgentMessage],
) -> list[AgentMessage]:
    """
    若返回的与传入的逐项相同 -> 返回原列表，保留所有 system message
      （支持 mid-conversation system message 的模型能保住缓存前缀）

    若不同 -> 取当前 prompt 作为唯一前导 system message 拼在前面。
      防止扩展做剪枝或从压缩摘要切片时丢掉 prompt 和工具声明。
    """
    if _same(returned, visible):
        return current
    head = get_current_system_message(current)
    return [head, *returned] if head is not None else returned
```

#### 扩展 API

```python
# app/extensions/types.py
class ExtensionAPI(Protocol):
    # ── 事件订阅 ──
    def on(self, event: str, handler: Callable) -> Callable[[], None]: ...

    # ── 注册 ──
    def register_tool(self, tool: ToolDefinition) -> None: ...
    def register_command(self, name: str, options: CommandOptions) -> None: ...
    def register_shortcut(self, key: str, options: ShortcutOptions) -> None: ...
    def register_flag(self, name: str, options: FlagOptions) -> None: ...
    def register_provider(self, name: str, options: ProviderRegistration) -> None: ...
    def register_message_renderer(self, custom_type: str, renderer: MessageRenderer) -> None: ...
    def register_entry_renderer(self, custom_type: str, renderer: EntryRenderer) -> None: ...
    def register_tool_renderer(self, resolver: ToolRendererResolver) -> None: ...

    # ── 动作 ──
    def send_message(
        self,
        message: CustomMessageDraft,
        *,
        trigger_turn: bool = False,
        deliver_as: Literal["steer", "follow_up", "next_turn"] | None = None,
    ) -> None: ...

    def send_user_message(
        self,
        content: str | list[ContentBlock],
        *,
        deliver_as: Literal["steer", "follow_up"] | None = None,
        expand_prompt_templates: bool = False,
    ) -> None:
        """总是触发一轮。"""

    def append_entry(self, custom_type: str, data: Any = None) -> None:
        """追加自定义 entry，不参与 LLM 上下文。"""

    async def exec(
        self, command: str, args: list[str], options: ExecOptions | None = None
    ) -> ExecResult: ...

    # ── 会话控制 ──
    def get_active_tools(self) -> list[str]: ...
    def set_active_tools(self, names: list[str]) -> None: ...
    def get_all_tools(self) -> list[ToolInfo]: ...
    def get_settings(self) -> Settings: ...
    async def set_model(self, model: Model) -> bool:
        """未配置鉴权时返回 False。"""

    def get_thinking_level(self) -> ThinkingLevel: ...
    def set_thinking_level(self, level: ThinkingLevel) -> None: ...

    # ── 会话元数据 ──
    def set_session_name(self, name: str) -> None: ...
    def set_label(self, entry_id: str, label: str | None) -> None: ...


class ExtensionToolContext(ExtensionContext, Protocol):
    """
    工具 execute() 拿到的上下文。

    execute_tool 走和模型发起调用完全相同的校验、钩子、权限检查。
    这是嵌套调用（codemode 脚本调工具）的关键——
    否则权限钩子会被绕过。
    """

    tools: Sequence[AgentTool]

    async def execute_tool(
        self,
        name: str,
        args: Any,
        options: ExecuteToolOptions | None = None,
    ) -> AgentToolCallOutcome:
        """
        嵌套调用 id 形如 "callingId/n"。
        不进 transcript，只在该调用工具的结果消息上留 nested_calls 记录。
        永不因工具失败而 reject。
        """
```

#### 事件清单（第一天定义，可逐步实现）

```
session 类：
  project_trust, resources_discover, session_start, session_info_changed,
  session_before_switch, session_before_fork, session_before_compact,
  session_compact, session_compact_failed, session_shutdown, session_before_tree, session_tree

上下文 / provider 类：
  context, context_with_system, before_provider_request, before_provider_headers,
  after_provider_response, provider_stream_event

agent 类：
  before_agent_start, agent_start, agent_end, agent_before_settle, agent_settled,
  turn_start, turn_end, message_start, message_update, message_end,
  tool_execution_start, tool_execution_update, tool_execution_end,
  model_select, thinking_level_select, tool_call, tool_result,
  user_bash, input, ui_prompt_start, ui_prompt_end
```

### 6.4 接口模式（app/modes/）

#### wire 格式（app/wire.py）

```python
def to_wire(event: SessionEvent) -> dict[str, Any]:
    """
    剥离累积快照。

    partial 随每个 delta 携带完整消息至今的内容，一条 2000 token 的回复
    会产生 O(n^2) 带宽。wire 格式只留增量。

    但 toolcall_start 的 id 和 name 只在 partial 里，需要提升到顶层——
    这两个字段大小恒定，可以安全重复发送。
    """
    if event.type != "message_update":
        return _serialize(event)

    inner = event.assistant_message_event
    payload = {k: v for k, v in _serialize(inner).items() if k != "partial"}

    if inner.type == "toolcall_start":
        tool_call = inner.partial.content[inner.content_index]
        if tool_call is None or tool_call.type != "toolCall":
            raise ValueError(
                f"toolcall_start content at index {inner.content_index} is not a tool call"
            )
        payload["id"] = tool_call.id
        payload["tool_name"] = tool_call.name

    return {
        "type": "message_update",
        "usage": _serialize(event.message.usage),
        "assistant_message_event": payload,
    }
```

#### JSON 模式

```python
async def run_json_mode(session: AgentSession) -> int:
    unsub_events = session.subscribe(lambda e: write_raw_stdout(json.dumps(to_wire(e)) + "\n"))
    # 背压挂在 agent 层 -> 会被 await -> stdout 拥塞时 agent 循环自动减速
    unsub_backpressure = session.agent.subscribe(lambda e, tok: wait_for_stdout_drain())
    try:
        await session.prompt(initial_message)
        return 0
    finally:
        unsub_events()
        unsub_backpressure()
```

#### stdout 接管

```python
# app/output_guard.py
_takeover: _Takeover | None = None


def take_over_stdout() -> None:
    """
    把 sys.stdout 重定向到 stderr，只允许 write_raw_stdout 写真正的 stdout。

    否则扩展或依赖库的 print() 会打断 JSONL 流。
    WebUI 场景对应的是：日志走独立 logger，不要写进事件通道。
    """
    global _takeover
    if _takeover is not None:
        return
    _takeover = _Takeover(real_stdout=sys.stdout, real_stderr=sys.stderr)
    sys.stdout = sys.stderr


def restore_stdout() -> None:
    global _takeover
    if _takeover is None:
        return
    sys.stdout = _takeover.real_stdout
    _takeover = None


def write_raw_stdout(text: str) -> None:
    """串行化写入 + 重试瞬时错误。"""
    global _tail
    _tail = _tail.then(lambda: _write_chunk(text))


async def _write_chunk(text: str) -> None:
    while True:
        try:
            real = _takeover.real_stdout if _takeover else sys.stdout
            real.write(text)
            real.flush()
            return
        except (BlockingIOError, InterruptedError):
            await asyncio.sleep(0.01)


async def wait_for_stdout_drain() -> None:
    while True:
        tail = _tail
        await tail
        if tail is _tail:
            return
```

---

## 7. 关键接缝清单

这张表是本文档的核心。左列是接缝，右列是「缺了它会怎样」。

| # | 接缝 | 缺失后果 |
|---|---|---|
| 1 | `AgentMessage` / `Message` 分离 + `convert_to_llm` 边界 | UI-only 消息无法存在；加「压缩提示」需要重构消息管道 |
| 2 | `system_prompt` 只读，从 transcript 重放 | prompt 无历史、不可回放；中途改 prompt 会打断缓存前缀 |
| 3 | 错误编码进流，异常只表编程错误 | 错误处理分叉成两套；每个调用点都要 try/except |
| 4 | `EventStream` 的 `is_complete` / `extract_result` 参数化 | 每层写一套流实现；`result()` 与迭代无法分离 |
| 5 | 双队列 + `one_at_a_time` / `all` 模式 | steering 与 follow-up 语义混淆；无法做「打断」 |
| 6 | 4 个钩子的精确位置 | 扩展点位置错了，后期加功能要改循环 |
| 7 | `finish_turn` 的 continue / end 语义 | 无法表达「还要再问一次」；或写成死循环 |
| 8 | session entry 带 `id` + `parent_id` | 加会话树需要迁移已有文件 |
| 9 | 投影是派生视图，原始 entry 不删 | 加压缩、分支摘要都要改存储格式 |
| 10 | 扩展注册表 + 快照式派发 | 派发中改注册表会崩溃；handler 顺序不确定 |
| 11 | 钩子按语义分类（含 fail-closed 那类） | 权限检查出错时放行，安全漏洞 |
| 12 | 边界草稿 + 预览 + 原子提交 | 扩展直接写会话会与并发运行的消息流打架 |
| 13 | `SessionEvent` 独立于 `AgentEvent` | UI 需要自己算 `will_retry`、自己拼队列状态 |
| 14 | wire 格式剥离 `partial` | 流式带宽 O(n^2) |
| 15 | 背压作为独立订阅 | 慢消费者拖垮内存，或无法反压 |
| 16 | 上下文原语放 ai 层（重放 / 估算 / 溢出） | 写 CLI 工具、evals、批处理脚本时要重新实现 |
| 17 | 压缩 / 投影靠钩子挂载，不塞进 loop | agent 层无法复用；压缩算法换不掉 |
| 18 | 工具分两层（机制在 agent，实现在 app） | 库用户被迫接受整套内置工具 |
| 19 | 事件类型逐层增加，不合并 | UI 层暴露到 provider 细节（partial 共享语义等） |

---

## 8. Python 工程约定

### 8.1 取消：不要直接用 asyncio.CancelledError

```python
class AbortToken:
    """
    CancelledError 继承 BaseException 且是 task 作用域的——
    在子任务里捕获它可能吞掉真正的取消信号。
    钩子需要一个能同步检查（token.aborted）并往下传的东西。
    """
```

### 8.2 消息用 dataclass，工具参数用 Pydantic

| 用途 | 类型 | 理由 |
|---|---|---|
| 消息、事件 | `@dataclass(slots=True)` | partial 每个 delta 拷贝一次，必须便宜 |
| 工具参数 | Pydantic `BaseModel` | `model_json_schema()` 直接产出 provider 需要的 schema |
| 配置 | `@dataclass` | 不需要校验 |
| 会话 entry | `@dataclass(frozen=True, slots=True)` | 不可变，可安全共享 |

### 8.3 类型检查

`pyright --strict` 或 `mypy --strict`。对齐 pi 的 strict TypeScript 标准。

`AgentMessage` 这类联合配合 `Literal` 判别字段，`match` 语句能获得完整穷尽性检查。

### 8.4 并发

- 工具并行天然是 IO 并发，`asyncio.gather` 足够
- CPU 密集工具走 `loop.run_in_executor`
- 不要用 `asyncio.to_thread` 做需要取消的操作（无法中断）

### 8.5 测试策略

三层，全部不依赖网络：

```python
# 1. 纯函数测试：loop 用 faux provider
async def test_tool_call_produces_two_turns():
    faux = FauxProvider(
        [
            FauxResponse.tool_call("read", {"path": "a.txt"}),
            FauxResponse.text("Done."),
        ]
    )
    agent = Agent(initial_state=..., stream_fn=faux.stream)
    events = await collect_events(agent.prompt("read a.txt"))
    assert [e.type for e in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "message_start",
        "message_end",
        "turn_end",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert len(faux.requests) == 2


# 2. 投影测试：纯数据
def test_compaction_replaces_earlier_messages():
    entries = build_entries([...])
    projection = build_projection(entries, leaf_id="e10")
    assert projection.messages[0].role == "user"  # 摘要包裹成 user 消息
    assert len(projection.messages) == 4


# 3. 派发测试：注册假扩展
async def test_tool_call_block_short_circuits():
    registry = ExtensionRegistry()
    registry.register(blocking_extension(block_on="bash"))
    result = await Dispatcher(registry).emit_tool_call(
        ToolCallEvent(tool_name="bash", tool_call_id="1", input={})
    )
    assert result.block
```

### 8.6 分层强制

```python
def test_layering():
    """用 AST 断言依赖方向。"""
    rules = {
        "ai": (),
        "agent": ("ai",),
        "session": ("ai", "agent"),
        "app": ("ai", "agent", "session"),
        "ui": ("ai", "agent", "session", "app"),
    }
    for package, allowed in rules.items():
        for path in (ROOT / "mini_pi" / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                module = _imported_module(node)
                if module and module.startswith("mini_pi."):
                    target = module.split(".")[1]
                    assert target in allowed, f"{path} 不得 import {module}"
```

---

## 9. 实施顺序

每个阶段产出**完整的层**，不是残缺的层。

### 阶段 1：provider + core loop

产出：
- `ai/`：types、stream、transcript、estimate、overflow、faux provider、一个真实 provider
- `agent/`：types、stream、hooks、loop、tools、abort

验收：
- faux provider 跑通「工具调用 -> 第二轮」全链路
- 覆盖分支：工具调用、截断（`stop_reason == "length"`）、错误、中断
- 并行工具执行：`tool_execution_end` 按完成顺序，`toolResult` 按源顺序

### 阶段 2：Agent 类 + 钩子

产出：
- `agent/agent.py`：状态、双队列、subscribe

验收：
- 4 个钩子在各分支下位置正确
- `finish_turn` 三种返回值（None / "end" / "continue"）的语义测试
- steering 与 follow-up 在 `one_at_a_time` 和 `all` 下的行为
- `agent_end` 后 awaited 监听器仍计入 settle

### 阶段 3：会话层

产出：
- `session/`：entries、store、projection、manager

验收：
- JSONL 往返一致
- 从任意 leaf 能重建投影
- compaction entry 正确替换更早消息，原始 entry 仍可访问

### 阶段 4：编排 + 边界

产出：
- `session/orchestration.py`：外层 run 循环、重试、压缩检查
- `session/boundary.py`：草稿机制
- `session/events.py`

验收：
- 重试、压缩、边界续跑都能用 faux provider 确定性复现
- 非法边界草稿整体回滚
- `agent_before_settle` 的 abort 能取消续跑

### 阶段 5：扩展系统

产出：
- `app/extensions/`：registry、dispatch、types、loader

验收：
- 四类语义各有测试
- 错误隔离（通知 / 转换类）与 fail-closed（`tool_call`）行为正确
- `emit_context` 的 system message 恢复
- 快照式派发：派发中注册新 handler 不影响当前轮

### 阶段 6：应用层

产出：
- `app/prompt.py`：system prompt 分段 + diff
- `app/tools/`：内置工具
- `app/output_guard.py`

验收：
- prompt diff 只发差量
- `write` / `edit` 触发整批串行
- 端到端 CLI 可用

### 阶段 7：接口

产出：
- `app/wire.py`、`app/modes/`

验收：
- JSONL 流可被外部程序消费
- 剥离 `partial` 后带宽符合预期
- stdout 接管生效（扩展 print 不污染流）

### 阶段 8：UI

产出：
- `ui/tui/` 或 `ui/web/`：只订阅 `SessionEvent`

验收：
- 不 import `agent` 以下任何包
- 背压生效（慢消费者不会撑爆内存）

到阶段 7 结束，功能上接近 pi 的骨架；阶段 8 之后是加法。

---

## 10. 与 pi 的差异

### 10.1 建议简化的

| 项 | pi | mini pi | 理由 |
|---|---|---|---|
| 模型目录 | 生成式 `models.generated.ts` | 手写 TOML + 运行时覆盖 | Python 无对应构建步骤；目录更新频率不高 |
| 虚拟模型 | 一等公民 | 阶段 4 之后做 | 复杂度高；`prepare_request` 返回模型覆盖已能覆盖大部分场景 |
| 缓存预热 | 独立 `CacheWarmer` | 阶段 8 之后 | 纯优化，不影响架构 |
| 供应商归因头 | `provider-attribution.ts` | 不需要 | pi 用于自己的服务统计 |
| 自持压缩（`first_kept_entry_id: None`） | 支持 | 第一版不支持 | 边界情况；加它需要额外投影分支 |
| 会话树 UI（`/tree` `/fork` `/clone`） | 完整 | 存储层支持，UI 后补 | 格式已带 `parent_id`，加 UI 不需迁移 |

### 10.2 建议加强的

**`prepare_request` 的契约要在类型上表达清楚。**

pi 的注释说「returned context, model, and thinking level replace the runtime values for this and later requests in the run」，这个「for later requests」容易被忽略。

Python 版：
- 返回类型明确为 `AgentRequestUpdate`
- docstring 强调「影响本 run 后续所有请求」
- 加一条测试断言「连续两次请求都用了更新后的模型」

**分层用测试强制。** pi 靠 code review 维持依赖方向；Python 版用 AST 断言自动化。

---

## 11. 快速参考

### 11.1 一次 run 的完整时序

```
input -> before_agent_start -> agent.prompt
  |
  |-- 外层 run 循环（session） ------------------------------
  |   |-- 内层 turn 循环（agent） ------------------------  |
  |   |   prepare_next_turn  <- 压缩检查 + 重建 prompt      |
  |   |   turn_start                                        |
  |   |   待发消息 + 工具声明增量 -> message_start/end       |
  |   |   prepare_request    <- 会话投影 + 模型路由          |
  |   |   transform_context  <- context 事件                |
  |   |   convert_to_llm                                    |
  |   |   stream_fn          <- before_provider_headers     |
  |   |                         before_provider_request     |
  |   |                         [provider HTTP]              |
  |   |                         after_provider_response      |
  |   |                         provider_stream_event        |
  |   |   工具批次            <- tool_call (可阻止) -> 执行   |
  |   |                            -> tool_result            |
  |   |   finish_turn         <- turn_end 边界               |
  |   |---------------------------------------------------- |
  |   agent_end                                             |
  |   _handle_post_run      <- 重试 / 压缩 / 队列            |
  |   agent_before_settle 边界                              |
  |-------------------------------------------------------- |
  agent_settled
```

**关键点：`agent_end` 不是终点。** 它之后还有自动重试、上下文溢出恢复、压缩、队列续跑，然后才是 `agent_before_settle`（最后可行动边界），最后 `agent_settled`（仅通知）。

### 11.2 钩子四类语义速查

| 语义 | 事件 | 返回值作用 | 失败策略 |
|---|---|---|---|
| 纯通知 | `agent_start`、`agent_end`、`turn_start`、`message_start`、`message_update`、`tool_execution_*`、`session_compact`、`after_provider_response`、`agent_settled` | 忽略 | fail-open |
| 链式转换 | `context`、`context_with_system`、`before_provider_request`、`message_end`、`tool_result` | 每个 handler 看到前一个输出 | fail-open |
| 就地修改 | `before_provider_headers`、`before_agent_start` | 直接改 event 对象 | fail-open |
| 短路 / 取消 | `tool_call`（`block`）、`session_before_*`（`cancel`）、`input` | 一旦生效立即停止后续 handler | **fail-closed** |
| 边界草稿 | `turn_end`、`agent_before_settle` | `{entries, continue}` | 整体回滚 |

### 11.3 消息与类型速查

| 层 | 消息类型 | 事件类型 | 终止事件 |
|---|---|---|---|
| ai | `Message`（4 种 role） | `AssistantMessageEvent` | `done` / `error` |
| agent | `AgentMessage`（+ 自定义） | `AgentEvent` | `agent_end` |
| session | `AgentMessage` | `SessionEvent` | `agent_settled` |

### 11.4 文件尺寸参考（pi 对应模块）

| pi 模块 | 大小 | mini pi 对应 |
|---|---|---|
| `agent/src/agent-loop.ts` | 29.5 KB | `agent/loop.py` + `agent/tools.py` |
| `agent/src/agent.ts` | 19.7 KB | `agent/agent.py` |
| `agent/src/types.ts` | 21.2 KB | `agent/types.py` + `agent/hooks.py` |
| `coding-agent/src/core/agent-session.ts` | 160 KB | `session/orchestration.py` |
| `coding-agent/src/core/session-manager.ts` | 63.8 KB | `session/manager.py` + `session/store.py` |
| `coding-agent/src/core/compaction/compaction.ts` | 35.5 KB | `session/compaction.py` |
| `coding-agent/src/core/extensions/runner.ts` | 51.7 KB | `app/extensions/dispatch.py` |
| `coding-agent/src/core/extensions/types.ts` | 83.5 KB | `app/extensions/types.py` |
| `coding-agent/src/core/system-prompt.ts` | 10.2 KB | `app/prompt.py` |
| `ai/src/utils/transcript.ts` | ~10 KB | `ai/transcript.py` |
| `ai/src/utils/estimate.ts` | ~4 KB | `ai/estimate.py` |
| `ai/src/utils/overflow.ts` | ~5 KB | `ai/overflow.py` |
| `ai/src/utils/event-stream.ts` | 4.4 KB | `ai/stream.py` |

`agent-session.ts` 是 pi 最大的单文件（160 KB）。Python 版应拆成 `orchestration.py` + `boundary.py` + `events.py`，不要照抄成单文件。
