"""ot-protocols.txt stays aligned with the pyproject pins."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _watch():
    path = ROOT / "scripts" / "ot_protocol_watch.py"
    spec = importlib.util.spec_from_file_location("ot_protocol_watch", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ot_protocol_pins_match_pyprojects():
    watch = _watch()
    pins = watch.parse_pins(watch.PIN_FILE.read_text(encoding="utf-8"))
    assert pins
    assert watch.verify(pins) == []


def test_report_flags_a_newer_release():
    watch = _watch()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b""

    def opener(url, timeout=30):
        name = url.rsplit("/", 2)[-2]
        payload = {"info": {"version": "9.9.9" if name == "bacpypes3" else "1.9.2"}}

        class _Body:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return _Body()

    # json.load needs a file-like with read(); urlopen response supports read via json.load.
    body = watch.report([("bacpypes3", "0.1.0"), ("websocket-client", "1.9.2")], opener=opener)
    assert body["updates"] == [{"name": "bacpypes3", "pinned": "0.1.0", "latest": "9.9.9"}]
