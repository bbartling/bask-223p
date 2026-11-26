"""Open-FDD-style SQL sweeps (files under sql/) plus snapshot API."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from edge_microdot.history_store import HistoryStore, _target_equipment
from edge_microdot.models import PointTarget, utc_now_iso


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
    def from_row(cls, row: dict[str, Any]) -> "FaultResult":
        return cls(
            rule_id=str(row.get("rule_id") or ""),
            rule_name=str(row.get("rule_name") or ""),
            ref=str(row.get("ref") or ""),
            severity=str(row.get("severity") or "info"),
            message=str(row.get("message") or ""),
            observed_at_utc=str(row.get("observed_at_utc") or ""),
            value=row.get("value"),
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
    ) -> None:
        self.history = history
        self.stale_minutes = stale_minutes
        self.flatline_window = max(2, int(flatline_window))
        self.flatline_epsilon = flatline_epsilon
        self.jump_threshold = jump_threshold
        self.temp_lo = temp_lo
        self.temp_hi = temp_hi
        self.sql_dir = sql_dir or (Path(__file__).resolve().parent.parent / "sql")
        self.targets = targets
        self._lock = threading.Lock()
        self._last_results: list[FaultResult] = []
        self._last_run_utc: str | None = None

    def default_rule_params(self) -> dict[str, list[Any]]:
        return {
            "01_stale.sql": [self.stale_minutes, self.stale_minutes],
            "02_out_of_range.sql": [self.temp_lo, self.temp_hi, self.temp_lo, self.temp_hi],
            "03_flatline.sql": [self.flatline_window, self.flatline_window, self.flatline_epsilon],
            "04_spike.sql": [self.jump_threshold, self.jump_threshold],
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
                    "name": path.stem,
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
        eff_targets = targets if targets is not None else (self.targets or ())
        target_by_ref = {t.ref: t for t in eff_targets if t.ref}
        results: list[FaultResult] = []
        params_map = self.default_rule_params()
        for path in sorted(self.sql_dir.glob("*.sql")) if self.sql_dir.is_dir() else []:
            params = params_map.get(path.name, [])
            try:
                rows = self.history.run_sql_file(path, params)
            except Exception:
                continue
            results.extend(FaultResult.from_row(r) for r in rows)

        run_ts = utc_now_iso()
        if results:
            fdd_rows = []
            for r in results:
                eq = _target_equipment(target_by_ref.get(r.ref), r.ref)
                detail = f"[{r.rule_id}/{r.severity}] {r.ref}: {r.message}"
                fdd_rows.append((r.observed_at_utc or run_ts, eq, r.rule_name or r.rule_id, True, detail))
            self.history.record_fdd_results(fdd_rows)

        observed = results[0].observed_at_utc if results else None
        with self._lock:
            self._last_results = results
            if observed:
                self._last_run_utc = observed
        return results

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_run_utc": self._last_run_utc,
                "count": len(self._last_results),
                "faults": [f.to_dict() for f in self._last_results],
            }
