"""Tool-loop tests against a stubbed chat endpoint. No model server, no network.

The loop is the load-bearing part of the local-model migration: the Anthropic SDK
used to own it, and now we do. These pin the behaviours that silently corrupt a
conversation when they go wrong -- a tool call with no matching reply, a null
content field echoed back, arguments arriving as a dict on one server and a JSON
string on another.
"""

from __future__ import annotations

import json

import httpx
import pytest

from reldo.llm import ChatClient, LLMError, Tool, supports_tool_calling


def tool(name="search", handler=None, **kw):
    async def default(**_):
        return "tool output"

    return Tool(
        name=name,
        description=kw.get("description", "A tool."),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=handler or default,
    )


def reply(content=None, tool_calls=None):
    """One /chat/completions response body."""
    message: dict = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})


def call(name="search", arguments='{"query": "whip"}', call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def client_over(responses):
    """A ChatClient whose transport replays the given responses in order."""
    it = iter(responses)
    transport = httpx.MockTransport(lambda request: next(it))
    return ChatClient("http://stub/v1", "test-model", transport=transport)


# -- request shape ---------------------------------------------------------


def test_tool_serialises_to_openai_function_schema():
    payload = tool("search_wiki").to_openai()
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "search_wiki"
    assert payload["function"]["parameters"]["required"] == ["query"]


def test_missing_base_url_or_model_is_rejected_at_construction():
    with pytest.raises(ValueError, match="base_url"):
        ChatClient("", "m")
    with pytest.raises(ValueError, match="model"):
        ChatClient("http://x/v1", "")


async def test_request_carries_model_tools_and_temperature():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply("done")

    async with ChatClient(
        "http://stub/v1", "qwen3:32b", temperature=0.15,
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.complete([{"role": "user", "content": "hi"}], tools=[tool()])

    assert seen["model"] == "qwen3:32b"
    assert seen["temperature"] == 0.15
    assert seen["stream"] is False
    assert seen["tools"][0]["function"]["name"] == "search"


async def test_trace_headers_ride_on_every_request_in_the_tool_loop():
    """Not just the first call. denden tags per request, so a header that only
    made it onto the opening turn would leave the tool round trips untagged --
    losing exactly the part of the trace that shows what the model searched."""
    seen: list[str | None] = []
    responses = iter([reply(tool_calls=[call()]), reply("done")])

    def handler(request):
        seen.append(request.headers.get("X-DenDen-Agent"))
        return next(responses)

    async with ChatClient(
        "http://stub/v1", "m", headers={"X-DenDen-Agent": "reldo"},
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.run_tools([{"role": "user", "content": "hi"}], [tool()])

    assert seen == ["reldo", "reldo"]


async def test_client_for_builds_a_traced_client_from_settings():
    """The factory exists so no call site can forget tracing; pin that it wires
    both the proxy URL and the tag."""
    from reldo.config import Settings
    from reldo.llm import client_for

    settings = Settings(
        _env_file=None,
        user_agent="reldo/test (local)",
        trace_proxy_url="http://127.0.0.1:8443",
        trace_agent="reldo",
    )
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["agent"] = request.headers.get("X-DenDen-Agent")
        return reply("done")

    async with client_for(settings, transport=httpx.MockTransport(handler)) as client:
        await client.complete([{"role": "user", "content": "hi"}])

    assert seen["url"] == "http://127.0.0.1:8443/v1/chat/completions"
    assert seen["agent"] == "reldo"


# -- the loop --------------------------------------------------------------


async def test_loop_exits_immediately_when_no_tools_are_called():
    async with client_over([reply("just an answer")]) as client:
        messages = await client.run_tools([{"role": "user", "content": "q"}], [tool()])
    assert messages[-1]["content"] == "just an answer"


async def test_tool_is_executed_and_its_result_fed_back():
    async with client_over([reply(None, [call()]), reply("grounded answer")]) as client:
        messages = await client.run_tools([{"role": "user", "content": "q"}], [tool()])

    tool_turn = next(m for m in messages if m["role"] == "tool")
    assert tool_turn["content"] == "tool output"
    assert tool_turn["tool_call_id"] == "call_1"
    assert messages[-1]["content"] == "grounded answer"


async def test_every_tool_call_gets_a_reply_even_in_parallel():
    """A requested call with no matching tool message wedges the conversation."""
    calls = [call(call_id="a"), call(call_id="b")]
    async with client_over([reply(None, calls), reply("done")]) as client:
        messages = await client.run_tools([{"role": "user", "content": "q"}], [tool()])
    assert {m["tool_call_id"] for m in messages if m["role"] == "tool"} == {"a", "b"}


async def test_null_content_is_normalised_to_a_string_for_replay():
    """Servers differ on whether a pure tool-call turn has content at all."""
    async with client_over([reply(None, [call()]), reply("x")]) as client:
        messages = await client.run_tools([{"role": "user", "content": "q"}], [tool()])
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["content"] == ""
    assert "tool_calls" in assistant


async def test_dict_arguments_work_as_well_as_json_strings():
    """Ollama sends a dict here; vLLM and OpenAI send a JSON string."""
    async with client_over(
        [reply(None, [call(arguments={"query": "whip"})]), reply("ok")]
    ) as client:
        messages = await client.run_tools([{"role": "user", "content": "q"}], [tool()])
    assert next(m for m in messages if m["role"] == "tool")["content"] == "tool output"


async def test_loop_stops_at_max_iterations():
    endless = [reply(None, [call()]) for _ in range(10)]
    async with client_over(endless) as client:
        messages = await client.run_tools(
            [{"role": "user", "content": "q"}], [tool()], max_iterations=3
        )
    assert sum(m["role"] == "tool" for m in messages) == 3


# -- failures the model should see, not exceptions -------------------------


async def test_unknown_tool_returns_a_readable_error():
    async with client_over([reply(None, [call(name="nope")]), reply("ok")]) as client:
        messages = await client.run_tools([{"role": "user", "content": "q"}], [tool()])
    assert "No such tool" in next(m for m in messages if m["role"] == "tool")["content"]


async def test_unparseable_arguments_return_an_error_not_a_crash():
    async with client_over([reply(None, [call(arguments="{oh no")]), reply("ok")]) as client:
        messages = await client.run_tools([{"role": "user", "content": "q"}], [tool()])
    assert "Could not parse" in next(m for m in messages if m["role"] == "tool")["content"]


async def test_wrong_argument_names_return_an_error():
    """Real handlers have strict signatures, so a hallucinated argument name
    raises TypeError -- which must reach the model as text, not propagate."""

    async def strict(query: str) -> str:
        return f"searched {query}"

    async with client_over(
        [reply(None, [call(arguments='{"wrong": 1}')]), reply("ok")]
    ) as client:
        messages = await client.run_tools(
            [{"role": "user", "content": "q"}], [tool(handler=strict)]
        )
    assert "Wrong arguments" in next(m for m in messages if m["role"] == "tool")["content"]


async def test_exploding_tool_does_not_kill_the_conversation():
    async def boom(**_):
        raise RuntimeError("disk on fire")

    async with client_over([reply(None, [call()]), reply("recovered")]) as client:
        messages = await client.run_tools(
            [{"role": "user", "content": "q"}], [tool(handler=boom)]
        )
    assert "disk on fire" in next(m for m in messages if m["role"] == "tool")["content"]
    assert messages[-1]["content"] == "recovered"


async def test_unreachable_server_raises_actionable_llm_error():
    def down(request):
        raise httpx.ConnectError("no route to host")

    async with ChatClient(
        "http://stub/v1", "m", transport=httpx.MockTransport(down)
    ) as client:
        with pytest.raises(LLMError, match="is the model server up"):
            await client.complete([{"role": "user", "content": "q"}])


async def test_http_error_surfaces_status_and_body():
    async with client_over([httpx.Response(404, text="model not found")]) as client:
        with pytest.raises(LLMError, match="404"):
            await client.complete([{"role": "user", "content": "q"}])


async def test_response_without_choices_is_rejected():
    async with client_over([httpx.Response(200, json={})]) as client:
        with pytest.raises(LLMError, match="No choices"):
            await client.complete([{"role": "user", "content": "q"}])


# -- the tool-calling probe ------------------------------------------------


async def test_probe_detects_a_model_that_emits_tool_calls(monkeypatch):
    monkeypatch.setattr(
        "reldo.llm.ChatClient",
        lambda *a, **k: client_over([reply(None, [call(name="ping")])]),
    )
    assert await supports_tool_calling("http://stub/v1", "good-model") is True


async def test_probe_detects_the_silent_failure_mode(monkeypatch):
    """qwen3-coder:30b writes <tools>{...} into content and calls nothing."""
    monkeypatch.setattr(
        "reldo.llm.ChatClient",
        lambda *a, **k: client_over([reply('<tools>{"name": "ping"}</tools>')]),
    )
    assert await supports_tool_calling("http://stub/v1", "qwen3-coder:30b") is False


async def test_probe_raises_rather_than_blaming_the_model_when_unreachable(monkeypatch):
    """This used to return False, which doctor printed as "reachable but NO
    tool_calls" -- sending you to debug the model while the proxy in front of it
    was simply down. An unreachable endpoint is not a model capability result."""
    def down(request):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(
        "reldo.llm.ChatClient",
        lambda *a, **k: ChatClient("http://stub/v1", "m", transport=httpx.MockTransport(down)),
    )
    with pytest.raises(LLMError, match="Could not reach"):
        await supports_tool_calling("http://stub/v1", "m")


async def test_probe_raises_on_a_proxy_error_rather_than_reporting_no_tool_calls():
    """A denden proxy with an unknown upstream answers 502. That is a broken
    hop, not a model that cannot tool-call."""
    async with ChatClient(
        "http://stub/v1", "m",
        transport=httpx.MockTransport(lambda r: httpx.Response(502, text='unknown upstream')),
    ) as client:
        with pytest.raises(LLMError, match="502"):
            await client.complete([{"role": "user", "content": "q"}], tools=[tool()])
