# 会话交接摘要

> **用途**：当前实施交接文档，保留设计决策并记录已验证的阶段进展。新会话从本页继续，无需回溯历史对话。
>
> **当前状态**：**阶段 A–E、共享提供商/模型管理和 9router 兼容修复已实现并验收**。E 包含独立人设、隔离记忆、通用宿主接口与 Web 管理；QQ 客户端与真实联调仍属于 F。阶段 E 提交/推送哈希以 Git 为准。

---

## 0. 一句话状态

阶段 E 提供两个独立可选包 `moonfox-ext-persona` / `moonfox-ext-memory`，不修改 ai/agent 核心。配置与管理员记忆管理经同一 Hub/HTTP API；可选模型抽取复用原输入预算，默认关闭。启动/边界见 [STAGE-E.md](STAGE-E.md)，用户提供项目的只读调研见 [QQ-BRIDGE-REFERENCE.md](QQ-BRIDGE-REFERENCE.md)。此前已推送的 9router 兼容修复为 `71093f2`。

### 当前代码边界

- `moonfox_ai.api`：消息/事件、有界单消费者流、transcript 重放、估算/溢出、faux、OpenAI-compatible Chat Completions / OpenAI Responses / Anthropic Messages；HTTPTransport 注入，仍仅依赖标准库。
- `moonfox_agent.api`：steering/follow-up 双队列（one_at_a_time/all）、完整请求/工具/turn 钩子、并行/串行工具、受检嵌套调用、事件、取消和显式运行预算；项目内仅依赖 ai。
- `continue_`、`reset`、状态替换/模型/工具设置均提供公开入口；运行中修改规范状态被拒绝，取消须等待 idle。
- 默认工具执行改为 parallel；任一工具 sequential 覆盖使整批串行。阶段 A 四参数 execute 保持兼容，嵌套调用使用新增 execute_with_context 与 ctx.execute_tool。
- `moonfox_app.api`：headless 单会话与 SessionHub，持久 inbox/outbox、共享提供商/模型/生成参数、配置/预算/授权、受管会话扩展；可选 `server` 和 `providers` extras。runtime/SDK 不读取具体产品配置。人设/记忆为独立可选分发，仅使用 `extension_api`；QQ 业务包尚未实现。

### 已验证与复现

- `python scripts/check.py --installation --web`：Ruff、格式、strict mypy、Python 单元/架构/独立安装、协议导出一致性和前端测试/构建。浏览器另外执行 `npm.cmd --prefix packages/web-ui run test:browser`。
- 阶段 B 最终实测：完整验收 **209 passed**；Agent 专项 **126 passed**；mypy strict 检查 **19** 个源码文件；coverage 启用分支统计的总覆盖率 **97%**（非 100% 分支覆盖）。
- 阶段 C 基线 448 passed、D 基线 576 passed、管理基线 804 passed、Responses/ID 947 passed、会话路由修正 950 passed、9router 修复 991 passed。阶段 E 最终 **1052 Python tests passed**（含安装）、strict mypy **105** 源文件、Ruff 格式 **157** 文件；前端 **64/64** 单元测试、生产构建与 **19/19** Edge 浏览器夹具通过；独立真实后端的离线模型烟雾测试也通过，详见 STAGE-E。
- 安装验收五个包 sdist → wheel，七个干净环境：保留 AI 单包、Agent、headless app、依次目录安装、server/providers extras，再分别验证 persona-only、memory-only 及组合。普通核心/headless 不安装业务扩展、Web 框架、httpx 或前端产物。
- 七个示例复制到仓库外用 `python -I`：core_only/core_stage_b、headless_app/minimal_extension、application_service/server_smoke、persona_memory。独立导入不注入 PYTHONPATH。
- 实测 Windows / Python 3.14.8；声明最低 Python 3.12，但本机没有对 3.12 单独执行测试。开发工具安装在本会话 scratch 虚拟环境，不改全局 Python。
- Git 分支 `main`，公开仓库 `https://github.com/FYHC1/moonfox-agent`；阶段 A `a92898f`、B `cb9ce06`、C `012211b`、D `15b5694`、管理基线 `4c0d69b`、Responses/ID `e04266f`、会话路由修正 `b26ed27`、9router 修复 `71093f2` 已推送；阶段 E 最新哈希/推送状态以 Git 为准。使用仓库局部 noreply 身份与 GitHub CLI 凭据，不把 token 写入项目。
- **阶段交付规则（用户确认）**：每完成一个阶段，先完成相关验收，提交并推送 GitHub、核对本地/远程哈希一致，再开始下一阶段。阶段 C 推送必须先于阶段 D 实现。

实现修正参考伪代码：取消信号以 AI 层协议表示，避免反向依赖；流不是广播，溢出明确失败且最终结果可等待；未终止流确定性报错；快照采用深拷贝；未知自定义消息要求显式 codec，编程错误不冒充模型失败。

### 阶段 B 的实现约束

- 首轮也调用 prepare_request；返回的 context/model/thinking_level 在当前 run 后续请求持续有效，不改下次 run 默认配置。canonical_messages 提供未增强 transcript，transform_context 每次只改临时副本。
- finish_turn 的 None/end/continue 按设计派发；错误/中断优先，continue 由一次自然请求满足或补一次 context-only 请求，所有路径共用同一次轮数预算。到限不消费下一批队列、不自动重入。
- 工具参数先兼容准备、再严格 schema 校验、再 before 检查；并行 preflight 顺序稳定。完成事件按完成顺序，工具结果按调用源顺序写入；after 不能掩盖失败。
- 嵌套调用共享 before/after、取消信号、ToolCallLimits（每 turn，默认 max_depth=8/max_calls=128，可注入）。子调用仅留在父结果 nested_calls；默认 codec 清除工具审计/程序字段，不发给模型。
- 单向取消收敛已启动工具/嵌套/更新/监听器后才 idle；外部 task 取消收尾后重抛。监听器或编程错误先补齐可配对结果再传播；非法 provider 工具 ID 保存诊断并拒绝执行。
- 核心不是插件宿主或沙箱；不实现通知隔离/业务重试/跨 run 成本网关。任意未注册后台任务与阻塞同步工具由调用方负责。

### 阶段 C 的实现与验收

- runtime 仅依赖核心公开 API、轻量 SDK 和注入端口，不 import extensions/services/config/security/jobs/server 的具体实现；SDK 导入不载入 runtime/bootstrap/扩展宿主，不创建任务/线程/进程/网络连接。
- entry 具有 id/parent_id；消息 entry 沿用 Agent message_id。模型/thinking 变更、context edit、compaction、namespaced custom 均追加为事实；投影和持久 leaf 可重建分支，原 entry 不删除。
- JSONL 使用 begin/entry/commit + SHA-256、修订号 CAS、成功批 flush/fsync 和 OS 单写者锁。损坏/未提交尾部截断到安全边界；中部损坏/重复 ID/孤儿 parent 报错。不是网络分布式存储，也不承诺硬件故障绝对零丢失。
- `SessionBinding(session_id, scope_id)` 是不可变 opaque 绑定，恢复须完全匹配；C 只提供绑定契约，D 已补身份/domain 路由，E 通过作用域资源与独立记忆包完成两域多会话隔离验收。
- 缺失尾部工具结果恢复为 unknown 错误，不自动重放；未知 custom 数据保留，关键上下文缺 codec 明确报不兼容。持久化为严格有限 JSON，未知 role/type/字段不得静默丢弃。
- 首轮/每次请求从规范投影重建 prompt/context。section 增删版本化提交；参考块有来源、受 token 估算预算约束，不污染 transcript，不复用失败检索结果。卸载移除工具与 section。
- 可注入压缩策略；支持手动和显式阈值（每 run 次数/超时上限）。切点不拆工具配对，系统/工具基线保留，新摘要可纠正旧事实。非法/取消/无效压缩不提交，不自动 retry/reenter/reset 预算。
- 扩展显式工厂/启用列表/授权能力；manifest SDK/版本/依赖校验。会话/宿主生命周期分开，部分失败回滚、注销句柄撤销、受管后台任务收敛。宿主 ChannelGateway 仅协议和注入测试，不实现 QQ 路由。
- 工具默认拒绝；宿主 checker 使用校验后的真实参数且先于扩展限制，嵌套工具相同检查。boundary 草稿预览后原子提交；不能用预览期间另行 stage/enqueue 绕过事务，continue 仍用同次预算。
- `create_headless_app` 显式拥有 store 与扩展生命周期；无扩展也能保存恢复。关闭/外部 task 取消等待工具与通知收尾；未消费 steering/follow-up 仍是进程内队列，不冒充 durable inbox。
- 最终实测：完整 `check.py --installation` **448 passed**；应用专项 **221 passed**；mypy strict **48** 个源码文件；Ruff 格式检查 **76** 个文件。coverage 开启 branch 后，应用总覆盖率 **93%**（不是 93% 分支覆盖）。核心源码和参考 Markdown 本轮未改动。

### 阶段 D 的实现与验收

- services/SessionHub：每会话单执行者、durable FIFO 输入、幂等回执、跨会话并行；runtime/扩展实例跨输入保留，配置修订仅下次 run 安全边界生效。关闭浏览器不取消 Agent。
- SQLite 保存会话/domain/route/scope、配置、输入、预算、审批、outbox；JSONL 独立保留 transcript。未领取输入可恢复，运行中断标 unknown，不自动重放；历史丢失/损坏隔离，单宿主 lease 防双写。
- config/security：不可变 strict 设置、CAS 修订、显式 env secret references、owner/admin 授权；前端不能指定 domain/owner/scope。工具授权与单次审批 fail-closed，嵌套调用同链。
- ModelGateway：共享并发、逻辑输入请求/时间/token 预留，continue 不清零。预留为估算且不退崩溃额度，usage 超额追加记账；不声称精确金额硬上限，无自动 Retry-After/业务重试。
- BoundChannelGateway：固定连接/账号/目标授权，按 bot+target+message_id 去重；最终公开回复先进入 outbox，发送不明标 unknown、不盲目重发。仅假 transport 验收，不实现 OneBot/SnowLuma。
- server：可选依赖，HTTP Bearer + WS 首帧鉴权/Origin allowlist，命令/快照/订阅均授权；稳定 message ID、epoch/seq、独立有界缓冲/原子水位补发/resync，wire 不含 partial/reasoning/内部审计。API-only 无前端也运行。
- web-ui：独立 Vite/原生 JS/AJV，消费服务端导出 schema/夹具；中文认证、会话、流式消息、工具/审批、配置冲突、重连/手机布局。token 只在页面内存，无浏览器业务队列。
- 最终验收：Python **576 passed**（含五环境安装）；应用专项 **337 passed**；strict mypy **74** 源文件、Ruff 格式 **112** 文件；前端 **41** 单元测试、生产构建与 **7/7** Playwright 浏览器夹具测试通过。应用 coverage 启用 branch 的总覆盖率 **89%**，不是分支覆盖率。
- 真实本机服务 + Edge 154 浏览器联调通过：登录/新建/发送流式完成、保存配置、刷新恢复、关闭页面后 run 继续、390px 无横向溢出、页面 JS 错误 0、local/sessionStorage 均空。与夹具测试分开验证；测试令牌/数据/截图全部在 scratch。内置浏览器面板不可用，改用 Playwright/本机 Edge。
- 环境：Python 3.14.8、Node **24.21.0**；最低 Python 3.12 未单独测试。Starlette TestClient 使用 httpx 有一条上游弃用警告，不影响测试；未以忽略失败方式消除。AI/Agent 与参考 Markdown 本轮不变。

### 阶段 D 基础管理补充

- 管理员多提供商/模型 CRUD/CAS、启停、显式模型发现、默认模型、模型生成默认值和会话覆盖；空主机可直接管理。WebUI 不是平行配置源，headless/未来 QQ 走同一 ModelCatalog/预算网关。
- Windows 当前用户 DPAPI，其他平台环境变量引用或注入 SecretProtector；凭据 keep/replace/clear，只写不回显，改 stored-key 地址需重新输入/清除。发现只 GET /models，不发聊天/生成，HTTPS（loopback 可 HTTP），不跟重定向，不用环境代理。
- 管理基线接通 Chat Completions/Messages 双协议流式适配、文本/用户图片/工具往返/用量/取消与关闭；工具命名空间映射为稳定协议别名后恢复原名。AI 不引入 HTTP 客户端，httpx 为应用可选依赖；不自动重试。Responses 后续补充见下一节；Gemini/Anthropic 扩展思考仍不实现。
- 每 run 冻结端点/凭据/模型/参数，编辑下一 run 生效；停用/无凭据历史仍可读。null 继承、0 保留、stop=[] 明确清空；预算准入限制输出。SQLite v1→v2 追加迁移，旧 session JSON 自动补 generation 默认值。
- 实测：完整 **804 Python tests**；应用专项 **379 passed**，启用 branch 后总覆盖率 **89%**（非分支覆盖率）；strict mypy **86** 源文件、格式 **128** 文件；前端 **50** 单元测试、构建、**11/11** Edge 浏览器夹具通过。
- 真实 CLI（不带 --demo）+ 本机合成 HTTP/SSE + Edge：空目录新增两个提供商、发现/录入模型、默认选择、模型默认和会话参数覆盖进入实际 HTTP 请求、刷新和进程重启恢复 DPAPI/配置/历史；390px 无横向溢出、页面 JS 错误 0、local/sessionStorage 均空。工具续轮两协议/图片/小片段/取消清理/渠道目录共享另有本地网络测试。无真实 API key/付费请求，临时数据和截图在 scratch。
- 原 8765 预览仅为可丢弃的离线演示，不是用户正式模型配置目录。具体限制、API 和启动步骤见 PROVIDER-MANAGEMENT。

### Responses 协议与模型 ID 补充

- `openai-responses` / `OpenAIResponsesProvider` 实际 POST `/responses`：input、instructions、扁平函数声明、function_call_output 续轮，max_tokens 映射 max_output_tokens。支持文本/拒绝内容/图片输入与图片工具结果、推理摘要/同提供商同模型加密 reasoning 回放、用量、length 截断和受保护取消；使用 store:false，不依赖远端会话存储。禁止 stop（包括 []）、penalties、seed。
- 提供商 slug 不变；模型 ID 独立支持大小写、中文、`-`、`.` 等，最多 200 字符，不含空白/控制字符或路径保留字符、不以 `.` 开头。新建默认模型名称，冲突使用提供商前缀，再冲突加序号；规范化不安全字符。已有 ID、默认值和会话引用不自动改名。
- POST `/api/v1/models`（PutModel，expected_revision=0）由共享目录分配 ID；headless 的 hub.put_model 可省略 identity 使用同一规则。手工 ID 仍用 PUT，CAS 不覆盖。WebUI 自动预览但以服务端最终 ID 为准，支持手工覆盖和清空恢复自动；编辑已有名称不改 ID。
- 前后端协议 schema 已重新生成。协议切换只在明确操作时清空不支持字段，初次渲染不静默修改；Responses 的 Chat token_field 控件不生效，保留旧值不阻止保存。地址/协议变更仍要求显式更新已存密钥。
- 最终运行 `scripts/check.py --installation --web`：**947 Python tests**、strict mypy **88** 文件、Ruff/格式 **131** 文件、**58 JS tests** 和生产构建通过；`npm.cmd --prefix packages/web-ui run test:browser`：**14/14 Edge 测试通过**。未重新计算覆盖率。
- 本机 HTTP/SSE 验证三协议工具续轮、图片/小片段、取消关闭；Responses 思考级别/参数映射、配置与凭据按 run 冻结、输出预算/渠道共享；API 与恢复测试验证 ID 查重、Unicode 路径、长前缀 ID、默认与会话持久引用。浏览器验证新协议、参数修复、发现预填、自动/手工 ID、过时目录冲突和既有功能回归。
- 全部使用合成凭据与本机夹具，无真实厂商付费请求；原有 Starlette/httpx 弃用警告保留，不影响验收。现有演示服务与数据未重启/改写；使用新适配器需重启后端并刷新页面。生产前端已重新构建。

### 代码审查修复：会话查找与渠道订阅

- 修复已存在会话仍受当前全局默认配置校验阻断的问题：渠道路由查找、取消与输入，以及 Web 新建会话的幂等重试，不再要求当前默认模型可用。只在首次插入会话前调用宿主校验；真正新建仍拒绝不可用默认值，不绕过 owner/domain/route 冲突检查。
- 修复 reply 订阅初始化失败后监听器残留、无法重试的问题：路由解析全部成功后才发布监听器与内存路由映射。
- 新增 3 项回归测试，均先验证旧代码失败再修复转绿；相关会话/渠道测试 **20 passed**。完整 `scripts/check.py --installation --web`：**950 Python tests**、strict mypy **88** 文件、Ruff/格式 **132** 文件、**58 JS tests** 和生产构建通过；Edge 浏览器回归 **14/14** 通过。
- 本轮直接审查共享配置、会话生命周期、渠道与 WebUI 衔接；并行协议适配辅助复核因耗时停止，未返回结论，不计入完成的审查。无真实厂商调用；未重启/修改演示服务或用户配置。现有 Starlette/httpx 弃用警告仍保留。

### 9router 实测兼容性修复

- 真实调用定位：Chat 流使用空字符串 finish_reason/refusal，结束块可能带空旧式 function_call；Responses 的 response.created/in_progress/completed 不含 model。修复仅位于 AI 协议边界，不按厂商品牌分叉。
- 仅忽略空占位与缺省模型元数据，响应 ID/实际模型名称一致性、明确终态、工具完整性校验保留；response_model 未知时为 None，不伪造远端解析结果。无自动重试或业务降级。
- 新增 **41** 项合成回归：旧代码 **14 failed / 27 passed**，修复后新测试及已有远端/Responses/应用 HTTP 集成 **342 passed**。不把真实模型的自由回复内容当作确定性协议断言。
- 完整 `scripts/check.py --installation --web`：**991 Python tests**（含三包构建、五环境安装）、strict mypy **88** 文件、Ruff/格式 **133** 文件、**58 JS tests**、协议导出一致性与生产构建通过；Edge 浏览器回归 **14/14** 通过。保留原 Starlette/httpx 上游弃用警告；未重新计算覆盖率。
- 已用用户配置的 9router / `cbai/deepseek-v4.1-flash` 实测两种协议的短文本流、用量与关闭；WebUI 实测普通问答（17+25 → 42）、增量与持久回复一致、刷新恢复、390px 无横向溢出、页面错误 0、浏览器存储为空。图片/思考/真实工具未进行厂商 API 验收。
- 真实 Edge 管理验证：Responses 保存，含 `-`/`.` 的自动/手工 ID、同名提供商前缀、编辑名称不改 ID。临时提供商/模型均已删除；用户原提供商、凭据、模型 ID、默认模型与现有会话保持不变。
- 8765 原进程未加载此前 Responses/ID 更新，已在空闲时备份 SQLite/JSONL 并以同一 home/令牌重启，检查最新协议加载与目录恢复。内置浏览器面板仍不可用，使用本机 Edge/Playwright；不影响用户访问 `http://127.0.0.1:8765/`。
- **数据位置提醒**：用户本次真实配置仍在本 PI 会话 scratch 的 `interactive-preview-home`，不是固定正式目录；备份与测试产物也仅在 scratch。删除本 PI 会话前应迁移到固定私有 `--home`，本轮未擅自迁移。只保留带“修复验证/实测”标题的新增验证对话，未改写原聊天记录。

### 阶段 E：人设与隔离记忆

- 两个独立可选包均为 0.1.0，普通应用没有强制依赖；CLI `--extension persona --extension memory` 只登记，不替已有会话启用扩展、工具授权或模型抽取。两个核心及两份保留参考文档本轮不改。
- `SessionExtensionDefinition` 提供工厂/配置 schema/默认值/工具/管理命令；`ExtensionConfiguration` JSON 快照用会话 CAS 保存。宿主每会话 fork registry，注入 `ExtensionResources(binding, domain, data_dir, models)`；配置只在安全边界替换实例，卸载中再次保存时下个输入仍读最新修订。
- 人设 `honesty_mode=roleplay` 默认，另有 honest/deflect；首轮注入 `persona.identity`，携带修订/SHA256，完整文本版本化，停用移除，工具续轮不叠加。静态人设没有跨域关系库，不伪造权限或工具成功。
- 记忆目录 `home/extensions/memory/{domain}/{sha256(scope_id)}/memory.sqlite3`；所有 SQL、ID 工具、合并、缓存/快照、作业恢复与删除仍使用 domain+scope。首版是字面关键词/CJK n-gram 检索，不是向量/语义召回。参考是有来源的临时 ContextBlock，不升级为 system 或反复落聊天。
- 默认 recall=true、max_results=6、max_reference_tokens=800、extraction=off、extraction_max_tokens=512。工具 search/get/remember/forget 另行授权；模型声明按 allowlist 过滤，实际执行仍强制检查。管理员 CRUD/遗忘/删除/清空/jobs 绑定当前会话，不启动聊天、runtime 或模型。
- 抽取仅在显式开启后登记幂等作业，只发送选定真实用户输入；来源由宿主绑定、subject 固定 conversation、置信度封顶 0.6。SQLite schema 2 保存原 logical input 预算票据；恢复 pending 不借用新额度，缺票据失败关闭，running→unknown 不重试。最多 4 个受管任务，停止不再派发，旧模型端口撤销。
- 遗忘阻止未来召回及模型 `get`，forget 不回显正文；管理员可显式查看已遗忘记录。删除扩展副本和作业原文并用 epoch 防止旧结果复活，不宣称擦除 JSONL/已产生的工具结果或复述/远端/备份/当前请求上下文。
- 有界复核发现的“模型按 ID 读取已遗忘文本”和“同毫秒/时钟回拨时快照淘汰刚写入项”均先加失败回归后修复。跨重启借错预算、卸载中热配置竞态也有先失败后转绿的回归；修复后记忆/宿主/集成专项 **39 passed**，最终全量 **1052 passed**。
- 本机 pytest 偶发卡在 `_pytest.pathlib._force_symlink` 的辅助 current 链接创建；最终检查通过 scratch-only 公共 fixture 用 `mktemp(..., numbered=False)` 避免该链接，不改产品/断言、不跳过测试。安装验收清除 PYTHONPATH、仍用 `python -I`。详见 STAGE-E 验收说明。保留 Starlette/httpx 上游警告；本轮不重算覆盖率。
- Web 使用导出协议及扩展 schema，保留未知配置，管理员记忆页只操作当前会话；切换/退出中止请求并忽略迟到响应，409 保留草稿、未知网络写入不自动重发。最终 **64/64** JS、构建、**19/19** Edge 夹具通过；真实 Python CLI + 离线 demo 的独立 home/临时端口验证人设配置、记忆增改/遗忘/删除和刷新恢复，390px 无横向溢出、页面错误 0、local/sessionStorage 为空，默认输入仅 1 次模型请求。

- 阶段 E 未调用真实收费模型、未连接 QQ、未重启 8765 或改动用户原模型/凭据/默认值/会话。生产前端构建不意味着已有 Python 进程加载新接口；启用 E 需安全重启并沿用原 home/令牌。用户真实数据仍在 PI scratch，删除 PI 会话前应经确认迁移固定私有目录。

### 下一步：阶段 F

用户确认后实现自写薄 OneBot 11 客户端，SnowLuma 优先/NapCat 备选，首版 reverse WS Universal。先完成假 OneBot 的 echo/断连/重放/跨账号与目标授权/unknown 发送，再在明确授权的测试目标真机验收。复用现有 ChannelGateway、共享模型目录和 durable inbox/outbox，不引入 SDK/MCP/NoneBot2，也不另建凭据配置。

qq-bridge 只读检查固定提交 `9df6a7e7fc5abcb36793337f778483bd442a3d2d`：借鉴引用归属/负缓存、群触发与有界合并、发送时权限复检、消息分段、连接/账号健康分层；不照搬 DSH/SDK/MCP 单体、内存队列或社交状态。未安装/执行其代码，项目自身 MIT 与 SnowLuma 独立许可需分别核对。每阶段验收后先提交推送并核对远端，再进入下一阶段。

文档维护：Ruff 只处理 Python 文件，排除 docs；原 mini-pi 和历史草案保持本轮前的内容。

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

## 4. 目录规划（阶段 A–E 已实现，QQ/运维部分按后续路线推进）

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
│   │   │   └── providers/            # base / faux / HTTP 契约 / OpenAI / Anthropic
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
| `SessionExtensionDefinition` | 配置校验/默认值/工具/JSON 管理命令；仅显式登记可信包 |
| `ExtensionResources.models` | 声明 models 能力后获得同会话计量端口；作业保存原输入预算票据 |

### 宿主扩展能力（QQ）

```python
class ChannelGateway(Protocol):
    async def submit(self, message: ChannelInput) -> InputReceipt: ...
    async def control(self, command: ChannelControl) -> None: ...
    def subscribe_replies(self, listener: ReplyListener) -> RegistrationHandle: ...
```

### 记忆隔离

`MemoryScope(domain, scope_id)` 属于独立记忆包，是不可变值；`ScopedMemoryStore` 绑定只读 scope，不提供跨范围查询入口。具体方法以 `moonfox_ext_memory.api` 和其测试为准。

- 宿主认证/路由后生成 `SessionBinding(session_id, scope_id)`，domain 与数据目录经 `ExtensionResources` 注入，runtime 无需导入记忆库。
- 模型工具和管理 JSON 参数不能覆盖 domain/scope/session；按 ID 读取与写入也必须校验绑定，不仅 search。
- E 已覆盖记录/索引、检索/缓存/快照、作业/恢复、管理/删除；媒体、完整备份与导入导出留在后续阶段，不冒充已验收能力。

---

## 6. 文档地图

| 文件 | 内容 | 何时读 |
|---|---|---|
| [README.md](README.md) | 文档入口、包布局、职责约束 | 先读 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 整体架构 11 节：目标映射、包边界、目录、核心保留/移出、扩展系统、记忆隔离、调用链、部署存储、实施顺序 | **实现前必读** |
| [DESIGN.md](DESIGN.md) | 接口设计 8 节：公开 API、run 契约、扩展 SDK、派发/上下文/事务、业务扩展契约、预算事件、验证清单 | 写接口时查 |
| [STAGE-E.md](STAGE-E.md) | 人设/记忆安装配置、管理、隔离、预算与删除边界、最终验收 | 启用扩展前必读 |
| [QQ-BRIDGE-REFERENCE.md](QQ-BRIDGE-REFERENCE.md) | 固定提交的只读调研、取舍与许可/联调边界 | 阶段 F 开始前读 |
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
| **E（已实现）** | 独立人设/记忆、配置/管理、原输入计量端口 | 禁用后核心不受影响；两域多会话负例与独立安装 |
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
