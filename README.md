# Moonfox Agent

基于 pi 分层思想构建的个人 Agent。当前实现范围是**阶段 A：独立核心骨架、依赖边界和 faux provider**。

## 当前可用

- `packages/ai` → `moonfox-ai`：结构化消息、单消费者有界事件流、transcript 重放、token 估算、溢出识别及离线 faux provider；运行时零第三方依赖。
- `packages/agent` → `moonfox-agent-core`：最小顺序工具续轮、Pydantic 参数校验、事件、单向取消、显式轮数预算；项目内只依赖 `moonfox-ai`。
- `tests/architecture`：AST 导入规则、依赖声明与闭包检查、公开导入无启动副作用、干净环境构建/安装验收。
- `examples/core_only.py`：无需网络、密钥或宿主的“工具调用 → 第二轮回应”示例。

**尚未实现**：阶段 B 的双队列调度、完整钩子派发、并行/嵌套工具执行；应用宿主、真实 provider、WebUI 和业务扩展也不在阶段 A 中。`queues.py` 已提供独立队列原语，`hooks.py` 只预留契约，不假装已接通。

## 开发安装（PowerShell）

要求 Python ≥ 3.12；本轮实际验证环境为 Windows / Python 3.14.8。使用 pip 即可，不要求 uv、Node 或 Docker。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --group dev -e .\packages\ai -e .\packages\agent
.\.venv\Scripts\python.exe .\examples\core_only.py
```

示例预期输出：

```text
2 + 3 = 5
reason=completed; turns=2; requests=2
```

生产式本地安装可依次执行 `python -m pip install ./packages/ai` 和 `python -m pip install ./packages/agent`。两包可分别构建 wheel，分发时先安装 AI wheel，或让 pip 从同一 wheel 目录解析依赖；分发名尚未发布到 PyPI。

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

安装验收先准备依赖 wheel（需包索引可访问或缓存齐全），随后断开索引，在三个干净虚拟环境中验证：AI 单包、Agent wheel 自动解析 AI 依赖、两包本地目录安装。示例复制到仓库外，使用 `python -I` 运行，不注入 `sys.path` 或 `PYTHONPATH`。

`check.py --work-dir <目录>` 可指定临时检查文件的父目录。默认检查不下载依赖；安装验收默认跳过，须显式开启。

## 设计与边界

- [当前交接](docs/moonfox/HANDOFF.md)
- [整体架构](docs/moonfox/ARCHITECTURE.md)
- [接口设计](docs/moonfox/DESIGN.md)
- [mini-pi 参考](docs/mini-pi/DESIGN.md)（保留参考设计，不直接视作已验证代码）

公开入口为 `moonfox_ai.api` 与 `moonfox_agent.api`。模型请求只接受规范化 transcript；自定义 Agent 消息必须通过显式 `convert_to_llm` 转换。每次 run 必传 `RunOptions(max_turns_per_run=...)`；到限返回 `limit_reached`，不自动压缩或重置预算。取消后须等待 `wait_for_idle()` 才能启动新 run。
