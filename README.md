# Moonfox Agent

基于 pi 分层思想构建的个人 Agent。当前已完成**阶段 A/B/C：独立核心、Agent 机制、持久会话运行时与扩展宿主**。

## 当前可用

- `packages/ai` → `moonfox-ai`：结构化消息、单消费者有界事件流、transcript 重放、token 估算、溢出识别及离线 faux provider；运行时零第三方依赖。
- `packages/agent` → `moonfox-agent-core`：steering/follow-up 双队列、请求/工具/turn 钩子、并行与嵌套工具、参数校验、事件、取消与轮数预算；项目内只依赖 `moonfox-ai`。
- `packages/moonfox-app` → `moonfox-app`：追加式会话、JSONL 原子批提交/恢复、分支投影、prompt/context、可注入压缩、扩展注册/派发/生命周期与 headless 装配。
- `tests/architecture`：AST 导入规则、依赖声明与闭包检查、公开导入无启动副作用、干净环境构建/安装验收。
- `examples/core_only.py`：无需网络、密钥或宿主的“工具调用 → 第二轮回应”示例。
- `examples/core_stage_b.py`：带权限钩子的嵌套工具、follow-up、到限停止与显式 continue 示例。
- `examples/headless_app.py`：不启用 server 或业务扩展，也能保存、恢复并继续会话。
- `examples/minimal_extension/run.py`：显式授权扩展、首轮上下文注入、卸载清理。

**尚未实现**：阶段 D 的 SessionHub、配置/认证服务、持久 inbox/outbox、server/WebUI，以及后续真实 provider、人设/记忆/QQ 业务扩展。核心仍不读取应用配置、不自动压缩/重试、不启动常驻服务。

## 开发安装（PowerShell）

要求 Python ≥ 3.12；本轮实际验证环境为 Windows / Python 3.14.8。使用 pip 即可，不要求 uv、Node 或 Docker。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --group dev -e .\packages\ai -e .\packages\agent -e .\packages\moonfox-app
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

## 检查入口

```powershell
# Ruff、格式、mypy strict、离线单元/架构测试
.\.venv\Scripts\python.exe .\scripts\check.py

# 加上 sdist → wheel、独立环境安装、依赖闭包和仓库外示例验收
.\.venv\Scripts\python.exe .\scripts\check.py --installation

# 也可分别运行
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m mypy
```

安装验收先准备依赖 wheel（需包索引可访问或缓存齐全），随后断开索引，在四个干净虚拟环境中验证：AI 单包、Agent wheel 自动解析 AI 依赖、应用 wheel 自动解析核心依赖、依次本地目录安装。示例复制到仓库外，使用 `python -I` 运行，不注入 `sys.path` 或 `PYTHONPATH`。headless 环境不安装 Web 框架或业务扩展。

`check.py --work-dir <目录>` 可指定临时检查文件的父目录。默认检查不下载依赖；安装验收默认跳过，须显式开启。

阶段 B 的完整 API 行为与兼容说明见 [Agent 包说明](packages/agent/README.md)。默认工具并行；到限不会消费下一批队列，显式 `continue_` 才开始新 run。`prepare_request` 首轮也执行，嵌套调用经过相同检查链。

阶段 C 的接口与存储/扩展约束见 [应用包说明](packages/moonfox-app/README.md)。工具默认拒绝，执行必须注入宿主权限检查；未消费的 steering/follow-up 队列为进程内数据，不冒充持久 inbox。完整验收包含扩展生命周期、事务损坏恢复、首轮上下文、预算与取消负例。

## 设计与边界

- [当前交接](docs/moonfox/HANDOFF.md)
- [整体架构](docs/moonfox/ARCHITECTURE.md)
- [接口设计](docs/moonfox/DESIGN.md)
- [mini-pi 参考](docs/mini-pi/DESIGN.md)（保留参考设计，不直接视作已验证代码）

公开入口为 `moonfox_ai.api`、`moonfox_agent.api`、`moonfox_app.api`；扩展使用轻量 `moonfox_app.extension_api`。每次 run 必传 `RunOptions(max_turns_per_run=...)`；到限不自动重置预算。运行时通过公开钩子接入 prompt/压缩，不更改核心。取消后须等待 `wait_for_idle()`；JSONL 成功批提交执行 flush/fsync，不宣称硬件故障下绝对零丢失。
