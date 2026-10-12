# moonfox-app

Moonfox 的 headless 会话运行时与可信本地扩展宿主，Python ≥ 3.12。公开入口：`moonfox_app.api`；扩展只依赖轻量 `moonfox_app.extension_api`。导入不发现插件、不创建任务、不启动网络服务。

安装不需要 Web 框架、前端、QQ、人设或记忆扩展。应用包依赖两个核心包、Pydantic 和 packaging；这些应用能力没有进入核心。

阶段 D 已增加持久 SessionHub、durable inbox/outbox、配置/身份/审批与计量，以及可选 HTTP/WS server。使用 `create_application` 构造服务，或安装 `moonfox-app[server]` 后运行 CLI。独立 WebUI、启动命令和限制见 [阶段 D 说明](../../docs/moonfox/STAGE-D.md)；以下单会话接口保持兼容。

## Headless 使用

```python
from moonfox_agent.api import RunOptions
from moonfox_ai.api import FauxProvider, FauxResponse, Model
from moonfox_app.api import (
    InMemorySessionStore, SessionBinding, create_headless_app,
)

async def demo():
    faux = FauxProvider([FauxResponse.text("hello")])
    async with await create_headless_app(
        store=InMemorySessionStore(),
        binding=SessionBinding("my-session", "opaque-private-scope"),
        model=Model("offline", "faux", "faux"),
        stream_fn=faux.stream,
        system_prompt="Be helpful.",
    ) as app:
        outcome = await app.prompt("hi", options=RunOptions(max_turns_per_run=3))
        return app.view(), outcome
```

持久化时传入 `JsonlSessionStore(path, create=True)`；恢复使用 `JsonlSessionStore(path)`，注入相同 binding 和 provider。工厂取得 store 所有权，成功关闭或启动失败都会释放它。调用者提供路径，运行时不读取固定目录或应用配置。

- 新会话必须提供 model；恢复时以 entry 中的 model/prompt/thinking 为准，不用构造参数覆盖历史。`set_model`、`set_thinking_level` 记录新 entry，只允许 idle 调用。
- `prompt` / `continue_` 每次必须传 `RunOptions`。到限停止，不自动开始另一 run、重试或重置预算。
- `enqueue(UserMessage(...), "steer" | "follow_up")` 复用核心队列。**未消费队列是进程内的，关闭会丢弃**；不是阶段 D 的 durable inbox。已交付消息才写入 transcript。
- `abort()` 单向取消；`wait_for_idle()` 包括工具、持久化和 `agent_settled` 通知收尾。外部 task 取消会等待收尾后重抛。回调不能等待或关闭自己的会话。
- `aclose()` 先取消并等待 run，然后卸载扩展、提交 section/工具移除、关闭 store。关闭后不接收新输入。
- `view()` / 事件 payload 是脱离内部状态的数据快照，新增只读 `message_ids` 与消息逐项对应；`SessionEvent` 与 `AgentEvent` 分层，不是 HTTP/WS wire 协议。

完整示例：`examples/headless_app.py`；扩展示例：`examples/minimal_extension/run.py`。

## 追加式事实、投影与恢复

`SessionManager` 提供低层 `preview` / `commit_preview` / `commit` / `select_leaf`；`AgentSession` 包装公开 Agent 钩子。低层 manager/store 属于可信装配代码，不应在活动会话外并发写入，也不授予普通扩展。

- entry 从开始就有 `id`、`parent_id`、timestamp。模型完成消息使用核心 `message_id` 作为 entry ID，不依赖 Python 对象身份。
- `HeaderEntry` 绑定不可变 `SessionBinding(session_id, scope_id)`，恢复必须完全匹配。runtime 只认识 opaque scope；阶段 D services 已提供身份与 domain/route/scope 结构映射。当前没有记忆数据库，完整跨域记忆验收属于阶段 E。
- 原始 entry 永不因压缩或 context edit 删除。投影沿 active leaf 回溯；导航到旧 leaf 后可继续形成分支，leaf 选择持久化。
- 压缩保留重放后的 prompt/sections/工具基线与完整工具配对；摘要作为 user 内容，不伪造 assistant 回答。新摘要可替换过期事实，不要求无限累加。
- 普通 namespaced custom entry 无扩展也可读取，payload 原样保留；`context_required=True` 必须提供显式 codec，否则拒绝建立上下文。codec 只允许输出 user 参考数据，不可注入系统/工具身份。
- 只有尾部缺失工具结果可恢复：追加 `is_error=True`、`details.status="unknown"` 的结果，不自动重放工具。中部非法配对拒绝投影。
- 持久化字段必须是有限 JSON 值。未知角色/内容标签/字段不静默丢弃；不可序列化的工具审计数据会显式失败。

### JSONL 事务边界

每批记录为 `begin(tx, revision, count, leaf_id)` → entry records → `commit(tx, digest)`。提交前校验全部 entry/投影，修订号冲突拒绝写入；成功返回前 `flush + os.fsync`。内存 store 使用相同校验和原子批语义。

重新打开只接纳有匹配校验和的完整批次，截断未提交/损坏尾部到最后安全提交边界；中部损坏、非法提交、重复 ID、孤儿 parent 报错，不自动改写关系。操作系统文件锁保证本机协作进程单写者，进程退出会释放。不是分布式存储，也不保证硬件故障、网络文件系统或断电时绝对零丢失；目录创建由调用者负责，未额外承诺目录元数据 fsync。

I/O 失败尝试回滚整个未成功批次；回滚本身失败时 store 标记不可继续使用，必须重新打开检查。恢复后不会因副作用结果不明而自动执行工具。

## Prompt 与压缩

首轮及每次续轮都从规范投影重建请求。授权 prompt provider 返回 namespaced section，变化以系统消息增量落盘；卸载移除旧 section 和工具声明。参考 context provider 只返回带 source 的 `ContextBlock`，作为临时 user 参考内容，受 `PromptPipelineOptions.max_reference_tokens` 的估算预算约束，不累计到 transcript，也不会沿用失败 provider 的旧结果。

`max_context_tokens` 是可选的请求前估算上限，不是计费精确 tokenizer。引用转义仅用于分帧，不能保证语义上的 prompt injection 免疫。工具调用/结果不能被参考内容拆开。

`compact()` 是显式操作；可注入 `CompactionPolicy` 或 `SummarizingCompactor(summarize, keep_recent_tokens=...)`。后者自动移动切点以保留工具配对，summary callback 可使用宿主的已计量模型服务。默认不进行任何摘要模型请求。启用 `CompactionSettings(trigger_tokens=..., max_per_run=..., timeout=...)` 后，只在安全请求边界有界调用，不重入 core run；失败、取消、非法切点或没有缩小上下文都不提交摘要。

单会话 `create_headless_app` 不自行跨 run 计费；阶段 D 的 `create_application` 提供共享并发与持久逻辑输入请求/时间/token 预算。压缩工厂收到同一个计量 stream，不应绕过它；没有真实 provider 费用网关或自动重试。

## 扩展 SDK 与生命周期

`ExtensionRegistry.register(manifest, factory)` 显式登记工厂；`enabled_extensions` 指定启用列表，`grants` 明确授予 manifest 请求的全部能力。不扫描或执行未知目录。

- SDK 版本 `0.1.0`，manifest 版本/范围用 packaging 校验；依赖必须显式启用，循环/缺失依赖在调用工厂前拒绝。
- 会话级：`setup → attach → detach → dispose`，每个会话新实例。宿主级 `HostExtensionManager`：`setup → start → stop → dispose`，注入 `ChannelGateway`。阶段 D 提供授权路由/outbox 能力；具体 QQ 协议实现仍后置。
- setup 只注册；attach/start 后才能使用 task、entry 或 enqueue 等活动能力。卸载撤销旧 API/上下文/订阅句柄，删除注册，取消并等待所有受管任务。部分启动失败逆序清理。
- `on` 通知快照派发、稳定顺序、逐监听器深拷贝、超时/错误隔离；context 失败贡献空内容；required prompt 失败阻止请求。链式转换和就地修改都在副本上校验，失败退回该步之前的有效结果；权限异常 fail-closed。
- 工具/命令/section/custom type 必须使用自己的 `extension_id.name`。`stage_entries` 只暂存 namespaced custom 草稿，在安全边界预览后整批提交。boundary handler 通过返回 `BoundaryResult` 追加草稿，预览期间禁止另行 stage/enqueue；非法批次不写入、不续跑。
- 扩展 boundary 的 continue 共用当前 run 预算。命令入口必须注入授权回调。**工具默认拒绝**：未注入宿主 `permission_checker` 不执行；权限用校验/参数准备后的实际 args，顶层和嵌套工具走相同强制检查，扩展只能进一步限制，不能覆盖宿主拒绝。
- 后台工作使用 `TaskService.submit(name, work_factory, timeout=...)`，不留下无主 `create_task`。阻塞同步代码和忽略取消的任务不能被 asyncio 安全强制终止；可信本地 Python 插件不是沙箱。

## 阶段边界与检查

阶段 D 已实现应用服务与独立 WebUI；仍未实现真实 provider、人设/记忆/QQ 具体业务扩展。Web 框架只通过可选 `server` extra 安装，headless 不需要前端构建或 Web 依赖。

根 `python scripts/check.py --installation --web` 检查 Ruff、格式、strict mypy、确定性测试、协议导出一致性、三个包 sdist → wheel、五个干净环境安装、仓库外示例与前端测试/构建。`--web` 需先在 packages/web-ui 执行 npm ci；省略则仅检查 Python。当前实际验证环境为 Windows / Python 3.14.8，未单独实测最低 Python 3.12。
