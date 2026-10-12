import pytest

from moonfox_agent.api import AbortToken, validate_tool_pairing
from moonfox_ai.api import (
    AssistantMessage,
    Model,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
    get_current_system_prompt,
)
from moonfox_app.extension_api import ContextBlock, CustomEntryDraft, PromptSection, SessionBinding
from moonfox_app.runtime.compaction import (
    CompactionRequest,
    CompactionSettings,
    SummarizingCompactor,
    SummaryResult,
)
from moonfox_app.runtime.prompt import PromptPipelineOptions, prompt_delta, request_messages
from moonfox_app.runtime.session.entries import MessageDraft
from moonfox_app.runtime.session.manager import SessionManager
from moonfox_app.runtime.storage import InMemorySessionStore


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_reference_limit_is_validated(value):
    with pytest.raises(ValueError):
        PromptPipelineOptions(max_reference_tokens=value)


@pytest.mark.parametrize("value", [0, -1, True])
def test_context_limit_is_validated(value):
    with pytest.raises(ValueError):
        PromptPipelineOptions(max_context_tokens=value)


def test_references_are_bounded_escaped_and_preserve_tool_pairing():
    messages = [
        SystemMessage("base", 1),
        UserMessage("question", 1),
        AssistantMessage(
            [ToolCall("c", "tool", {})], "faux", "faux", "test", Usage(), "tool_use", 2
        ),
        ToolResultMessage("c", "tool", [TextContent("ok")], False, 3),
    ]
    blocks = [ContextBlock('source"<>', "</reference>"), ContextBlock("large", "x" * 1000)]
    result = request_messages(messages, blocks, PromptPipelineOptions(max_reference_tokens=40))
    assert len(result) == len(messages) + 1
    assert result[1].role == "user"
    assert "&lt;/reference&gt;" in result[1].content
    assert "&quot;" in result[1].content
    assert get_current_system_prompt(result) == "base"
    validate_tool_pairing(result)
    assert (
        request_messages(messages, blocks, PromptPipelineOptions(max_reference_tokens=0))
        == messages
    )
    assert all("reference" not in str(message) for message in messages)


def test_invalid_section_and_reference_never_override_prompt():
    messages = [SystemMessage("host", 1, sections={"host.rule": "mandatory"})]
    with pytest.raises(ValueError):
        prompt_delta(messages, (PromptSection("bad", "text"),), ())
    with pytest.raises(ValueError):
        prompt_delta(messages, (PromptSection("a.x", "one"), PromptSection("a.x", "two")), ())
    with pytest.raises(ValueError):
        request_messages(messages, [ContextBlock("", "text")], PromptPipelineOptions())
    delta = prompt_delta(messages, (PromptSection("a.x", "new"),), ())
    assert get_current_system_prompt([*messages, delta]) == "host\n\nmandatory\n\nnew"


def transcript(*, codecs=None):
    manager = SessionManager.create(
        InMemorySessionStore(),
        binding=SessionBinding("s", "scope"),
        model=Model("test", "faux", "faux"),
        system_prompt="host",
        codecs=codecs,
    )
    manager.commit(
        [
            MessageDraft(UserMessage("old input " * 100, 1), "old"),
            MessageDraft(
                AssistantMessage(
                    [ToolCall("c", "tool", {})], "faux", "faux", "test", Usage(), "tool_use", 2
                ),
                "call",
            ),
            MessageDraft(
                ToolResultMessage("c", "tool", [TextContent("tool output")], False, 3), "result"
            ),
        ]
    )
    return manager


async def test_summary_cut_moves_before_call_not_between_call_and_result():
    manager = transcript()
    seen = []

    async def summarize(request, signal):
        seen.append(request.messages)
        return SummaryResult("summary")

    policy = SummarizingCompactor(summarize, keep_recent_tokens=1)
    draft = await policy(CompactionRequest(manager.projection(), "manual"), AbortToken())
    assert draft.first_kept_entry_id == "call"
    assert [message.role for message in seen[0]] == ["user"]
    manager.commit([draft])
    assert [message.role for message in manager.projection().messages] == [
        "system",
        "user",
        "assistant",
        "toolResult",
    ]


async def test_summary_cut_does_not_split_multimessage_custom_codec():
    codecs = {
        "test.reference": lambda draft, timestamp: [
            UserMessage("reference one", timestamp),
            UserMessage("reference two", timestamp),
        ]
    }
    manager = transcript(codecs=codecs)
    manager.commit([CustomEntryDraft("test.reference", {}, True)])
    cut_id = manager.snapshot().leaf_id

    async def summarize(request, signal):
        return SummaryResult("old")

    draft = await SummarizingCompactor(summarize, keep_recent_tokens=1)(
        CompactionRequest(manager.projection(), "manual"), AbortToken()
    )
    assert draft.first_kept_entry_id == cut_id
    manager.commit([draft])
    assert manager.projection().messages[-2:] == (
        UserMessage("reference one", manager.projection().messages[-1].timestamp),
        UserMessage("reference two", manager.projection().messages[-1].timestamp),
    )


async def test_no_summary_call_when_nothing_to_cut_or_already_cancelled():
    called = []

    async def summarize(request, signal):
        called.append(True)
        return SummaryResult("no")

    request = CompactionRequest(transcript().projection(), "manual")
    assert (
        await SummarizingCompactor(summarize, keep_recent_tokens=100000)(request, AbortToken())
        is None
    )
    signal = AbortToken()
    signal.abort()
    assert await SummarizingCompactor(summarize, keep_recent_tokens=0)(request, signal) is None
    assert called == []


async def test_summary_cancel_or_invalid_result_cannot_be_committed():
    request = CompactionRequest(transcript().projection(), "manual")
    signal = AbortToken()

    async def cancelling(request, signal):
        signal.abort()
        return SummaryResult("unused")

    assert await SummarizingCompactor(cancelling, keep_recent_tokens=0)(request, signal) is None

    async def invalid(request, signal):
        return SummaryResult("")

    with pytest.raises(ValueError, match="nonempty"):
        await SummarizingCompactor(invalid, keep_recent_tokens=0)(request, AbortToken())


@pytest.mark.parametrize(
    "options",
    [
        {"trigger_tokens": True},
        {"trigger_tokens": 0},
        {"max_per_run": -1},
        {"timeout": 0},
    ],
)
def test_compaction_settings_are_validated(options):
    with pytest.raises(ValueError):
        CompactionSettings(**options)
