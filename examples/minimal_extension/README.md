# 最小会话扩展

安装三个 Python 包后：

```powershell
python examples/minimal_extension/run.py
```

`plugin.py` 只使用 `moonfox_app.extension_api`，声明 namespaced prompt section、临时参考上下文和 custom entry；不使用运行时私有模块。`run.py` 是应用装配代码，显式加载已知插件文件、启用并授予 manifest 所需能力。

示例验证首轮注入及卸载后的 section 清理。插件无需常驻任务；有后台工作时必须使用 `ctx.tasks.submit(...)`，并遵守取消。可信本地 Python 扩展不是安全沙箱。
