"""Serve the UI contract on 127.0.0.1 with demo points so the Open-FDD suite can run."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from edge_microdot.app import create_app  # noqa: E402
from edge_microdot.cache import HotCache  # noqa: E402
from edge_microdot.config import LoadedConfig  # noqa: E402
from edge_microdot.fdd import FddEngine  # noqa: E402
from edge_microdot.history_store import HistoryStore  # noqa: E402
from edge_microdot.models import PointTarget  # noqa: E402
from edge_microdot.security import hash_password, load_auth_from_env  # noqa: E402
from edge_microdot.sources.fake import FakePollSource  # noqa: E402
from edge_microdot.sparql_kb import SparqlKb  # noqa: E402


def main() -> None:
    os.environ.setdefault("EDGE_SECRET_KEY", "contract-suite-secret-key-32-bytes-ok")
    os.environ.setdefault(
        "EDGE_USERS",
        "viewer:"
        + hash_password("viewer-demo")
        + ";operator:"
        + hash_password("operator-demo"),
    )
    os.environ.setdefault("EDGE_ROLES", "viewer:viewer,operator:operator")
    os.environ.setdefault("EDGE_RULES_WRITE", "0")
    targets = [
        PointTarget(
            site="demo",
            device="1",
            object_id="analog-input,1",
            units="degC",
            label="outside_air_temperature",
            extra={"role": "outside_air_temperature", "equipment": "ahu-1"},
        )
    ]
    history = HistoryStore(Path("/tmp/edge-contract.duckdb"))
    source = FakePollSource(targets)
    history.upsert_points(targets)
    history.stage(source.poll_once())
    history.flush()
    app = create_app(
        source=source,
        source_kind="fake",
        targets=targets,
        cfg=LoadedConfig(site="demo"),
        hot=HotCache("dict"),
        history=history,
        fdd=FddEngine(history=history, targets=targets),
        sparql=SparqlKb(None),
        auth=load_auth_from_env(),
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8791")), debug=False)


if __name__ == "__main__":
    main()
