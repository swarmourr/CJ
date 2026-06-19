"""Multi-turn conversation fault injection for chaos-jungle.

Inspired by the ``Turn`` + ``BaselineScenario`` API in agent-chaos.
Models a multi-step agent conversation where individual turns can have
different fault behaviors injected — enabling surgical, per-turn chaos
without modifying agent code.

Usage
-----
::

    from chaos_jungle.conversation import ConversationScenario, Turn, TurnResult
    from chaos_jungle.intercept import Latency, RateLimit, ToolMutate

    def my_agent(message: str, history: list[dict]) -> str:
        # call your agent here — receives the current message and full history
        return openai_client.chat.completions.create(...).choices[0].message.content

    scenario = ConversationScenario(
        name="flight-booking-flow",
        turns=[
            # Turn 1 — baseline, no chaos
            Turn("Search for flights from NYC to Paris on July 10"),

            # Turn 2 — inject rate limit after the first LLM call in this turn
            Turn(
                "Book the cheapest business-class option",
                chaos=[RateLimit(after_n=0)],
                chaos_after_n=1,
                expected="sorry",          # agent should degrade gracefully
            ),

            # Turn 3 — dynamic input built from previous response
            Turn(
                lambda prev: f"Confirm booking ref from: {prev[:60]}",
                chaos=[ToolMutate(tool_name="confirm_booking", mode="wrong_type")],
            ),
        ],
    )

    results = scenario.run(my_agent)
    print(scenario.summary(results))
    assert scenario.assert_all_complete(results)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Any

from chaos_jungle.intercept import inject, Behavior


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """A single turn in a multi-turn agent conversation.

    Parameters
    ----------
    input : str | Callable[[str], str]
        The user message for this turn.  Can be a static string or a
        one-argument callable that receives the *previous* turn's response
        and returns a string — useful for dynamic follow-up questions.
    chaos : list[Behavior], optional
        Fault behaviors to inject during THIS turn only.  The behaviors
        are activated via :func:`~chaos_jungle.intercept.inject` for the
        duration of the turn's agent call, then automatically removed.
    chaos_after_n : int, optional
        Skip the first *n* LLM calls within this turn before activating
        *chaos*.  Maps directly to ``after_n_calls`` in :func:`~chaos_jungle.intercept.inject`.
        Default ``0`` = activate from the very first call.
    expected : str | None, optional
        Optional substring check on the turn response.  Used by
        :meth:`ConversationScenario.assert_expected` — the turn passes if
        ``expected.lower()`` appears anywhere in the response (case-insensitive).
    urls : list[str] | None, optional
        URL patterns to intercept for this turn's chaos.  Defaults to all
        LLM API hosts when ``None``.

    Examples
    --------
    Static input::

        Turn("What is the cheapest flight to Paris?")

    Dynamic input (builds on previous answer)::

        Turn(lambda prev: f"Book the option you mentioned: {prev[:40]}")

    Chaos on turn 2, skip first call::

        Turn("Confirm booking", chaos=[RateLimit(after_n=0)], chaos_after_n=1)

    Assert graceful degradation::

        Turn("Try again", chaos=[Unavailable()], expected="unable to complete")
    """

    input: str | Callable[[str], str]
    chaos: list[Behavior] = field(default_factory=list)
    chaos_after_n: int = 0
    expected: str | None = None
    urls: list[str] | None = None

    def get_input(self, prev_response: str = "") -> str:
        """Resolve the turn input, calling the lambda if needed."""
        if callable(self.input):
            return self.input(prev_response)
        return self.input


@dataclass
class TurnResult:
    """Result of a single conversation turn.

    Attributes
    ----------
    turn_index : int
        Zero-based position of this turn in the conversation.
    input : str
        The resolved user message that was sent.
    response : str
        The agent's response (empty string on failure).
    completed : bool
        ``True`` if the agent returned a response without raising.
    error : str
        Exception message if ``completed`` is ``False``, else ``""``.
    chaos_active : bool
        ``True`` if fault behaviors were injected during this turn.
    expected_passed : bool | None
        Result of the ``expected`` substring check, or ``None`` if no
        ``expected`` was set for this turn.
    latency_s : float
        Wall-clock time for the agent call, in seconds.
    """

    turn_index: int
    input: str
    response: str
    completed: bool
    error: str = ""
    chaos_active: bool = False
    expected_passed: bool | None = None
    latency_s: float = 0.0


# ---------------------------------------------------------------------------
# ConversationScenario
# ---------------------------------------------------------------------------


class ConversationScenario:
    """Run a multi-turn agent conversation with per-turn fault injection.

    Manages turn-by-turn conversation flow.  Each :class:`Turn` can
    optionally inject its own set of :class:`~chaos_jungle.intercept.Behavior`
    faults, scoped to that turn alone.

    Parameters
    ----------
    name : str
        Human-readable scenario name (shown in summaries and dashboard).
    turns : list[Turn]
        Ordered list of conversation turns to execute.

    Examples
    --------
    ::

        scenario = ConversationScenario(
            name="hotel-search",
            turns=[
                Turn("Find a 4-star hotel in Paris under €200/night"),
                Turn("Book room 203 for 3 nights",
                     chaos=[ToolMutate(tool_name="book_room", mode="null")],
                     expected="error"),
                Turn("Try a different hotel instead"),
            ],
        )
        results = scenario.run(my_agent)
        print(scenario.summary(results))
    """

    def __init__(self, name: str, turns: list[Turn]) -> None:
        if not turns:
            raise ValueError("ConversationScenario requires at least one Turn.")
        self.name  = name
        self.turns = turns

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def run(
        self,
        agent: Callable[[str, list[dict[str, Any]]], str],
        *,
        stop_on_failure: bool = False,
    ) -> list[TurnResult]:
        """Execute the conversation turn by turn.

        Parameters
        ----------
        agent : Callable[[str, list[dict]], str]
            Function that accepts ``(current_input: str, history: list[dict])``
            and returns the agent's response string.
            ``history`` is a list of ``{"input": ..., "response": ...}``
            dicts representing all previous turns.
        stop_on_failure : bool, optional
            If ``True``, stop executing turns as soon as one fails (raises an
            exception).  Default ``False`` — all turns are always attempted.

        Returns
        -------
        list[TurnResult]
            One :class:`TurnResult` per executed turn (may be fewer than
            ``len(self.turns)`` when *stop_on_failure* is ``True``).
        """
        import time

        history: list[dict[str, Any]] = []
        results: list[TurnResult]     = []
        prev_response = ""

        for i, turn in enumerate(self.turns):
            user_input   = turn.get_input(prev_response)
            chaos_active = bool(turn.chaos)

            t0 = time.time()
            try:
                if turn.chaos:
                    inject_kwargs: dict[str, Any] = {"after_n_calls": turn.chaos_after_n}
                    if turn.urls is not None:
                        inject_kwargs["urls"] = turn.urls
                    with inject(*turn.chaos, **inject_kwargs):
                        response = agent(user_input, list(history))
                else:
                    response = agent(user_input, list(history))

                latency_s = time.time() - t0
                completed = True
                error     = ""
            except Exception as exc:
                latency_s = time.time() - t0
                response  = ""
                completed = False
                error     = str(exc)

            # Check expected substring
            expected_passed: bool | None = None
            if turn.expected is not None:
                expected_passed = turn.expected.lower() in response.lower()

            tr = TurnResult(
                turn_index=i,
                input=user_input,
                response=response,
                completed=completed,
                error=error,
                chaos_active=chaos_active,
                expected_passed=expected_passed,
                latency_s=round(latency_s, 4),
            )
            results.append(tr)
            history.append({"input": user_input, "response": response})
            prev_response = response

            if not completed and stop_on_failure:
                break

        return results

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def assert_all_complete(self, results: list[TurnResult]) -> bool:
        """Return ``True`` if every executed turn completed without raising."""
        return all(r.completed for r in results)

    def assert_all_turns_ran(self, results: list[TurnResult]) -> bool:
        """Return ``True`` if all defined turns were executed (none skipped)."""
        return len(results) == len(self.turns)

    def assert_expected(self, results: list[TurnResult]) -> list[tuple[int, bool]]:
        """Check ``expected`` substrings for all turns that define one.

        Returns
        -------
        list[tuple[int, bool]]
            ``(turn_index, passed)`` pairs — only for turns that set an
            ``expected`` value.
        """
        return [
            (r.turn_index, bool(r.expected_passed))
            for r in results
            if r.expected_passed is not None
        ]

    def passed(self, results: list[TurnResult]) -> bool:
        """Return ``True`` if all turns completed AND all ``expected`` checks passed."""
        if not self.assert_all_complete(results):
            return False
        for _, ok in self.assert_expected(results):
            if not ok:
                return False
        return True

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self, results: list[TurnResult]) -> str:
        """Human-readable summary of the conversation results.

        Parameters
        ----------
        results : list[TurnResult]
            Output from :meth:`run`.

        Returns
        -------
        str
            Multi-line formatted summary.
        """
        lines = [f"ConversationScenario: {self.name}"]
        ran   = len(results)
        total = len(self.turns)
        ok    = sum(1 for r in results if r.completed)
        lines.append(f"  Turns: {ran}/{total} executed, {ok}/{ran} completed")

        for r in results:
            status = "OK  " if r.completed else "FAIL"
            chaos  = " [chaos]" if r.chaos_active else "        "
            lat    = f"{r.latency_s:.2f}s"
            inp    = repr(r.input[:50])
            lines.append(f"  [{status}]{chaos} Turn {r.turn_index + 1:>2} {lat:>7}  {inp}")
            if r.response:
                lines.append(f"           → {repr(r.response[:70])}")
            if r.expected_passed is not None:
                flag = "✓" if r.expected_passed else "✗"
                exp  = repr(self.turns[r.turn_index].expected)
                lines.append(f"           {flag} expected {exp}")
            if r.error:
                lines.append(f"           ✗ {r.error}")

        overall = "PASSED" if self.passed(results) else "FAILED"
        lines.append(f"  Overall: {overall}")
        return "\n".join(lines)
