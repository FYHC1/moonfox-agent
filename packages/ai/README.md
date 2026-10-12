# moonfox-ai

独立的模型消息、事件流、transcript 重放、faux provider，以及 OpenAI-compatible Chat Completions / OpenAI Responses / Anthropic Messages 流式适配器。运行时仅依赖 Python 标准库。

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

## 远端协议（注入传输）

`OpenAIProvider(transport, base_url=..., api_key=...)`、`OpenAIResponsesProvider(...)` 和 `AnthropicProvider(...)` 提供相同的 `.stream(model, transcript, options)`；`transport` 遵循公开 `HTTPTransport`，返回具有 `status`、`headers`、`iter_lines()`、`aclose()` 的 `HTTPResponse`。AI 包不安装 HTTP 客户端、不读取环境/配置、不存密钥，不自动重试。应用可选 `providers` extra 提供 httpx 传输，也可注入其它实现。

- 支持增量文本/工具参数、用量、终止原因、受保护取消与响应清理；只有收尾完成才发布终态。传输须响应 task 取消并最终完成关闭。
- 生成字段包含 temperature、top_p、max_tokens、penalties、seed、stop；Chat/Anthropic 空元组 stop 明确清空停止词。Responses 固定映射 `max_tokens` → `max_output_tokens`，拒绝 penalties、seed、stop（包括空元组）。不支持的协议选项在网络前拒绝，编程错误不伪装可重试失败。
- 用户图片需声明 supports_images；Chat Completions 工具结果图片与所有协议的 assistant 图片历史显式拒绝，Responses 支持图片工具结果。Anthropic 扩展思考请求暂不支持；两种 OpenAI 协议仅在声明 supports_reasoning 后发送 low/medium/high。
- 命名空间工具名自动映射为稳定协议别名，返回时恢复原名；历史/权限不改名。请求移除工具审计等非模型数据。
- Responses 采用 `store:false` 和完整本地历史输入，函数调用使用 call_id 配对（不是 output item ID），不依赖 previous_response_id。`Model.api="openai-responses"` 时仅回放同提供商/模型的加密 reasoning item；没有签名的推理文本不发回远端。支持文本/拒绝内容/摘要增量、用量与 length 截断；终止事件为 response.completed/response.incomplete，不要求 Chat 的 [DONE]。
- 兼容 Chat 的空 `finish_reason` / `refusal` / 旧式 `function_call` 占位，以及 Responses 缺省的 `model` 元数据；不会把空占位当成工具调用或成功结束，实际提供的响应 ID/模型字段仍检查一致性。未返回的解析模型名称保持 `response_model=None`，不伪造元数据。
- SSE 帧/总流量有界，缺终止标记视作错误；HTTP 错误不包含原始 body/key。自动化测试只使用合成 HTTP/SSE；9router/DeepSeek 短文本人工实测的范围见 [提供商管理](../../docs/moonfox/PROVIDER-MANAGEMENT.md)。
