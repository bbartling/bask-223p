"""Open-FDD-style SQL rules over hot latest + recent file history."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget


@dataclass(slots=True)
class FaultResult:
    rule_id: str
    rule_name: str
    ref: str
    severity: str
    message: str
    observed_at_utc: str
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "ref": self.ref,
            "severity": self.severity,
            "message": self.message,
            "observed_at_utc": self.observed_at_utc,
            "value": self.value,
        }


# Generic SI-ish guardrails when targets lack explicit bounds (room/duct temps °C scale).
_DEFAULT_MIN_MAX: dict[str, tuple[float, float]] = {
    "C": (-40.0, 85.0),
    "CEL": (-40.0, 85.0),
    "DEG_C": (-40.0, 85.0),
    "PA": (-500.0, 5000.0),
    "PCT": (0.0, 100.0),
    "%": (0.0, 100.0),
}


class FddEngine:
    def __init__(
        self,
        *,
        history: HistoryStore,
        stale_minutes: float = 10.0,
        flatline_window: int = 6,
        flatline_epsilon: float = 1e-6,
    ) -> None:
        self.history = history
        self.stale_minutes = stale_minutes
        self.flatline_window = max(2, int(flatline_window))
        self.flatline_epsilon = flatline_epsilon
        self._lock = threading.Lock()
        self._last_results: list[FaultResult] = []
        self._last_run_utc: str | None = None

    def run(self, latest: Sequence[dict[str, Any]], targets: Sequence[PointTarget]) -> list[FaultResult]:
        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        stale_cutoff = now - timedelta(minutes=self.stale_minutes)
        bounds_by_ref = {t.ref: (t.min_value, t.max_value) for t in targets if t.ref}

        results: list[FaultResult] = []
        for row in latest:
            ref = str(row.get("ref") or "")
            if not ref:
                continue
            ts_raw = row.get("timestamp_utc") or ""
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = now
            status = str(row.get("status") or "ok")
            val = row.get("value")
            if row.get("value_num") is not None:
                val = row["value_num"]

            if status != "ok":
                results.append(
                    FaultResult(
                        rule_id="R-STATUS",
                        rule_name="bad_status",
                        ref=ref,
                        severity="warn",
                        message=f"Point status is {status}",
                        observed_at_utc=now_iso,
                        value=val,
                    )
                )

            if ts < stale_cutoff:
                results.append(
                    FaultResult(
                        rule_id="R-STALE",
                        rule_name="stale_data",
                        ref=ref,
                        severity="warn",
                        message=f"No fresh sample for {self.stale_minutes:g} min",
                        observed_at_utc=now_iso,
                        value=val,
                    )
                )

            if isinstance(val, (int, float)):
                unit_key = str(row.get("units") or "").upper().replace("°", "DEG_")
                lo, hi = bounds_by_ref.get(ref, (None, None))
                if lo is None or hi is None:
                    lo, hi = _DEFAULT_MIN_MAX.get(unit_key, (-1e9, 1e9))
                if val < lo or val > hi:
                    results.append(
                        FaultResult(
                            rule_id="R-RANGE",
                            rule_name="out_of_range",
                            ref=ref,
                            severity="alarm",
                            message=f"Value {val} outside [{lo}, {hi}] ({unit_key or 'unitless'})",
                            observed_at_utc=now_iso,
                            value=val,
                        )
                    )

                recent = self.history.query_history(ref=ref, limit=self.flatline_window)
                nums = [
                    float(r["value_num"])
                    for r in recent
                    if r.get("value_num") is not None
                ]
                if len(nums) >= self.flatline_window:
                    spread = max(nums) - min(nums)
                    if spread <= self.flatline_epsilon:
                        results.append(
                            FaultResult(
                                rule_id="R-FLAT",
                                rule_name="sensor_flatline",
                                ref=ref,
                                severity="info",
                                message=f"Last {len(nums)} samples unchanged (spread={spread:g})",
                                observed_at_utc=now_iso,
                                value=val,
                            )
                        )

        with self._lock:
            self._last_results = results
            self._last_run_utc = now_iso
        return results

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_run_utc": self._last_run_utc,
                "count": len(self._last_results),
                "faults": [f.to_dict() for f in self._last_results],
            }