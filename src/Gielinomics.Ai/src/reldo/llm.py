"""Local LLM access over an OpenAI-compatible chat-completions endpoint.

Talks to Ollama or vLLM with plain ``httpx`` and raw JSON -- the same shape as
nexus-v2's ``ai_service`` -- rather than pulling in a vendor SDK. The endpoint is
a de-facto standard, and both servers speak it, so a client library would buy
nothing but a dependency.

**Not every local model can do tool calling, and the ones that can't fail
silently.** ``qwen3-coder:30b`` emits a raw ``<tools>{...}`` blob into the message
*content* and returns zero ``tool_calls``, so the loop below sees a plain text
answer, exits, and hands back a confidently wrong reply with no error anywhere.
Probed against Ollama on a 5090:

    qwen3:32b              tool_calls=1   works
    mistral-small3.2:24b   tool_calls=1   works
    qwen3-coder:30b        tool_calls=0   BROKEN -- emits <tools> as text

Verify a model with :func:`supports_tool_calling` before trusting it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

# Local models are slow and we would rather wait than lose a half-finished
# agent loop. Matches nexus-v2's 600s.
DEFAULT_TIMEOUT = 600.0


class LLMError(RuntimeError):
    """The model server failed, or answered in a shape we can't use."""


@dataclass(slots=True)
class Tool:
    """A tool the model may call, plus the coroutine that runs it."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ChatClient:
    """Minimal async client for an OpenAI-compatible ``/chat/completions``.

    Args:
        base_url: Endpoint root including ``/v1`` (e.g. ``http://192.168.1.78:11434/v1``).
        model: Model name as the server knows it (e.g. ``qwen3:32b``).
        timeout: Per-request timeout in seconds.
        temperature: Sampling temperature.
        headers: Extra headers on every request. Used to tag traces when the
            base URL points at a recording proxy; see :func:`client_for`.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        temperature: float = 0.3,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required, e.g. http://192.168.1.78:11434/v1")
        if not model:
            raise ValueError("model is required, e.g. qwen3:32b")
        self.model = model
        self.temperature = temperature
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=timeout, headers=headers or None, transport=transport
        )

    async def __aenter__(self) -> ChatClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Tool] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """One round trip. Returns the raw assistant message object."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions", json=payload
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"{exc.response.status_code} from {self._base_url}: "
                f"{exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"Could not reach {self._base_url} -- is the model server up? {exc!r}"
            ) from exc

        body = response.json()
        choices = body.get("choices")
        if not choices:
            raise LLMError(f"No choices in response: {str(body)[:300]}")
        return choices[0]["message"]

    async def run_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        *,
        max_iterations: int = 12,
        max_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """Drive the tool loop until the model stops calling tools.

        Returns the full message list, so the caller can read the final assistant
        turn and inspect everything that happened on the way. The conversation is
        mutated in place as well.
        """
        for _ in range(max_iterations):
            message = await self.complete(messages, tools=tools, max_tokens=max_tokens)
            messages.append(_assistant_turn(message))

            calls = message.get("tool_calls") or []
            if not calls:
                return messages

            by_name = {t.name: t for t in tools}
            for call in calls:
                messages.append(await _run_one_call(call, by_name))

        log.warning("Tool loop hit max_iterations=%d without settling", max_iterations)
        return messages


def client_for(settings: Settings, **overrides: Any) -> ChatClient:
    """Build a ChatClient from settings, tracing included.

    Every caller went through the same three-argument incantation, which meant
    adding trace headers would have been three chances to forget one -- and a
    forgotten call site doesn't fail, it just silently stops being recorded.
    Construct clients through here so that can't happen.
    """
    kwargs: dict[str, Any] = {
        "temperature": settings.temperature,
        "headers": settings.trace_headers,
    }
    kwargs.update(overrides)
    return ChatClient(settings.chat_base_url, settings.chat_model, **kwargs)


def _assistant_turn(message: dict[str, Any]) -> dict[str, Any]:
    """Normalise an assistant message for replay.

    Some servers omit ``content`` entirely on a pure tool-call turn; others send
    null. Both break strict endpoints when echoed back, so pin it to a string.
    """
    turn: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") or "",
    }
    if message.get("tool_calls"):
        turn["tool_calls"] = message["tool_calls"]
    return turn


async def _run_one_call(call: dict[str, Any], by_name: dict[str, Tool]) -> dict[str, Any]:
    """Execute one tool call and build its ``role: tool`` reply.

    Every failure path still returns a tool message: a missing reply for a
    requested call wedges the conversation, so an error string the model can read
    and recover from beats an exception that kills the turn.
    """
    function = call.get("function", {})
    name = function.get("name", "")
    call_id = call.get("id") or name

    def reply(content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}

    tool = by_name.get(name)
    if tool is None:
        return reply(f"No such tool {name!r}. Available: {', '.join(sorted(by_name))}.")

    raw = function.get("arguments") or "{}"
    try:
        # Ollama sends a dict here; vLLM and OpenAI send a JSON string.
        arguments = raw if isinstance(raw, dict) else json.loads(raw)
    except json.JSONDecodeError:
        return reply(f"Could not parse arguments as JSON: {str(raw)[:200]}")

    try:
        return reply(await tool.handler(**arguments))
    except TypeError as exc:
        return reply(f"Wrong arguments for {name}: {exc}")
    except Exception as exc:  # a tool blowing up shouldn't kill the conversation
        log.exception("Tool %s failed", name)
        return reply(f"Tool {name} failed: {type(exc).__name__}: {exc}")


async def supports_tool_calling(
    base_url: str,
    model: str,
    *,
    timeout: float = 120.0,
    headers: dict[str, str] | None = None,
) -> bool:
    """Probe whether a model actually emits structured tool calls.

    Worth running once against any new model: the failure mode is silent, and a
    model that answers plausibly without ever calling a tool looks like a working
    agent right up until you check its citations.

    Returns False *only* for a server that answered and did not emit tool calls.
    Anything else -- refused connection, 502, timeout -- raises, because this
    used to swallow LLMError and return False, which `reldo doctor` rendered as
    "reachable but NO tool_calls". That sentence sent you looking at the model
    when the proxy in front of it was simply not running. A diagnostic that
    misreports which component is broken is worse than no diagnostic.

    Raises:
        LLMError: the endpoint could not be reached or refused the request.
    """
    probe = Tool(
        name="ping",
        description="Look up a fact. Call this whenever the user asks a question.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look up"}},
            "required": ["query"],
        },
        handler=lambda **_: _noop(),
    )
    async with ChatClient(base_url, model, timeout=timeout, headers=headers) as client:
        message = await client.complete(
            [{"role": "user", "content": "Look up the capital of France."}],
            tools=[probe],
        )
    return bool(message.get("tool_calls"))


async def _noop() -> str:
    return "ok"
