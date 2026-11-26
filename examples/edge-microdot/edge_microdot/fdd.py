"""Open-FDD-style SQL sensor sweeps (sql/fdd/rules/) plus snapshot API."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget, utc_now_iso

LOG = logging.getLogger(__name__)

RULE_FILE_IDS = {
    "sv_stale.sql": "SV-STALE",
    "sv_range.sql": "SV-RANGE",
    "sv_flatline.sql": "SV-FLATLINE",
    "sv_spike.sql": "SV-SPIKE",
}

_FAULT_STATUSES = frozenset({"FAULT", "STALE", "ERROR", "INCONCLUSIVE"})


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

    @classmethod
    def from_rule_row(cls, row: dict[str, Any]) -> "FaultResult":
        rule_id = str(row.get("rule_id") or "")
        status = str(row.get("status") or "")
        severity = "alarm" if status == "FAULT" else "warn" if status in _FAULT_STATUSES else "info"
        reason = row.get("reason") or status
        fault_hours = row.get("fault_hours")
        return cls(
            rule_id=rule_id,
            rule_name=rule_id,
            ref=str(row.get("subject_key") or row.get("series_id") or ""),
            severity=severity,
            message=str(reason),
            observed_at_utc=str(row.get("window_end") or row.get("observed_at_utc") or ""),
            value=fault_hours,
        )


class FddEngine:
    def __init__(
        self,
        *,
        history: HistoryStore,
        stale_minutes: float = 10.0,
        flatline_window: int = 6,
        flatline_epsilon: float = 1e-6,
        jump_threshold: float = 5.0,
        temp_lo: float = -40.0,
        temp_hi: float = 85.0,
        sql_dir: Path | None = None,
        targets: Sequence[PointTarget] | None = None,
        poll_seconds: float = 300.0,
        stale_after_hours: float | None = None,
        flatline_hours: float | None = None,
        flatline_tol: float | None = None,
    ) -> None:
        self.history = history
        self.stale_minutes = stale_minutes
        self.flatline_window = max(2, int(flatline_window))
        self.flatline_epsilon = flatline_epsilon
        self.jump_threshold = jump_threshold
        self.temp_lo = temp_lo
        self.temp_hi = temp_hi
        self.sql_dir = sql_dir or (Path(__file__).resolve().parent.parent / "sql" / "fdd" / "rules")
        self.targets = targets
        self.poll_seconds = max(1.0, float(poll_seconds))
        # Open-FDD parameter names (defaults derived from existing edge knobs).
        self.stale_after_hours = (
            float(stale_after_hours) if stale_after_hours is not None else max(stale_minutes, 0.0) / 60.0
        )
        self.flatline_hours = float(flatline_hours) if flatline_hours is not None else 1.0
        self.flatline_tol = float(flatline_tol) if flatline_tol is not None else (
            flatline_epsilon if flatline_epsilon >= 0.01 else 0.055556
        )
        self._lock = threading.Lock()
        self._last_results: list[FaultResult] = []
        self._last_run_utc: str | None = None

    def default_rule_params(self) -> dict[str, list[Any]]:
        return {
            "sv_stale.sql": [self.stale_after_hours, self.stale_after_hours],
            "sv_range.sql": [self.temp_lo, self.temp_hi],
            "sv_flatline.sql": [self.flatline_hours, self.flatline_tol],
            "sv_spike.sql": [self.jump_threshold],
        }

    def list_sql_rules(self) -> list[dict[str, Any]]:
        defaults = self.default_rule_params()
        out: list[dict[str, Any]] = []
        if not self.sql_dir.is_dir():
            return out
        for path in sorted(self.sql_dir.glob("*.sql")):
            out.append(
                {
                    "filename": path.name,
                    "name": RULE_FILE_IDS.get(path.name, path.stem.upper().replace("_", "-")),
                    "sql": path.read_text(encoding="utf-8"),
                    "default_params": defaults.get(path.name, []),
                }
            )
        return out

    def run(
        self,
        latest: Sequence[dict[str, Any]] | None = None,
        targets: Sequence[PointTarget] | None = None,
    ) -> list[FaultResult]:
        del latest  # rules are SQL-driven against in-memory DuckDB history
        eff_targets = list(targets if targets is not None else (self.targets or ()))
        if eff_targets:
            self.history.upsert_points(eff_targets)

        params_map = self.default_rule_params()
        official_rows: list[dict[str, Any]] = []
        run_ts = utc_now_iso()
        for path in sorted(self.sql_dir.glob("*.sql")) if self.sql_dir.is_dir() else []:
            params = params_map.get(path.name, [])
            rule_id = RULE_FILE_IDS.get(path.name, path.stem.upper().replace("_", "-"))
            try:
                rows = self.history.run_sql_file(path, params)
            except Exception:
                LOG.exception("FDD rule %s failed", path.name)
                continue
            for r in rows:
                fault_hours = r.get("fault_hours")
                covered_hours = r.get("covered_hours")
                try:
                    fh = float(fault_hours) if fault_hours is not None else 0.0
                except (TypeError, ValueError):
                    fh = 0.0
                status = str(r.get("status") or ("FAULT" if fh > 0 else "PASS"))
                official_rows.append(
                    {
                        "site_id": r.get("site_id") or "",
                        "equipment_id": r.get("equipment_id") or "edge",
                        "rule_id": str(r.get("rule_id") or rule_id),
                        "subject_key": r.get("series_id") or r.get("subject_key") or "",
                        "window_end": r.get("window_end") or run_ts,
                        "window_start": r.get("window_start"),
                        "status": status,
                        "reason": r.get("reason"),
                        "fault_hours": fault_hours,
                        "covered_hours": covered_hours,
                        "role": r.get("sensor_role") or r.get("role"),
                        "engine": "duckdb",
                        "poll_seconds": self.poll_seconds,
                    }
                )

        if official_rows:
            self.history.record_rule_results(
                official_rows, poll_seconds=self.poll_seconds, engine="duckdb"
            )

        results = [
            FaultResult.from_rule_row(r)
            for r in official_rows
            if str(r.get("status") or "").upper() in _FAULT_STATUSES
        ]
        observed = results[0].observed_at_utc if results else run_ts
        with self._lock:
            self._last_results = results
            self._last_run_utc = observed
        return results

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_run_utc": self._last_run_utc,
                "count": len(self._last_results),
                "faults": [f.to_dict() for f in self._last_results],
            }
