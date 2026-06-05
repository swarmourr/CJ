"""
pytest plugin for chaos-jungle.

Provides the ``@pytest.mark.chaos(...)`` marker that injects HTTP-level faults
into test functions automatically — no ``with inject(...)`` boilerplate needed.

The plugin auto-discovers when ``chaos-jungle`` is installed (via the
``pytest11`` entry point).  No ``conftest.py`` changes required.

Usage
-----
::

    import pytest

    # Single fault
    @pytest.mark.chaos(Latency(3.0))
    def test_agent_handles_slow_llm(agent):
        result = agent.run("What is 2+2?")
        assert result is not None

    # Multiple faults stacked
    @pytest.mark.chaos(Latency(1.0), RateLimit(after_n=3))
    def test_agent_degrades_gracefully(agent):
        results = [agent.run("ping") for _ in range(6)]
        assert any(r is not None for r in results)

    # Restrict to one provider
    @pytest.mark.chaos(Unavailable(), urls=["api.openai.com"])
    def test_fallback_to_anthropic(agent):
        result = agent.run("hello")   # OpenAI fails; Anthropic unaffected
        assert result is not None

    # Async tests work identically
    @pytest.mark.chaos(Latency(2.0))
    async def test_async_agent(async_agent):
        result = await async_agent.run("hello")
        assert result is not None

Results
-------
When ``chaos_jungle.db`` is available the plugin automatically records
each test's pass/fail status and fault configuration to the session database
so results appear in the chaos-jungle dashboard.

::

    chaos-jungle list        # see the test session
    chaos-jungle dashboard   # browse results in the web UI
"""

from __future__ import annotations

from typing import Generator

import pytest

from chaos_jungle.intercept import inject, Behavior


# ── Plugin registration ───────────────────────────────────────────────────────

def pytest_configure(config: pytest.Config) -> None:
    """Register the ``chaos`` marker so pytest doesn't warn about unknown marks."""
    config.addinivalue_line(
        "markers",
        (
            "chaos(*behaviors, urls=None): "
            "inject HTTP-level LLM faults for the duration of this test. "
            "Accepts any chaos_jungle.intercept.Behavior instance "
            "(Latency, RateLimit, Unavailable, Timeout, CorruptResponse, …). "
            "Pass urls=[...] to restrict interception to specific hostnames."
        ),
    )


# ── Auto-fixture ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _chaos_inject(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """
    Auto-fixture activated for every test that carries ``@pytest.mark.chaos``.

    For tests without the marker this fixture is a transparent no-op.
    """
    marker = request.node.get_closest_marker("chaos")
    if marker is None:
        yield
        return

    behaviors: list[Behavior] = [b for b in marker.args if isinstance(b, Behavior)]
    urls: list[str] | None = marker.kwargs.get("urls", None)

    if not behaviors:
        yield
        return

    # Optionally record to SessionDB if available
    _record_start(request, behaviors)

    try:
        with inject(*behaviors, urls=urls):
            yield
        _record_result(request, passed=True)
    except Exception:
        _record_result(request, passed=False)
        raise


# ── Optional SessionDB recording ──────────────────────────────────────────────

def _record_start(request: pytest.FixtureRequest, behaviors: list[Behavior]) -> None:
    """Write a session entry to chaos_jungle.db if the DB module is available."""
    try:
        from chaos_jungle.db.session_db import SessionDB

        db = SessionDB()
        name = f"pytest::{request.node.nodeid}"
        session_id = db.start_session(name)
        fault_descs = ", ".join(repr(b) for b in behaviors)
        db.log_event(session_id, None, f"pytest chaos inject: {fault_descs}")
        # Stash so _record_result can close it
        request.node._cj_session_id = session_id  # type: ignore[attr-defined]
        request.node._cj_db = db                  # type: ignore[attr-defined]
    except Exception:
        pass  # DB unavailable — silent degradation


def _record_result(request: pytest.FixtureRequest, passed: bool) -> None:
    """Close the session entry started in _record_start."""
    try:
        session_id = request.node._cj_session_id   # type: ignore[attr-defined]
        db = request.node._cj_db                    # type: ignore[attr-defined]
        db.record_result(session_id, {"passed": int(passed)})
        db.stop_session(session_id, status="reverted" if passed else "failed")
    except Exception:
        pass
