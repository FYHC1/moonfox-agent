# moonfox-ext-persona

可选、可关闭的会话人设扩展，仅依赖 `moonfox_app.extension_api`；不修改核心 Assistant 类型，不存储用户画像或关系状态。

```python
from moonfox_ext_persona import EXTENSION
# create_application(..., session_extensions=[EXTENSION])
# 管理员再通过 SessionSettings.enabled_extensions=('persona',) 显式启用。
```

`PersonaSettings` 保存 schema version、profile_id、revision、名字、身份设定、交流风格、示例与 `honesty_mode`（默认 `roleplay`，可选 `honest` / `deflect`）。应用配置以 CAS 持久化；每个实例固定一份配置快照。`persona.identity` section 携带内容 SHA256 和修订号，附加 `persona.profile` 元数据；完整实际文本由 runtime 版本化保存。用户重用显示修订号也不会丢失内容版本。

首轮即生效，工具续轮不重复叠加。更换配置在安全边界替换实例；停用移除 section。静态模板可被不同域选择，但本扩展没有跨域记忆通路。身份表达不改变实际权限、工具成功/失败和外部操作事实。

依赖为 Python ≥3.12、moonfox-app 0.1.0、Pydantic；没有 Web、网络、数据库或模型请求。与宿主一样，它是可信本地 Python 扩展而非沙箱。
