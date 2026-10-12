# moonfox-ext-memory

`moonfox-ext-memory==0.1.0` 是可选的 session-scope 记忆扩展。它只依赖 `moonfox-app==0.1.0`、Pydantic 2 与标准库；扩展实现只使用 `moonfox_app.extension_api` 作为 Moonfox 接口，不自行连接 provider 或网络。可选模型抽取通过宿主计量端口完成。

## 安装与默认值

```powershell
python -m pip install ./extensions/memory
```

分发包尚未发布到 PyPI；先安装仓库中的 ai、agent、moonfox-app，或从同一 wheelhouse 解析依赖。宿主需显式登记并启用 `EXTENSION`，按需授予 manifest 中的 `context`、`tools`、`events`、`tasks`、`commands`、`models` 能力。默认配置开启记忆召回、关闭模型抽取；不会因为安装或 attach 自动请求模型。配置严格拒绝未知字段：`version=1`、`recall=true`、`max_results=6`、`max_reference_tokens=800`、`extraction="off"`、`extraction_max_tokens=512`。设置由不可变 `MemorySettings` 校验。CLI/WebUI 启用方式见 [阶段 E](../../docs/moonfox/STAGE-E.md)。

## 隔离、检索与快照

每个实例只打开宿主提供的 `data_dir/memory.sqlite3`。目录由宿主按 `{domain}/{sha256(scope_id)}` 固定；所有 SQL 操作仍显式绑定 `MemoryScope(domain, scope_id)`，表约束和主键/外键/唯一键也包含 domain 与 scope。SQLite 使用 WAL、FULL synchronous、foreign keys 和 secure_delete；数据修订通过 CAS 更新。工具和管理参数均不能选择域或 scope。

索引是安全参数化的字面关键词索引：英文按词、中文/日文/韩文按重叠 2–3 字 n-gram；查询表达式不会交给 FTS。它不做语义召回，长查询受 1000 字符/900 个索引项限制。召回只查 active 记录；上下文来自 canonical view 中最新、带真实 entry id 的 user message，作为有 token 上限的 `ContextBlock`（不是 system 指令）。记录以 JSON 引用呈现，包含来源、版本与置信度，并明确标注为未验证数据。缓存仅实例内最多 32 项；每次复用前先查 scope revision。最多保留 200 个 SQLite 召回快照，内容只含选中记录 ID/revision 与实际注入文本，不保存 JSONL 或原始聊天。

记录 `subject` 默认是 `conversation`，只是记录分类，不代表操作者身份。来源为 `manual`、`tool` 或 `extraction`，并保存来源 entry id（手工管理没有聊天 entry id）。同 scope 内相同文本/kind/subject/source 幂等合并。记忆本身仍是不可信引用，不应被当作事实或指令。

## 可选抽取与遗忘边界

只有 `extraction="model"` 且宿主提供绑定的 `MeteredModelService` 时，成功的 `agent_settled` 才从最新真实 user entry 登记一个幂等作业，并交给注入的 `TaskService`；事件处理不等待模型。`complete` 只收到那条 user 文本及固定抽取 system 指令，不发送会话 system/tool/reasoning/历史内容，输出没有工具。登记时保存**原 logical input 的预算票据**，恢复后仍用同一票据；不可借用后来输入的预算。每个 source-entry/extractor-version 最多一次 complete。严格 JSON 解析拒绝多余 source/domain/scope 字段；抽取 subject 固定为 `conversation`，置信度上限为 0.6，表示未核验候选。

job 状态为 pending/running/done/failed/unknown/cancelled。SQLite schema 2 新增 `budget_ticket`，支持从 schema 1 保留记录迁移（与配置的 version=1 不同）。重新 attach 会将遗留 running 标为 unknown，不盲目重试；只有配置仍为 extraction=model 时才恢复绑定范围、当前 extractor version 的 pending，最多并发 4 个受管任务。旧作业缺失票据时失败关闭，不从当前 run 领取新额度。确定的调用前拒绝标为 failed，完成状态不明标为 unknown，均不自动重试。pending payload 是为恢复所需的扩展 SQLite 私有数据；完成/失败/未知/取消后会清除，管理响应不包含原文或预算票据。forget、delete、clear 会提升 scope epoch、取消该 scope 所有 pending/running job 并清空 payload；保留不含原文的 source/version job ledger，防止旧输入重复抽取。删除期间返回的模型结果会因 epoch/status 检查被丢弃。

`forget` 停用后续召回，模型 `memory.get` 也不能读取该记录；`memory.forget` 仅返回 id/revision/active，不回显正文，管理员仍可显式查看已遗忘记录。`delete` 删除记录、索引和该 scope 全部召回快照；`clear` 清除该 scope 当前扩展记录/索引/快照和待办 payload。它们**不会**删除 append-only 原始聊天历史、已写入历史的工具结果/模型复述、已发送给 provider 的副本、外部备份或正在执行的请求上下文；更广泛的留存策略属于阶段 G。

## 接口

`moonfox_ext_memory.api` 与包根导出：`EXTENSION`、`MemorySettings`、`MemoryScope`、`ScopedMemoryStore`、`MemoryExtension`。工具为 `memory.search`、`memory.get`、`memory.remember`、`memory.forget`（forget 要求记录当前 `expected_revision`）；管理命令固定为 `list/get/remember/update/forget/delete/clear/jobs`。管理 CAS 过期时抛出 SDK 的 `ExtensionConflictError`，不存在的同 scope 记录抛出 `ExtensionRecordNotFoundError`。管理只打开一次绑定 store，不 attach session、不启动 worker、不请求模型。
