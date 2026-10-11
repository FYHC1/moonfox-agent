# moonfox-agent-core

通用 Agent 核心，项目内仅依赖 `moonfox-ai`，工具参数使用 Pydantic 2。公开接口：`moonfox_agent.api`。Python ≥ 3.12。

## 运行与队列

- `await agent.prompt(message, options=RunOptions(max_turns_per_run=...))` 开始新 run；运行中重复调用拒绝执行。
- `agent.steer(message)` 在当前工具批次结束后、下一次模型请求前投递。启动时先检查一次 steering。
- `agent.follow_up(message)` 只在工具/steering 工作本应结束时投递。
- 两个队列都支持 `one_at_a_time` / `all`，模式可在运行中改变；提供 peek/clear 接口。队列不自行启动 run。
- `finish_turn`、工具续轮、steering、follow-up 共用一个不可变轮数预算。到限返回 `limit_reached`，尚未消费的队列留给后续 run；不压缩或自动重置预算。
- `continue_(options=...)` 从 user/toolResult 上下文继续，或在 assistant 后消费一个 steering/follow-up 批次。最终以唯一 codec 转换后的 role 校验。每次显式调用是新 run；跨 run 的总成本限制由调用方负责。
- `abort()` 使用单向令牌；`await wait_for_idle()` 等所有已启动工具、嵌套调用、更新和监听器结束。不会声称外部副作用已撤销。外部 task 取消在收尾后重新抛出 `CancelledError`。
- `reset()` 清空对话和队列，保留重放出的 prompt/sections 与当前工具基线。`replace_messages` / `set_tools` / `set_model` / `set_thinking_level` 只能在 idle 时调用。

## 注入钩子

| 钩子 | 时机与结果 |
|---|---|
| `prepare_request(ctx, signal)` | 每次请求，包括首轮；待发消息已写入。返回 `AgentRequestUpdate` 的 context/model/thinking_level 在本 run 后续请求中保持 |
| `transform_context(messages, signal)` | 每次请求的临时上下文变换；从当前请求投影复制，不回写 transcript |
| `convert_to_llm(messages)` | 唯一 AgentMessage → Message 边界；同步或异步。默认遇未知消息显式报错，工具的 details/structured_content/nested_calls 不送模型 |
| `before_tool_call(ctx, signal)` | 参数准备及 Pydantic 校验之后、执行之前；拒绝或异常均不放行；嵌套调用同样经过此处 |
| `after_tool_call(ctx, signal)` | 执行之后逐字段替换结果；内容替换而未提供 structured_content 时清除旧结构化结果。已存在的失败不能变为成功 |
| `finish_turn(ctx, signal)` | 工具结果写入之后、turn_end 事件之前。None 正常调度；end 立即结束且不查队列；continue 确保下一次请求（自然请求已满足则不额外添加）。error/abort 为硬出口 |
| `prepare_next_turn(ctx)` | 仅确定要续轮且尚有预算时运行，首轮不运行；可返回 `AgentLoopTurnUpdate`，包含新上下文/模型/草稿消息 |

钩子收到深拷贝的数据快照，但工具对象及其注入服务保留身份。`PrepareRequestContext.canonical_messages` 提供未增强的原始 transcript，适合每次重建临时投影，避免重复注入。钩子返回的新投影不会删改规范 transcript。

核心接收的是可信调用方的单个钩子，不实现插件注册、通知隔离或业务重试策略。编程错误会在必要的收尾/工具配对之后传播，不冒充可重试模型错误。

## 工具执行

默认 `tool_execution="parallel"`：顺序 preflight、并发执行，`tool_execution_end` 按完成顺序发送，工具结果消息按模型调用源顺序保存。全局模式或任一工具的 `execution_mode="sequential"` 会让该批次串行。

兼容阶段 A 的 `execute(call_id, args, signal, on_update)`；需嵌套调用时使用 `execute_with_context(call_id, args, ctx)`，两者只能提供一个：

```python
async def calculate(call_id, args, ctx):
    outcome = await ctx.execute_tool("add", {"left": args.left, "right": args.right})
    return outcome.result
```

- `ctx.execute_tool` 复用参数准备、校验、before/after 钩子和取消信号；子调用 ID 为 `parent/n`，只保存在父结果 `nested_calls`，不额外写入 transcript。
- `ToolCallLimits(max_depth=8, max_calls=128)` 是可配置的每 turn 工具调用限制，所有顶层/嵌套调用共享；顶层深度为 0。
- 工具及进度更新需返回 `AgentToolResult`；更新应 await，迟到更新忽略，已开始更新纳入收尾。上下文关闭后新的嵌套调用拒绝执行。
- 预期执行失败使用 `ToolExecutionError`、`OSError` 或 `AgentToolResult(is_error=True)`。编程错误不被吞掉；已发出的工具批次仍产生对应结果后才抛出。
- `prepare_arguments` 是可选参数兼容转换，运行在严格 schema 校验前；`replay` 是恢复元数据，本包不会自动重放工具。
- `run_tool_call(...)` 是独立单次执行入口，不发事件、不写 transcript；工具内部应使用注入的 `ctx.execute_tool`，不要自行创建无权限上下文。
- Python 同进程工具不是沙箱。任意未注册后台任务及阻塞同步操作仍由工具实现负责。

## 示例与验证

安装两个核心包后运行 `examples/core_only.py` 或 `examples/core_stage_b.py`，无网络、密钥或宿主要求。

根入口 `python scripts/check.py --installation` 包含类型/格式、确定性单元测试、依赖规则、独立 sdist/wheel 构建、三个干净环境安装和两个仓库外示例。`python -m coverage run --branch --source=moonfox_agent -m pytest packages/agent/tests` 可复现核心分支覆盖报告。
