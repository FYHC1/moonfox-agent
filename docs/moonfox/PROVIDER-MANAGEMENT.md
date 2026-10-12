# 提供商与模型管理（阶段 D 基础管理补充）

目标：WebUI 可管理多个独立提供商与模型，配置重启后保留；会话、headless、后续 QQ 均通过同一个模型目录和计量网关，不在 QQ 插件中单独配置 LLM。

## 范围与边界

- 协议：`openai` = OpenAI-compatible Chat Completions；`anthropic` = Anthropic Messages。支持用户图片/文本/工具调用/SSE；不把服务品牌当成不同协议。不实现 Responses/Gemini 原生协议，兼容端点可手工录入模型。OpenAI 工具结果图片、assistant 图片历史显式拒绝；Anthropic 扩展思考请求暂不支持，OpenAI 思考级别为 off/low/medium/high（需声明能力）。
- AI 包保留标准库依赖，仅增加可注入 HTTPTransport 的适配器；应用包可选 `providers` extra 提供 httpx 传输。Agent 不读取产品配置。
- 管理员可新增/编辑/启停/删除提供商、模型；CAS 修订冲突拒绝覆盖。删除仍被模型/会话/默认值引用的项返回 409，不静默改历史。
- Windows 本机密钥使用当前用户 DPAPI 保护；其它系统提供 env 引用/无密钥本机端点模式，除非注入其它 SecretProtector。不声称同进程恶意插件是沙箱。
- 密钥命令：`secret_action=keep|replace|clear`；replace 必须有 api_key，keep/clear 不得带 key。GET 只返回 key_configured/credential_source，不返 key/部分掩码。改 base_url/protocol 若保留旧 stored key 必须重新输入，不把旧密钥自动发到新地址。
- 仅显式管理员点击“检测/获取模型”才 GET `{base_url}/models`；不发送历史或发起计费生成，不跟随重定向。真实 API 需用户自己的 key；离线测试用本机假 HTTP/SSE。
- 参数：模型 context_window、max_output_tokens、tools/images/reasoning 能力、OpenAI token_field；生成默认值 temperature/top_p/max_tokens/penalties/seed/stop。会话 `generation` 可覆盖，null 继承，stop=[] 清除默认停止词；保存/启用/运行都校验协议限制。
- 每 run 冻结提供商、模型、参数和密钥快照，下一 run 才使用更新；停用/缺密钥阻止新运行，历史仍可读。预算网关在协议序列化前限制输出，模型默认值不能绕过预算。
- 命名空间工具名在协议边界映射为稳定合法别名，返回时恢复原名，权限/历史不改名；不支持工具的模型不声明可执行工具。
- httpx 传输禁重定向、禁环境代理、请求 identity 编码，拒绝压缩响应；SSE 单帧 1 MiB、总响应 16 MiB，模型发现 1 MiB/200 条。过长/非法模型 ID 跳过而非截断；原始错误体不返回前端。网络/响应读取/取消都等待关闭，传输实现须最终响应取消。

## 管理 HTTP 契约 `/api/v1`

数据模型单源为 `config/providers.py` + `server/schema.py`，生成前端 schema。

- GET `/providers` -> `ProviderCatalog {providers:ProviderView[], secret_storage:"dpapi"|"env_only"|"custom"}`
- PUT `/providers/{id}` body `PutProvider {request_id, expected_revision, settings:ProviderSettings, secret_action:"keep"|"replace"|"clear"="keep", api_key?:string|null}` -> ProviderView。创建 expected_revision=0；id 匹配 `[a-z][a-z0-9_-]{0,63}`。
- POST `/providers/{id}/delete` body `RevisionCommand {request_id,expected_revision}` -> `{deleted:true}`。
- POST `/providers/{id}/test` body `RevisionCommand` -> `ProviderTestResult {ok, status:"ok"|"auth_error"|"unavailable"|"unsupported"|"invalid_response", detail, latency_ms, models:DiscoveredModel[]}`。模型项 `{id,name}`，最多200；无 provider 原始异常/key。
- GET `/models` -> `ModelCatalog {models:ManagedModelView[], builtin_models: {id,label,provider}[], default_model_id, config_revision}`。
- PUT `/models/{id}` body `PutModel {request_id,expected_revision,settings:ModelSettings}` -> ManagedModelView。
- POST `/models/{id}/delete` body RevisionCommand -> `{deleted:true}`。
- POST `/models/{id}/default` body `SetDefaultModel {request_id,expected_revision}`（全局 config revision） -> ConfigView；只改 default model，新会话生效，现有会话不重绑。
- ProviderView `{id,revision,settings:ProviderSettings,key_configured:bool,credential_source:"stored"|"environment"|"none"}`。
- ManagedModelView `{id,revision,settings:ModelSettings,available:bool,unavailable_reason:string|null}`。
- GET `/config` 的 models 仍保持 `{id,label,provider}` 形状，包含可用 managed/builtin 模型；会话配置提供 generation 表单。所有管理路径管理员鉴权，JSON 边界、密钥不回显、不入 URL。

## WebUI

侧栏独立“提供商与模型”管理入口，不必先创建会话。中文提供商列表、模型列表、表单/启停/检测/删除/设默认模型；可把发现的远端 ID 填入新模型表单，也可手工输入。密钥字段保存后清空，空白 keep，明确按钮 clear；错误/空状态/修订冲突明确，禁止自动覆盖或泄密。会话配置下模型生成参数显示继承/覆盖含义，模型选项更新后可选新模型。保留目前工作台视觉系统与响应式布局。

## 使用步骤

1. 安装 `moonfox-app[server,providers]` 并构建 WebUI；按根 README 设置 `MOONFOX_ADMIN_TOKEN`，启动 CLI（不需要 `--demo`）。私有数据目录由 `--home` 显式指定。
2. 管理员登录，侧栏打开“提供商与模型”，创建提供商 ID/名称，选协议，填写 API **基础地址**（通常含 `/v1`），选择本机密钥、环境变量或无认证。保存不发远端请求。
3. 点击“检测 / 获取模型”读取目录后点“添加模型”，或在模型页手工填写远端 ID；发现不自动假设 tools/images/reasoning 能力，须按服务文档设置。
4. 设置模型的上下文/最大输出与生成默认值。Anthropic temperature≤1，temperature/top_p 二选一，不支持 penalties/seed；不支持的参数会被拒绝，不能靠表单绕过。
5. 设为默认后创建会话；既有会话需在“会话详情 → 配置”显式选模型。参数留空继承模型默认，0 是有效覆盖；清空停止词使用明确复选框。所有请求仍受逻辑输入预算限制。
6. 停用不删除历史；删除需先解除默认/会话/模型引用。409 需重新载入核对，不自动覆盖。改 stored-key 目标地址必须替换/清除密钥。

headless 可通过 `moonfox_app.api` 的 `ProviderSettings/ModelSettings/GenerationParameters` 和同一个 Hub 管理 API 配置；QQ 接缝读取相同默认目录，但 QQ domain/scope/历史与 Agent 独立。本补充不实现 OneBot 客户端或具体 QQ 扩展。

## 安全与兼容限制

配置只供可信管理员管理，不是多租户 SSRF 沙箱。私有 `--home` 应由当前用户保护；DPAPI 绑定当前 Windows 用户与提供商 ID，数据库拷贝到另一用户/机器后需重新输入密钥。其他平台默认只存环境变量名，无明文回退；同进程可信插件不构成安全隔离。

协议兼容由端点决定，成功获取 `/models` 不保证某个模型的生成权限或所有能力。部分兼容服务不支持模型列表，可手动录入；需要非标准协议/参数的服务暂不支持。没有自动重试、自动模型故障切换、金额计费硬上限或云端凭据管理。所有现有验收使用合成凭据和本机 HTTP/SSE，没有实际厂商付费请求。
