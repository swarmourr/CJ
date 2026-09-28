"""Activation evidence for a DistributedScenario run."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class MemberEvidence:
    """Timing record for one injection member."""

    host: str
    injection_id: str
    prepared_at: datetime | None = None
    active_at: datetime | None = None
    reverted_at: datetime | None = None
    clock_offset_ms: float | None = None
    manifested: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        def _fmt(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        return {
            "host": self.host,
            "injection_id": self.injection_id,
            "prepared_at": _fmt(self.prepared_at),
            "active_at": _fmt(self.active_at),
            "reverted_at": _fmt(self.reverted_at),
            "clock_offset_ms": self.clock_offset_ms,
            "manifested": self.manifested,
            "error": self.error,
        }


@dataclass
class GroupActivationEvidence:
    """Full synchronization evidence for one DistributedScenario run.

    After :meth:`finalize` is called the following fields are populated:

    * ``activation_skew_ms`` — max(t_i) - min(t_i) across members in ms.
    * ``synchronization_valid`` — True/False/None.
    * ``verdict`` — one of ``valid``, ``invalid``, ``inconclusive``,
      ``cancelled``, ``recovery_invalid``.
    """

    group_id: str
    requested_start: datetime | None = None
    members: list[MemberEvidence] = field(default_factory=list)
    activation_skew_ms: float | None = None
    maximum_allowed_skew_ms: float = 100.0
    synchronization_valid: bool | None = None
    strict_skew: bool = True
    verdict: str = "pending"

    @property
    def group_valid(self) -> bool:
        """Evaluate the compound group validity formula.

        .. math::

            Valid(G) = Ready(G) \\land Skew(G) \\leq \\varepsilon
                       \\land \\bigwedge_{i=1}^{n} Manifested(F_i)

        ``Manifested(F_i)`` is ``True`` or ``None`` (unknown, treated as
        valid for the formula).  A value of ``False`` fails the gate.

        Returns ``False`` if the verdict is ``cancelled`` or
        ``recovery_invalid``, even if skew and manifestation would pass.
        """
        if self.verdict in {"cancelled", "recovery_invalid", "pending"}:
            return False
        # Ready(G): no member failed during prepare
        ready = all(m.error is None for m in self.members)
        # Skew(G) ≤ ε: synchronization_valid is True or None (unset = unknown = ok)
        skew_ok = self.synchronization_valid is not False
        # ∧ Manifested(F_i): manifested is True or None for each member
        manifested_ok = all(m.manifested is not False for m in self.members)
        return ready and skew_ok and manifested_ok

    def compute_skew(self) -> float | None:
        """Return observed activation skew in milliseconds, or None."""
        times = [
            m.active_at.timestamp()
            for m in self.members
            if m.active_at is not None
        ]
        if len(times) < 2:
            return None
        return (max(times) - min(times)) * 1000.0

    def finalize(self) -> None:
        """Compute skew and set verdict. Call once, after all members finish."""
        if self.verdict in {"cancelled", "recovery_invalid"}:
            return

        self.activation_skew_ms = self.compute_skew()

        if self.activation_skew_ms is None:
            self.verdict = "inconclusive"
            self.synchronization_valid = None
        elif self.activation_skew_ms <= self.maximum_allowed_skew_ms:
            self.synchronization_valid = True
            self.verdict = "valid"
        else:
            self.synchronization_valid = False
            self.verdict = "invalid" if self.strict_skew else "inconclusive"

    def to_dict(self) -> dict:
        def _fmt(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        return {
            "group_id": self.group_id,
            "requested_start": _fmt(self.requested_start),
            "members": [m.to_dict() for m in self.members],
            "activation_skew_ms": self.activation_skew_ms,
            "maximum_allowed_skew_ms": self.maximum_allowed_skew_ms,
            "synchronization_valid": self.synchronization_valid,
            "verdict": self.verdict,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GroupActivationEvidence":
        def _parse_dt(s: str | None) -> datetime | None:
            if not s:
                return None
            return datetime.fromisoformat(s)

        members = [
            MemberEvidence(
                host=m["host"],
                injection_id=m["injection_id"],
                prepared_at=_parse_dt(m.get("prepared_at")),
                active_at=_parse_dt(m.get("active_at")),
                reverted_at=_parse_dt(m.get("reverted_at")),
                clock_offset_ms=m.get("clock_offset_ms"),
                manifested=m.get("manifested"),
                error=m.get("error"),
            )
            for m in d.get("members", [])
        ]
        ev = cls(
            group_id=d["group_id"],
            requested_start=_parse_dt(d.get("requested_start")),
            members=members,
            activation_skew_ms=d.get("activation_skew_ms"),
            maximum_allowed_skew_ms=d.get("maximum_allowed_skew_ms", 100.0),
            synchronization_valid=d.get("synchronization_valid"),
            verdict=d.get("verdict", "pending"),
        )
        return ev
