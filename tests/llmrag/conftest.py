# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Shared pytest fixtures for the unisi/llmrag.py unit tests.

These tests exercise llmrag.py in complete isolation: no network access, no
API key, and no running UNISI server/screens. The OpenAI client is replaced
with FakeLLMClient below, wired directly into the module-level
llmrag._acompletion slot — the exact same slot setup_llmrag() fills in for
real. Nothing here depends on any other test folder under tests/.

Note: `import unisi.llmrag` below runs unisi/__init__.py first (that's how
Python packages work), which imports unisi.utils — and unisi/utils.py
auto-creates a default config.py and a `log` file in the current working
directory at IMPORT TIME if config.py doesn't already exist there. That's
pre-existing unisi behaviour, unrelated to this test suite; harmless, and
one-time (it no-ops once config.py exists). If you'd rather it not touch
your CWD at all, run pytest from an empty/scratch directory once, or just
delete the generated config.py/log afterwards.

Run with:
    pip install pytest
    pytest tests/unit
"""
from __future__ import annotations

import asyncio
import copy
import functools
import sys
import types
from pathlib import Path

import pytest

# llmrag.py lives at <repo_root>/unisi/llmrag.py; this file lives at
# <repo_root>/tests/unit/conftest.py. Inserting <repo_root> explicitly here
# — rather than relying on pytest's own rootdir-insertion heuristics, which
# depend on --import-mode and on tests/ having no __init__.py — means these
# tests import correctly regardless of the working directory pytest is
# invoked from, and without requiring `pip install -e .` first.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import unisi.llmrag as llmrag  # noqa: E402
from unisi.common import Unishare  # noqa: E402


# ---------------------------------------------------------------------------
# Async test support (no pytest-asyncio dependency)
# ---------------------------------------------------------------------------

def run_async(coro_func):
    """
    Decorator that lets an `async def test_...` run under plain pytest, no
    pytest-asyncio plugin required — asyncio.run() under the hood. Used
    instead of @pytest.mark.asyncio so this whole test suite needs nothing
    beyond `pip install pytest`.

        @run_async
        async def test_something():
            result = await llmrag.Q(...)
            assert result == ...
    """
    @functools.wraps(coro_func)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_func(*args, **kwargs))
    return wrapper


# ---------------------------------------------------------------------------
# Isolation: llmrag.py keeps several pieces of process-lifetime global state
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_module_state():
    """
    Unishare (a shared singleton config object imported all over unisi),
    llmrag._incompatible_params (learned per-model quirks) and
    llmrag._acompletion (the active client) are all *intentionally*
    process-lifetime globals in production — never reset by the module
    itself. That means tests must reset them between cases, or one test's
    mutations (a mocked _acompletion, a model added to
    _incompatible_params, a cache pointed at a tmp dir) leak into the next
    test and make results depend on execution order.

    Autouse + snapshot/restore means every test starts from a clean slate
    with zero per-test boilerplate, and it generalises to any attribute
    (present or future) without needing to name each one individually.
    """
    unishare_snapshot = dict(Unishare.__dict__)
    incompatible_snapshot = copy.deepcopy(llmrag._incompatible_params)
    acompletion_snapshot = llmrag._acompletion

    yield

    Unishare.__dict__.clear()
    Unishare.__dict__.update(unishare_snapshot)
    llmrag._incompatible_params.clear()
    llmrag._incompatible_params.update(incompatible_snapshot)
    llmrag._acompletion = acompletion_snapshot


# ---------------------------------------------------------------------------
# Fake OpenAI client
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    """Mimics just the bit of an OpenAI ChatCompletion _call_llm reads:
    response.choices[0].message.content."""
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeBadRequestError(llmrag._BadRequestError):
    """
    A minimal stand-in for a real openai.BadRequestError. It IS a real
    _BadRequestError (subclassing it directly) so `except _BadRequestError`
    inside _call_llm still catches it — but its __init__ bypasses the real
    APIError/APIStatusError chain, which requires an actual HTTP
    Request/Response object to construct. Building one of those would
    couple these tests to exactly which transport library (httpx, or its
    newer httpx2 successor) the installed openai SDK version happens to
    depend on internally — orthogonal to anything this module does with
    the exception, which only ever reads .param / .code / .message.
    """
    def __init__(self, message: str, *, param: str | None = None, code: str | None = None):
        Exception.__init__(self, message)
        self.message = message
        self.param = param
        self.code = code
        self.type = 'invalid_request_error'
        self.body = {'message': message, 'param': param, 'code': code}
        self.status_code = 400


def make_bad_request_error(param=None, code='unsupported_value', message=None):
    """Convenience constructor: builds a FakeBadRequestError carrying just
    the fields _rejected_param() inspects."""
    message = message or f'Unsupported value: {param!r} is not supported with this model.'
    return FakeBadRequestError(message, param=param, code=code)


class FakeLLMClient:
    """
    Stand-in for AsyncOpenAI's `client.chat.completions.create`, an
    async-callable object wired directly into llmrag._acompletion so
    _call_llm's `await _acompletion(**kwargs)` calls this instead of the
    network.

    responses:  content strings to return, one per call, in order; the
                last entry repeats once the list is exhausted (so a
                single-element list means "always return this").
    errors:     optional {call_index: BaseException} — raised instead of
                returning a response on that (0-based) call index. Typically
                a FakeBadRequestError, to exercise _call_llm's retry logic.
    calls:      every kwargs dict `_acompletion(**kwargs)` was invoked
                with, in order — what assertions inspect to check exactly
                what _call_llm sent (model, messages, response_format, ...).
    """
    def __init__(self, responses=('{}',)):
        self.responses: list[str] = list(responses)
        self.errors: dict[int, BaseException] = {}
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        index = len(self.calls)
        # _call_llm builds `kwargs` once and MUTATES it in place across
        # retry attempts (del kwargs['temperature'], flips
        # response_format['json_schema']['strict'], ...) rather than
        # rebuilding a fresh dict each time. A plain `self.calls.append
        # (kwargs)` would store a live reference, so every entry in
        # `.calls` would alias the SAME dict and all show its FINAL state
        # after the whole retry loop finishes — not what was actually sent
        # on that particular attempt. Deep-copying here is what makes
        # `.calls[0]` correctly reflect attempt 0 even after attempt 1
        # mutates the original.
        self.calls.append(copy.deepcopy(kwargs))
        if index in self.errors:
            raise self.errors[index]
        content = self.responses[min(index, len(self.responses) - 1)]
        return FakeResponse(content)


@pytest.fixture
def fake_llm():
    """
    Installs a fresh FakeLLMClient as llmrag._acompletion and sets the
    minimal Unishare config _call_llm/Q/Qx read (model, temperature,
    strict_schema, extra_body, cache — cache off by default). Returns the
    client itself so tests can inspect `.calls`, queue `.errors`, or set
    `.responses` to whatever content the scenario needs.
    """
    client = FakeLLMClient()
    llmrag._acompletion = client
    Unishare.llm_model = 'test-model'
    Unishare.llm_temperature = 0.3
    Unishare.llm_strict_schema = True
    Unishare.llm_extra_body = None
    Unishare.llm_cache = None
    return client


@pytest.fixture
def fake_config(monkeypatch):
    """
    setup_llmrag() does `import config` — a plain top-level config.py a
    real UNISI app supplies on its own path (see tests/llm/config.py for
    the live-app equivalent). This fixture fabricates that module directly
    in sys.modules for the duration of one test, so setup_llmrag() picks it
    up with no dependency on CWD or a real file on disk. monkeypatch
    reverts sys.modules automatically when the test ends, so this can
    never leak a fake 'config' module into a later test.

    Usage:
        def test_x(fake_config):
            fake_config.llm = ['openai', 'gpt-4o']
            llmrag.setup_llmrag()
            ...
    """
    fake = types.ModuleType('config')
    monkeypatch.setitem(sys.modules, 'config', fake)
    return fake
