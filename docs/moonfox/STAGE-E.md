# 阶段 E：人设与隔离记忆

> **已实现并通过阶段验收**：独立人设/记忆包、受管宿主接口与 Web 管理。QQ 实际接入仍属于 F，本轮项目调研见 [qq-bridge 借鉴评估](QQ-BRIDGE-REFERENCE.md)。

## 安装与启用

两个扩展是独立可选分发，普通 `moonfox-app` 不依赖它们。开发环境要运行完整仓库测试时需都安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\extensions\persona -e .\extensions\memory

# 还需按阶段 D 的说明安装 server/providers extra、构建 WebUI 和设置管理员令牌
.\.venv\Scripts\python.exe -m moonfox_app.cli --home .moonfox --static-dir packages/web-ui/dist --extension persona --extension memory

# 无 Web、真实模型或 QQ 的离线示例
.\.venv\Scripts\python.exe examples\persona_memory.py
```

`--extension` 只登记已安装的可信包，**不自动替已有会话启用扩展、授权工具或开启模型抽取**。在工作台选定会话，进入配置，显式启用 `persona` / `memory` 并编辑对应配置。已有后端进程不会随源码/前端构建自动加载 Python 更新，需要在安全时机重启并沿用原 `--home` 与令牌。

Headless 装配同样使用 `create_application(..., session_extensions=(PERSONA, MEMORY))`；`PERSONA/MEMORY` 为各包公开的 `EXTENSION`。会话 `enabled_extensions` 决定实例启停，`extension_config` 是不可变的 `ExtensionConfiguration(extension_id, config_json)` 元组。省略条目使用扩展默认配置，不从模型输出读取配置。

## 人设

- `PersonaSettings` 保存配置 schema version、profile_id、revision、名字、身份设定、风格、示例与 `honesty_mode`。
- 默认 `roleplay`（在人设内回答），可选 `honest` / `deflect`；虚构设定不是现实操作事实。不会伪造工具成功、隐藏重要失败或修改宿主权限。
- 每实例固定配置，`persona.identity` 系统 section 携带显示修订与 SHA256，完整文本由 runtime 版本化保存；另有 `persona.profile` 元数据。
- 首轮即注入，续轮不重复叠加。保存配置不打断当前推理，运行结束后的安全边界释放旧实例；下一 run 使用新配置。停用会移除 section。
- 静态模板可以被不同会话选择，但本扩展不包含跨域关系状态或用户画像库。

## 记忆与隔离

宿主先完成 owner/admin 授权和会话路由，再注入 `ExtensionResources(binding, domain, data_dir, models)`；资源目录形如：

```text
<home>/extensions/memory/<agent|qq>/<SHA256(scope_id)>/
```

每个目录属于一个绑定范围。记忆扩展拥有 SQLite/迁移/索引，不接触宿主全局数据库。数据库查询、更新、删除、去重、召回快照和作业同时使用 **domain + scope**，并有复合键/约束；分目录只是额外防线。模型工具参数和管理参数没有 domain/scope/session 覆盖入口。QQ 群、私聊和不同 Agent 会话不会自动互通。

记录包含文本、类别（fact/preference/profile）、主体、来源、置信度、修订和 active 状态；自动抽取是有来源的候选声明，不是已证实的操作者事实。首版使用中文关键词/ngram 的字面检索，不声称语义召回或向量检索。

召回是带来源的临时参考，不提升为系统指令，不反复写入规范 transcript。`max_results` 与 `max_reference_tokens` 限制注入，token 数为项目估算器的预算口径，不是厂商计费 tokenizer。实例缓存包含绑定、输入/投影版本及存储修订；管理改动、遗忘和停用不能复用旧召回。实际选中记录/版本/注入文本的快照留在扩展库，参与扩展删除。

默认参数：

| 字段 | 默认 | 范围/含义 |
|---|---|---|
| recall | true | 自动参考召回，不等于工具授权 |
| max_results | 6 | 1–20 |
| max_reference_tokens | 800 | 64–4096，估算口径 |
| extraction | off | `off` / `model`，默认不发第二次请求 |
| extraction_max_tokens | 512 | 128–2048，仍受原输入预算限制 |

模型工具为 `memory.search/get/remember/forget`，需另外加入宿主 allowlist，可要求单次审批。未授权工具不出现在模型声明中，真正执行仍经过强制权限检查；管理端删除/清空不暴露为模型工具。

## 后台抽取与预算

开启 `extraction=model` 后，`agent_settled` 仅登记幂等作业，不在通知回调里等待第二次模型响应。受管工作器只提交选定的实际用户输入及来源，不发送完整旧聊天、系统提示或工具内部数据；严格验证候选 JSON，来源由作业绑定而非模型自报，subject 固定 conversation、置信度上限 0.6。

宿主的 `models` 能力提供原 logical input 的预算票据。抽取复用同一 ModelCatalog、并发网关以及**原输入持久请求/token/时间预算**，不重置额度；管理调用没有模型能力。没有余量时作业失败，不自动重试、重入或切换提供商。凭据只在请求启动时解析并冻结，不写进作业库，也不放进 prompt。

作业有 pending/running/done/failed/unknown/cancelled 状态；崩溃遗留的 running 恢复为 unknown，不重放未知调用。SQLite schema 2 新增 `budget_ticket` 并保留数据迁移，待办持久化**原输入票据**，恢复不得借用当前输入预算；无票据的旧待办失败关闭。仅在重新 attach 且配置仍允许抽取时恢复绑定范围和当前 extractor version 的 pending，最多 4 个受管任务。配置停用/宿主关闭会收敛任务、停止后续调度并撤销旧模型端口。foreground 的 `hub.wait_for_idle()` 不代表所有独立抽取作业都已完成，可通过管理状态查询确认。

## 管理接口

工作台的管理对象始终是**当前选中会话**，QQ 会话仍为 qq 域。管理不会投递聊天输入或把查询结果自动塞入 Agent 上下文。当前管理命令要求管理员身份。

```text
POST /api/v1/sessions/{session_id}/extensions/{extension_id}/commands/{command}
{request_id, expected_revision: 会话配置修订, arguments_json: "{...}"}
→ {request_id, result_json: "{...}"}
```

Headless 对应 `await hub.extension_command(principal, session_id, extension_id, command=..., arguments_json=..., expected_revision=..., request_id=...)`。允许的命令由服务端元数据声明，不支持任意 Python/前端脚本上传执行。

记忆命令：

- `list`：query、limit、include_forgotten；返回 records 和当前范围 revision。
- `get`：id；只读取绑定范围的记录。
- `remember`：text、kind、subject；管理来源为 manual。
- `update`：id、记录 expected_revision、text、kind、subject。
- `forget` / `delete`：id、记录 expected_revision。
- `clear`：当前范围 expected_revision；明确清理该范围。
- `jobs`：状态摘要，不回传原始输入或内部预算票据。

配置修订和记录修订是两个不同的 CAS。冲突返回 409，查不到当前范围的记录返回 404；错误响应不回显私密原文。网络失败不等于操作未执行，客户端不自动重发写操作。

工作台“配置”页按宿主 schema 显示人设与记忆字段，保留其它扩展配置，不自动授权工具或开启抽取。“记忆”页提供搜索、新增、编辑、停止召回、查看遗忘记录、确认单条删除及手动刷新任务状态；范围清空通过管理员 HTTP/headless 的 `clear` 命令完成。切换会话/退出会清空数据草稿、取消请求并忽略迟到结果。409 保留草稿供核对，未知网络结果不自动重发写入。未安装与无管理员权限均有明确空态，不尝试跨会话补查。

### “遗忘”和“删除”不是同一件事

- **遗忘**：标为不再召回，模型 `memory.get` 也拒绝已遗忘记录；`memory.forget` 只返回 id/revision/active，不回显正文。管理员可显式查看已遗忘记录。
- **删除/清空扩展副本**：清理扩展记录、索引、召回快照与相应作业原文，并通过失效代次/去重标记拒绝旧工作器返回后复活数据。
- **不会自动删除**：追加式 JSONL 原始聊天、已经进入聊天的工具结果/模型复述、已经发送给提供商的数据、外部备份和操作系统副本。保留无原文的去重标记也不同于原文记录。

因此 UI 不称其为“所有副本彻底擦除”。完整历史/备份保留和恢复策略属于 G，不能用 SQLite 行删除作这样的保证。正在进行的请求已经携带的上下文也无法被“撤回”；遗忘针对后续召回。

## 通用接缝与独立性

- 两个核心包不变。runtime 不导入扩展实现、应用配置或记忆数据库。
- SDK 增加显式 `SessionExtensionDefinition` 与作用域资源/计量端口；宿主以零参数注册工厂的会话分支装配它们，不扫描未知目录执行代码。
- 插件源码仅通过 extension_api 使用核心值类型与能力；WebUI 只消费 HTTP/WS/导出 schema。
- 停用所有业务扩展后，基础 Agent 继续工作；普通 headless 安装仍不需要这些扩展、httpx、Web 框架或前端产物。
- 这是可信同进程 Python 扩展契约，不是恶意代码沙箱。

## 验收记录

实际环境：**Windows / Python 3.14.8**，Node 24.21.0、npm 11.19.0、本机 Edge。声明最低 Python 3.12，但本轮未单独验收该版本或其它操作系统。未重新计算覆盖率。

| 检查 | 最终结果 |
|---|---|
| `scripts/check.py --installation` | **1052 Python tests passed**，含架构/隔离/集成与独立安装 |
| Ruff / 格式 / strict mypy | 通过；格式 **157** 文件，类型 **105** 源文件 |
| 分发独立性 | **5** 个 sdist→wheel、**7** 个干净环境、**7** 个仓库外 `python -I` 示例通过 |
| 协议导出 | `scripts/export_protocol.py --check` 通过，generated 文件未手改 |
| `npm.cmd --prefix packages/web-ui run test` | **64/64** 单元测试，无跳过 |
| `npm.cmd --prefix packages/web-ui run build` | 生产构建通过 |
| `npm.cmd --prefix packages/web-ui run test:browser` | **19/19** Edge/Playwright 本地夹具通过 |
| 独立真实后端 + Edge 烟雾测试 | 使用新 home/临时端口、离线 demo；人设配置、记忆增改/遗忘/删除、刷新恢复通过；390px 无横向溢出、页面错误 0、local/sessionStorage 为空；默认输入仅 1 次请求 |

测试覆盖：首轮/续轮/恢复/停用；两域多会话、相同文本/主体及 ID 越界负例；工具声明与执行权限、无模型管理；召回/缓存/快照修订；v1→v2 迁移、恢复原预算与无余量拒绝；抽取关闭、排队/关闭/未知执行；遗忘后的模型 ID 读取、旧任务返回不能复活；配置卸载期间再次保存的竞态；前端配置/记录双 CAS、迟到响应、QQ 域保持、删除确认与移动布局。

完整检查保留 1 条 Starlette/httpx 上游弃用警告，未用忽略失败的方式消除。烟雾测试首轮因扩展/工具同名复选框选择器匹配多项而中止，收窄脚本选择器后重跑通过，未因此修改产品行为。截图、日志和合成测试数据只保存在 session scratch，不进仓库。

### 本机 pytest 临时目录说明

Windows 环境中曾卡在 `_pytest.pathlib._force_symlink` 创建辅助 `current` 链接，而非产品代码。最终运行使用 scratch-only `stage_e_tmpdirs.py`，通过公开 fixture API 改变临时目录命名，避免该可选链接：

```python
from uuid import uuid4
import pytest

@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("case-" + uuid4().hex, numbered=False)
```

仅在检查进程设置 `PYTHONPATH=<scratch>`、`PYTEST_PLUGINS=stage_e_tmpdirs`、`PYTHONIOENCODING=utf-8`；未改仓库 conftest、产品或测试断言，也未跳过测试。独立安装测试清除 PYTHONPATH 并使用 `python -I`，不把开发源码目录注入被验收环境。普通环境可直接运行根 README 的检查命令，不要求这个本机 workaround。

本轮未连接真实 QQ、未调用收费模型，也未重启 8765 或修改用户原模型配置、凭据、默认值和会话。真实服务启用 E 仍需操作者在安全时机沿用原 home/令牌重启并显式登记扩展；这不是阶段 E 的代码阻塞。QQ 握手/echo/账号状态/媒体与平台限流留在 F，完整历史/备份保留留在 G。
