# moonfox-ai

独立的模型消息、事件流、transcript 重放与脚本化 faux provider。运行时仅依赖 Python 标准库。

公开接口：`moonfox_ai.api`。Python ≥ 3.12。

```python
import asyncio
from moonfox_ai.api import Context, FauxProvider, FauxResponse, Model, normalize_context


async def main():
    provider = FauxProvider([FauxResponse.text("Hello.")])
    model = Model(id="scripted", provider="faux", api="faux")
    stream = provider.stream(model, normalize_context(Context(messages=[])))
    message = await stream.result()
    print(message.content[0].text)


asyncio.run(main())
```

`EventStream` 是单生产者、单迭代消费者的有界流，不是广播通道。`result()` 与迭代独立；缓存溢出明确通知迭代消费者，不影响最终结果。`partial` 是共享 live 对象，保存快照应使用 `deepcopy`。提前放弃流时调用 `await stream.aclose()`。

faux 支持文本、thinking、工具调用、错误、延迟和结构化取消信号，无网络与密钥要求。脚本耗尽属于测试/调用错误，会抛出异常；模拟的模型失败编码为 `Error` 事件。
