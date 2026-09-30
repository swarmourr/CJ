"""Validity filtering for fault runs.

Classifies each fault-phase RunRecord into one of four categories:
  valid        — fault was confirmed active and manifested
  invalid      — fault failed to activate or was reverted incorrectly
  inconclusive — activation could not be verified either way
  untriggered  — fault was configured but never reached the agent

Never silently discards untriggered executions.

Primary resilience metrics use only valid records.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ValidityFilter:
    """Filters a list of RunRecords by validity classification.

    Attributes
    ----------
    valid, invalid, inconclusive, untriggered, baseline : list[dict]
        Partitioned records.
    trigger_rate : float
        triggered / attempted_injections
    manifestation_rate : float
        manifested / triggered  (None if no triggered records)
    recovery_rate : float
        recovered / manifested  (None if no manifested records)
    """

    valid:         list[dict]
    invalid:       list[dict]
    inconclusive:  list[dict]
    untriggered:   list[dict]
    baseline:      list[dict]

    @property
    def trigger_rate(self) -> float:
        fault = [r for r in self.all_fault if r["phase"] == "fault"]
        if not fault:
            return 0.0
        triggered = [r for r in fault if r.get("validity") in ("valid", "inconclusive")]
        return len(triggered) / len(fault)

    @property
    def manifestation_rate(self) -> float | None:
        triggered = [r for r in self.all_fault if r.get("validity") in ("valid", "inconclusive")]
        if not triggered:
            return None
        manifested = [r for r in triggered if r.get("validity") == "valid"]
        return len(manifested) / len(triggered)

    @property
    def recovery_rate(self) -> float | None:
        manifested = self.valid
        if not manifested:
            return None
        lc = [r.get("lifecycle", {}) for r in manifested]
        recovered = [l for l in lc if l.get("recovered") is True]
        return len(recovered) / len(manifested) if manifested else None

    @property
    def all_fault(self) -> list[dict]:
        return self.valid + self.invalid + self.inconclusive + self.untriggered


def classify_records(records: list[dict]) -> ValidityFilter:
    """Partition RunRecords by phase and validity.

    Parameters
    ----------
    records : list[dict]
        Raw dicts from RunRecord.to_dict() or loaded from JSONL.
    """
    baseline:     list[dict] = []
    valid:        list[dict] = []
    invalid:      list[dict] = []
    inconclusive: list[dict] = []
    untriggered:  list[dict] = []

    for r in records:
        phase    = r.get("phase", "baseline")
        validity = r.get("validity", "unchecked")

        if phase == "baseline":
            baseline.append(r)
        elif validity == "valid":
            valid.append(r)
        elif validity == "invalid":
            invalid.append(r)
        elif validity == "untriggered":
            untriggered.append(r)
        else:
            # inconclusive or unchecked → inconclusive bucket
            inconclusive.append(r)

    return ValidityFilter(
        valid=valid,
        invalid=invalid,
        inconclusive=inconclusive,
        untriggered=untriggered,
        baseline=baseline,
    )
