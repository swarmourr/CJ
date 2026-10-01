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
        """triggered / attempted_injections.

        Uses ``lifecycle.triggered`` from each record when present (set by the
        CJ DB query in the protocol).  Falls back to inferring from validity
        only for legacy records that pre-date the lifecycle field.  Inconclusive
        records are NOT counted as triggered — unknown is not confirmed.
        """
        fault = self.all_fault
        if not fault:
            return 0.0
        count = 0
        for r in fault:
            lc = r.get("lifecycle") or {}
            if isinstance(lc, dict):
                t = lc.get("triggered")
                if t is True:
                    count += 1
                elif t is None:
                    # lifecycle field absent or not set: infer conservatively
                    # Only "valid" implies confirmed trigger; inconclusive does not
                    if r.get("validity") == "valid":
                        count += 1
            # t is False → not triggered; inconclusive without lifecycle → skip
        return count / len(fault)

    @property
    def manifestation_rate(self) -> float | None:
        """manifested / triggered.

        Uses ``lifecycle.triggered`` and ``lifecycle.manifested`` when present.
        """
        triggered_records = []
        for r in self.all_fault:
            lc = r.get("lifecycle") or {}
            if isinstance(lc, dict):
                t = lc.get("triggered")
                if t is True:
                    triggered_records.append(r)
                elif t is None and r.get("validity") == "valid":
                    # Legacy: valid implies triggered
                    triggered_records.append(r)
        if not triggered_records:
            return None
        manifested = 0
        for r in triggered_records:
            lc = r.get("lifecycle") or {}
            if isinstance(lc, dict):
                mf = lc.get("manifested")
                if mf is True or r.get("validity") == "valid":
                    manifested += 1
        return manifested / len(triggered_records)

    @property
    def recovery_rate(self) -> float | None:
        """recovered / all_manifested.

        Counts all fault records where lifecycle.manifested is True, not just
        those in the valid bucket.  A triggered-but-invalid record that still
        manifested (proxy hit, response unmodified) counts in the denominator.
        Falls back to the valid bucket for legacy records without lifecycle field.
        """
        all_manifested = [
            r for r in self.all_fault
            if (r.get("lifecycle") or {}).get("manifested") is True
            or (
                (r.get("lifecycle") or {}).get("manifested") is None
                and r.get("validity") == "valid"
            )
        ]
        if not all_manifested:
            return None
        recovered = [
            r for r in all_manifested
            if (r.get("lifecycle") or {}).get("recovered") is True
        ]
        return len(recovered) / len(all_manifested)

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
