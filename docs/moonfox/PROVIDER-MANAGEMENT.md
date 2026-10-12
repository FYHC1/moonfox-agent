# 提供商与模型管理（阶段 D 基础管理补充）

目标：WebUI 可管理多个独立提供商与模型，配置重启后保留；会话、headless、后续 QQ 均通过同一个模型目录和计量网关，不在 QQ 插件中单独配置 LLM。

## 范围与边界

- 协议：`openai` = OpenAI-compatible Chat Completions；`openai-responses` = OpenAI Responses；`anthropic` = Anthropic Messages。支持用户图片/文本/函数工具调用/SSE；不把服务品牌当成不同协议。Gemini 原生协议暂不实现，兼容端点可手工录入模型。Chat Completions 工具结果图片、所有协议的 assistant 图片历史显式拒绝；Responses 支持工具结果中的图片（需声明图片能力）。Anthropic 扩展思考请求暂不支持，两种 OpenAI 协议的思考级别为 off/low/medium/high（需声明能力）。
- AI 包保留标准库依赖，仅增加可注入 HTTPTransport 的适配器；应用包可选 `providers` extra 提供 httpx 传输。Agent 不读取产品配置。
- 管理员可新增/编辑/启停/删除提供商、模型；CAS 修订冲突拒绝覆盖。删除仍被模型/会话/默认值引用的项返回 409，不静默改历史。
- Windows 本机密钥使用当前用户 DPAPI 保护；其它系统提供 env 引用/无密钥本机端点模式，除非注入其它 SecretProtector。不声称同进程恶意插件是沙箱。
- 密钥命令：`secret_action=keep|replace|clear`；replace 必须有 api_key，keep/clear 不得带 key。GET 只返回 key_configured/credential_source，不返 key/部分掩码。改 base_url/protocol 若保留旧 stored key 必须重新输入，不把旧密钥自动发到新地址。
- 仅显式管理员点击“检测/获取模型”才 GET `{base_url}/models`；不发送历史或发起计费生成，不跟随重定向。真实 API 需用户自己的 key；离线测试用本机假 HTTP/SSE。
- 参数：模型 context_window、max_output_tokens、tools/images/reasoning 能力；`token_field` 仅控制 Chat Completions 的 max_tokens/max_completion_tokens。Responses 固定发送 max_output_tokens。会话 `generation` 可覆盖模型默认值，null 继承；Chat/Anthropic 的 stop=[] 清除默认停止词。Responses 不支持 penalties、seed、stop（包括 []），前后端都拒绝；保存/启用/运行都校验协议限制。
- 每 run 冻结提供商、模型、参数和密钥快照，下一 run 才使用更新；停用/缺密钥阻止新运行，历史仍可读。预算网关在协议序列化前限制输出，模型默认值不能绕过预算。
- 命名空间工具名在协议边界映射为稳定合法别名，返回时恢复原名，权限/历史不改名；不支持工具的模型不声明可执行工具。
- Responses 使用 `/responses`、扁平函数声明与 `function_call_output` 续轮；`store:false`，从本地 transcript 重建输入，不依赖 previous_response_id 或远端会话存储。保留同提供商/同模型的加密 reasoning item 供工具续轮，跨协议/模型不回放；公开 WebUI 事件仍不含推理签名。支持文本/拒绝内容/推理摘要增量、缓存与推理 token 用量，输出上限截断归一化为 length；不完整工具 JSON 不执行。
- httpx 传输禁重定向、禁环境代理、请求 identity 编码，拒绝压缩响应；SSE 单帧 1 MiB、总响应 16 MiB，模型发现 1 MiB/200 条。过长/非法模型 ID 跳过而非截断；原始错误体不返回前端。网络/响应读取/取消都等待关闭，传输实现须最终响应取消。

## 管理 HTTP 契约 `/api/v1`

数据模型单源为 `config/providers.py` + `server/schema.py`，生成前端 schema。

- GET `/providers` -> `ProviderCatalog {providers:ProviderView[], secret_storage:"dpapi"|"env_only"|"custom"}`
- PUT `/providers/{id}` body `PutProvider {request_id, expected_revision, settings:ProviderSettings, secret_action:"keep"|"replace"|"clear"="keep", api_key?:string|null}` -> ProviderView。创建 expected_revision=0；id 匹配 `[a-z][a-z0-9_-]{0,63}`。
- POST `/providers/{id}/delete` body `RevisionCommand {request_id,expected_revision}` -> `{deleted:true}`。
- POST `/providers/{id}/test` body `RevisionCommand` -> `ProviderTestResult {ok, status:"ok"|"auth_error"|"unavailable"|"unsupported"|"invalid_response", detail, latency_ms, models:DiscoveredModel[]}`。模型项 `{id,name}`，最多200；无 provider 原始异常/key。
- GET `/models` -> `ModelCatalog {models:ManagedModelView[], builtin_models: {id,label,provider}[], default_model_id, config_revision}`。
- POST `/models` body `PutModel {request_id,expected_revision:0,settings:ModelSettings}` -> ManagedModelView。共享应用层从模型名称生成未占用 ID，返回值为最终 ID；非零 revision 拒绝。没有隐式重试。
- PUT `/models/{id}` body `PutModel {request_id,expected_revision,settings:ModelSettings}` -> ManagedModelView。手工 ID 创建仍用 revision=0；已有 ID 更新用当前 revision，不因名称变化重命名。
- POST `/models/{id}/delete` body RevisionCommand -> `{deleted:true}`。
- POST `/models/{id}/default` body `SetDefaultModel {request_id,expected_revision}`（全局 config revision） -> ConfigView；只改 default model，新会话生效，现有会话不重绑。
- ProviderView `{id,revision,settings:ProviderSettings,key_configured:bool,credential_source:"stored"|"environment"|"none"}`。
- ManagedModelView `{id,revision,settings:ModelSettings,available:bool,unavailable_reason:string|null}`。
- GET `/config` 的 models 仍保持 `{id,label,provider}` 形状，包含可用 managed/builtin 模型；会话配置提供 generation 表单。所有管理路径管理员鉴权，JSON 边界、密钥不回显、不入 URL。

## 模型 ID

- 提供商 ID 保持原有小写开头、最多 64 位的规则；模型 ID 单独校验，1–200 字符，保留大小写、中文、`-`、`.`、`_`、`:` 等。不能含空白、控制字符或 `/\\?#%`，不能以 `.` 开头。URL 路径按单段编码；远端模型 ID 独立保存，不受此本地路径规则限制。
- 新建默认使用模型名称，例如 `gpt-4.1`；已占用（含内置模型）时使用 `提供商ID-gpt-4.1`，再次重复使用 `提供商ID-gpt-4.1-2` 等。名称里的连续空白/路径保留字符替换成 `-`，去掉开头的 `.`，空值回退 `model`。
- WebUI 显示自动 ID 预览；手工编辑后停止跟随名称，清空可恢复自动模式。自动模式由服务端创建时再次查重，不依赖过时的浏览器目录。保存后的模型 ID 不可通过编辑名称改动；旧 ID、默认模型与会话引用不迁移。

## WebUI

侧栏独立“提供商与模型”管理入口，不必先创建会话。中文提供商列表、模型列表、表单/启停/检测/删除/设默认模型；可把发现的远端 ID 填入新模型表单，也可手工输入。密钥字段保存后清空，空白 keep，明确按钮 clear；错误/空状态/修订冲突明确，禁止自动覆盖或泄密。会话配置下模型生成参数显示继承/覆盖含义，模型选项更新后可选新模型。保留目前工作台视觉系统与响应式布局。

## 使用步骤

1. 安装 `moonfox-app[server,providers]` 并构建 WebUI；按根 README 设置 `MOONFOX_ADMIN_TOKEN`，启动 CLI（不需要 `--demo`）。私有数据目录由 `--home` 显式指定。
2. 管理员登录，侧栏打开“提供商与模型”，创建提供商 ID/名称，选协议，填写 API **基础地址**（通常含 `/v1`），选择本机密钥、环境变量或无认证。保存不发远端请求。
3. 点击“检测 / 获取模型”读取目录后点“添加模型”，或在模型页手工填写远端 ID；发现不自动假设 tools/images/reasoning 能力，须按服务文档设置。
4. 填模型名称，自动预览 ID（重名加提供商前缀，也可手工填写），再设置上下文/最大输出与生成默认值。Anthropic temperature≤1，temperature/top_p 二选一，不支持 penalties/seed；Responses 不支持 penalties/seed/stop，输出自动映射 max_output_tokens。显式切换协议可清除不支持字段；打开已有表单不会静默清除旧设置。
5. 设为默认后创建会话；既有会话需在“会话详情 → 配置”显式选模型。参数留空继承模型默认，0 是有效覆盖；支持 stop 的协议可用明确复选框清空停止词。所有请求仍受逻辑输入预算限制。
6. 停用不删除历史；删除需先解除默认/会话/模型引用。409 需重新载入核对，不自动覆盖。改 stored-key 目标地址必须替换/清除密钥。

headless 可通过 `moonfox_app.api` 的 `ProviderSettings/ModelSettings/GenerationParameters` 和同一个 Hub 管理 API 配置；`await hub.put_model(admin, settings=model, expected_revision=0)` 省略 identity 时也使用同一自动 ID 规则。QQ 接缝读取相同默认目录，但 QQ domain/scope/历史与 Agent 独立。本补充不实现 OneBot 客户端或具体 QQ 扩展。

## 安全与兼容限制

配置只供可信管理员管理，不是多租户 SSRF 沙箱。私有 `--home` 应由当前用户保护；DPAPI 绑定当前 Windows 用户与提供商 ID，数据库拷贝到另一用户/机器后需重新输入密钥。其他平台默认只存环境变量名，无明文回退；同进程可信插件不构成安全隔离。

协议兼容由端点决定，成功获取 `/models` 不保证某个模型的生成权限或所有能力。部分兼容服务不支持模型列表，可手动录入；需要非标准协议/参数的服务暂不支持。没有自动重试、自动模型故障切换、金额计费硬上限或云端凭据管理。自动化验收仍使用合成凭据和本机 HTTP/SSE，不依赖真实 API。

## 9router 真实调用验证

- 已使用用户配置的 `9router → cbai/deepseek-v4.1-flash` 验证 Chat Completions 与 Responses 的真实短文本流式调用、正常结束、用量读取和连接关闭；不向远端发送原有会话历史。
- Chat 兼容空字符串 `finish_reason`、空 `refusal` 和仅含空 name/arguments 的旧式 `function_call` 占位。非空旧式调用仍拒绝，空结束原因不算成功，必须收到有效结束原因及 `[DONE]`；未完成工具参数仍不会执行。
- Responses 允许起始、中间、最终事件省略或以 null 表示 `model`；请求模型保留在 `message.model`，实际解析模型未知时 `response_model=None`。响应 ID、已提供的模型名称、终态和工具配对校验不放宽。
- WebUI 实测包括 Responses 提供商保存，含 `-`/`.` 的自动与手工模型 ID、同名提供商前缀、编辑名称不改 ID，以及使用原始 DeepSeek 配置聊天、流式与持久回复一致、刷新恢复、390px 布局和空浏览器存储。临时提供商/模型验证后删除，原有模型 ID、凭据、默认模型与会话不改绑。
- 此次真实调用范围为短文本；没有据此宣称图片、扩展思考、工具调用或其他厂商已做真实 API 验收，这些路径仍由离线测试覆盖。
- 更新代码后应重启后端并刷新 WebUI；只重新构建前端不会热更新已运行的 Python 模块。重启须沿用原 `--home` 与令牌，并先等待运行/审批收尾、备份私有数据。
