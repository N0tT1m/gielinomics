"""Isolate the test suite from whatever the developer has configured locally.

``Settings`` reads ``.env`` and the real environment by design, which means the
suite silently inherits both. That is not hypothetical: enabling tracing by
adding ``RELDO_TRACE_PROXY_URL`` to a local ``.env`` turned
``test_chat_goes_direct_when_tracing_is_off`` red, because the "tracing is off"
case was only ever true by accident of that file not mentioning it.

A test that passes or fails depending on a gitignored file is worse than a
failing test -- it passes in CI, fails on one machine, and the difference is
invisible in the diff. So strip ``RELDO_*`` from the environment for every test
and pass ``_env_file=None`` wherever a test builds Settings directly.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_local_reldo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [k for k in os.environ if k.startswith("RELDO_")]:
        monkeypatch.delenv(name, raising=False)
