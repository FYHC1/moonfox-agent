# moonfox-agent-core

通用 Agent 核心，项目内仅依赖 `moonfox-ai`，工具参数使用 Pydantic 2。公开接口：`moonfox_agent.api`。Python ≥ 3.12。

阶段 A 提供最小顺序工具循环、消息/事件类型、单向取消令牌、显式运行预算和独立队列原语。`RunOptions(max_turns_per_run=...)` 每次运行必传，不读取配置或固定数据目录。

安装两个核心包后可运行仓库的 `examples/core_only.py`，无需任何宿主。工具实现由调用方注入；执行失败使用 `ToolExecutionError`、`OSError` 或 `AgentToolResult(is_error=True)`，编程错误不被吞掉。

阶段 B 再接入 steering/follow-up 调度、完整请求/工具/turn 钩子、并行及嵌套工具调用。当前不暴露这些尚未实现的运行选项。
