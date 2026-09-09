"""Settings routing, mostly around den-den-mushi tracing.

The bug these guard against is silent in both directions: route embeddings
through the proxy by accident and a wiki rebuild buries every useful trace under
~85k embed calls; drop the agent header and denden files everything under
"untagged", which is worthless the moment a second project shares the hub.
"""

from __future__ import annotations

import pytest

from reldo.config import Settings


def settings(**kw) -> Settings:
    # _env_file=None or these assertions read the developer's real .env; see
    # conftest.py for the failure that taught us.
    return Settings(_env_file=None, user_agent="reldo/test (local)", **kw)


# -- the user-agent gate ---------------------------------------------------
# Every wiki-touching command routes through require_user_agent, and nothing
# covered it: an edit that silently moved it out of the class body left every
# one of `build`, `search`, `ask` and `bot` raising AttributeError while the
# whole suite stayed green.


def test_require_user_agent_returns_the_configured_agent():
    assert settings().require_user_agent() == "reldo/test (local)"


def test_require_user_agent_refuses_to_run_unset():
    with pytest.raises(SystemExit, match="RELDO_USER_AGENT"):
        Settings(_env_file=None, user_agent="   ").require_user_agent()


# -- chat routing ----------------------------------------------------------


def test_chat_goes_direct_when_tracing_is_off():
    s = settings(ollama_api_url="http://192.168.1.78:11434")
    assert s.chat_base_url == "http://192.168.1.78:11434/v1"
    assert s.trace_headers == {}


def test_chat_goes_through_the_proxy_when_set():
    s = settings(
        ollama_api_url="http://192.168.1.78:11434",
        trace_proxy_url="http://127.0.0.1:8443",
    )
    assert s.chat_base_url == "http://127.0.0.1:8443/v1"


def test_proxy_wins_over_the_vllm_backend_too():
    """Tracing is transport-level, so it must not be an ollama-only path."""
    s = settings(ai_backend="vllm", trace_proxy_url="http://127.0.0.1:8443")
    assert s.chat_base_url == "http://127.0.0.1:8443/v1"


def test_v1_suffix_is_not_doubled():
    s = settings(trace_proxy_url="http://127.0.0.1:8443/v1/")
    assert s.chat_base_url == "http://127.0.0.1:8443/v1"


def test_embeddings_never_follow_the_proxy():
    """The whole point of a separate knob: rebuilds must not spam the hub."""
    s = settings(
        ollama_api_url="http://192.168.1.78:11434",
        trace_proxy_url="http://127.0.0.1:8443",
    )
    assert s.ollama_api_url == "http://192.168.1.78:11434"


# -- trace headers ---------------------------------------------------------


def test_agent_tag_is_sent_when_tracing():
    s = settings(trace_proxy_url="http://127.0.0.1:8443")
    assert s.trace_headers == {"X-DenDen-Agent": "reldo"}


def test_named_upstream_is_optional():
    s = settings(trace_proxy_url="http://127.0.0.1:8443", trace_upstream="goose")
    assert s.trace_headers["X-DenDen-Upstream"] == "goose"


def test_upstream_header_is_omitted_rather_than_blank():
    """An empty X-DenDen-Upstream is not the same as absent: denden looks the
    name up in its upstream map and errors when it isn't there."""
    s = settings(trace_proxy_url="http://127.0.0.1:8443")
    assert "X-DenDen-Upstream" not in s.trace_headers


def test_blank_agent_falls_back_rather_than_going_untagged():
    s = settings(trace_proxy_url="http://127.0.0.1:8443", trace_agent="")
    assert s.trace_headers == {"X-DenDen-Agent": "reldo"}
