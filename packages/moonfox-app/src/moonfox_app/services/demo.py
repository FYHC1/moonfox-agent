"""An explicitly labelled offline provider for protocol/UI acceptance, never a real LLM."""

from moonfox_ai.api import (
    AssistantMessageEventStream,
    FauxResponse,
    Model,
    StreamOptions,
    TranscriptContext,
    UserMessage,
)


def demo_stream(
    model: Model, context: TranscriptContext, options: StreamOptions | None = None
) -> AssistantMessageEventStream:
    users = [message for message in context.messages if isinstance(message, UserMessage)]
    text = users[-1].content if users and isinstance(users[-1].content, str) else "你好"
    reply = (
        "这是 Moonfox 的离线演示响应，不是真实模型推理。\n\n"
        f"已收到：{text[:1200]}\n\n"
        "输入回执、会话持久化、流式事件、配置和取消均由后台服务处理；关闭页面不会取消运行。"
    )
    return FauxResponse.text(reply, delay_ms=30, chunk_size=6).to_stream(model, options)
