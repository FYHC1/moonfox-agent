# Moonfox Agent

基于 pi 分层思想构建的个人 Agent。当前实现**阶段 A–E：独立核心、持久会话与扩展宿主、应用服务、独立 WebUI、共享提供商/模型管理，以及可启停的人设与隔离记忆**。

## 当前可用

- `packages/ai` → `moonfox-ai`：结构化消息、有界事件流、transcript 重放、token 估算、faux 和 OpenAI Chat Completions / Responses / Anthropic Messages 流式适配器；HTTPTransport 由宿主注入，运行时零第三方依赖。
- `packages/agent` → `moonfox-agent-core`：steering/follow-up 双队列、请求/工具/turn 钩子、并行与嵌套工具、参数校验、事件、取消与轮数预算；项目内只依赖 `moonfox-ai`。
- `packages/moonfox-app` → `moonfox-app`：会话/投影/压缩、扩展宿主、持久 SessionHub、配置授权/审批、逻辑输入预算、共享模型目录，以及可选 HTTP/WS server 和 httpx transport。
- `packages/web-ui`：独立中文工作台，消费导出的协议 schema；会话/流式消息、提供商与模型 CRUD、凭据只写、生成参数、扩展配置、记忆管理、审批和重连恢复，不拥有后台 run。
- `extensions/persona` / `extensions/memory`：独立可选包；版本化人设、domain/scope 隔离的字面记忆检索、管理与受授权工具；可选后台抽取共用原输入预算，默认关闭。
- `tests/architecture`：AST 导入规则、依赖声明与闭包检查、公开导入无启动副作用、干净环境构建/安装验收。
- `examples/core_only.py`：无需网络、密钥或宿主的“工具调用 → 第二轮回应”示例。
- `examples/core_stage_b.py`：带权限钩子的嵌套工具、follow-up、到限停止与显式 continue 示例。
- `examples/headless_app.py`：不启用 server 或业务扩展，也能保存、恢复并继续会话。
- `examples/minimal_extension/run.py`：显式授权扩展、首轮上下文注入、卸载清理。
- `examples/application_service.py`：无 Web 依赖的持久输入回执/后台执行示例；`server_smoke.py` 验证可选 API-only 安装。
- `examples/persona_memory.py`：两个扩展组合、QQ/Agent 范围隔离及停用后普通 Agent 的离线示例。

**尚未实现**：QQ 协议客户端与真实联调（阶段 F）、Gemini 原生协议。已有的提供商与模型配置由 WebUI/headless/渠道会话共用，后续 QQ 不另建一套 LLM 配置。模型 ID 支持 `-`、`.` 等字符；新建默认使用模型名称，重名加提供商 ID 前缀，已有 ID 保持稳定。核心仍不读取应用配置或启动常驻服务。[qq-bridge 借鉴评估](docs/moonfox/QQ-BRIDGE-REFERENCE.md) 记录了阶段 F 可参考的边界机制。

## 开发安装（PowerShell）

Python ≥ 3.12；实际验证 Windows / Python 3.14.8。核心与 headless 使用 pip 即可；独立前端构建另需 Node ≥22.12，不要求 uv 或 Docker。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --group dev -e .\packages\ai -e .\packages\agent -e .\packages\moonfox-app
# 完整仓库测试还需要两个可选业务扩展；仅用核心/headless 时可不安装
.\.venv\Scripts\python.exe -m pip install -e .\extensions\persona -e .\extensions\memory
.\.venv\Scripts\python.exe .\examples\core_only.py
.\.venv\Scripts\python.exe .\examples\core_stage_b.py
.\.venv\Scripts\python.exe .\examples\headless_app.py
.\.venv\Scripts\python.exe .\examples\minimal_extension\run.py
```

示例预期输出：

```text
2 + 3 = 5
reason=completed; turns=2; requests=2
```

只使用核心时，依次执行 `python -m pip install ./packages/ai` 和 `python -m pip install ./packages/agent` 即可。需要持久会话再安装 `./packages/moonfox-app`。三个包各自构建 sdist/wheel，pip 可从同一 wheelhouse 解析本地依赖；分发名尚未发布到 PyPI。

## WebUI 与提供商管理

完整说明见 [提供商管理](docs/moonfox/PROVIDER-MANAGEMENT.md) 和 [阶段 D](docs/moonfox/STAGE-D.md)。安装可选依赖、构建前端，设置随机管理员令牌后启动：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\packages\moonfox-app[server,providers]"
npm.cmd --prefix packages/web-ui ci
npm.cmd --prefix packages/web-ui run build
$env:MOONFOX_ADMIN_TOKEN = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
.\.venv\Scripts\python.exe -m moonfox_app.cli --home .moonfox --static-dir packages/web-ui/dist
```

访问 `http://127.0.0.1:8765`，连接表单使用该令牌。空目录可直接打开侧栏“提供商与模型”：新增提供商、填写 API 基础地址与认证、手工新增或发现模型、配置参数并设为默认，然后创建会话。Windows 本机密钥使用当前用户 DPAPI，其他平台默认使用环境变量引用；API 不回显密钥，浏览器不保存凭据。会话 `generation` 覆盖模型默认值，配置在下次运行生效。

省略 `--static-dir` 可仅启动 API；加 `--demo` 才启用明确标识的离线预设回复，不会调用真实 AI。关闭浏览器不取消后台任务。阶段 E 验收使用离线模型及隔离测试数据，不连接真实 QQ 或付费模型。

### 启用人设与记忆

安装上面的两个可选包后，在启动命令追加 `--extension persona --extension memory`。这只登记扩展；还需在会话“配置”中显式启用。人设默认 `honesty_mode=roleplay`；记忆自动召回默认开启、模型抽取默认关闭，工具必须另行授权。管理员通过“记忆”页管理当前会话，不能覆盖其 domain/scope。

已有 Python 服务不会随前端构建自动更新；安全重启时保留原 home/令牌。遗忘阻止后续召回与模型按 ID 读取，删除只清理扩展副本，不擦除聊天历史、远端副本或备份。详见 [阶段 E](docs/moonfox/STAGE-E.md)。

## 检查入口

```powershell
# Ruff、格式、mypy strict、离线单元/架构测试
.\.venv\Scripts\python.exe .\scripts\check.py

# 加上 sdist → wheel、独立环境安装、依赖闭包和仓库外示例验收
.\.venv\Scripts\python.exe .\scripts\check.py --installation

# 前端先 npm ci，再加前端测试/构建
.\.venv\Scripts\python.exe .\scripts\check.py --installation --web

# 也可分别运行
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m mypy
```

安装验收先准备依赖 wheel（需包索引可访问或缓存齐全），随后离线安装：五个包分别构建 sdist/wheel，七个干净环境覆盖 AI 单包、Agent wheel、headless 应用 wheel、依次目录安装、server/providers extras、人设单独安装、记忆单独安装及组合。七个示例复制到仓库外用 `python -I` 执行，不注入路径。核心和普通 headless 环境不安装业务扩展、Web 框架、httpx 或前端产物。

`check.py --work-dir <目录>` 可指定临时检查文件的父目录。默认检查不下载依赖；安装验收默认跳过，须显式开启。

阶段 B 的完整 API 行为与兼容说明见 [Agent 包说明](packages/agent/README.md)。默认工具并行；到限不会消费下一批队列，显式 `continue_` 才开始新 run。`prepare_request` 首轮也执行，嵌套调用经过相同检查链。

阶段 C 的接口与存储/扩展约束见 [应用包说明](packages/moonfox-app/README.md)。工具默认拒绝，执行必须注入宿主权限检查；未消费的 steering/follow-up 队列为进程内数据，不冒充持久 inbox。完整验收包含扩展生命周期、事务损坏恢复、首轮上下文、预算与取消负例。

阶段 D 的 SessionHub 使用持久 inbox，原子接收后返回回执；运行完成另发事件。显式继续共享逻辑输入预算，审批 fail-closed；慢订阅者 resync，不阻塞 Agent。通道 capability/outbox 已有假 transport 验收，但不是 QQ 客户端。详见 [协议](docs/moonfox/STAGE-D-PROTOCOL.md)。

阶段 E 的人设/记忆只通过公开扩展 SDK 接入；配置、工具声明与实际执行、管理命令和抽取预算仍由宿主约束。隔离覆盖记录、索引、召回缓存/快照、工具 ID 读取、作业恢复和删除；插件是可信同进程代码，不是恶意代码沙箱。

交付规则：每阶段验收后提交并推送 GitHub，核对远程哈希一致后再开始下一阶段。

## 设计与边界

- [当前交接](docs/moonfox/HANDOFF.md)
- [整体架构](docs/moonfox/ARCHITECTURE.md)
- [接口设计](docs/moonfox/DESIGN.md)
- [mini-pi 参考](docs/mini-pi/DESIGN.md)（保留参考设计，不直接视作已验证代码）

公开入口为 `moonfox_ai.api`、`moonfox_agent.api`、`moonfox_app.api`；扩展使用轻量 `moonfox_app.extension_api`。每次 run 必传 `RunOptions(max_turns_per_run=...)`；到限不自动重置预算。运行时通过公开钩子接入 prompt/压缩，不更改核心。取消后须等待 `wait_for_idle()`；JSONL 成功批提交执行 flush/fsync，不宣称硬件故障下绝对零丢失。
