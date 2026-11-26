from __future__ import annotations

import email.message
import importlib.util
import urllib.error
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_openfdd.py"
_SPEC = importlib.util.spec_from_file_location("fetch_openfdd", _PATH)
fetch_openfdd = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(fetch_openfdd)


def test_missing_release_is_not_a_pin(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fetch_openfdd, "VENDOR", tmp_path)
    monkeypatch.setattr(fetch_openfdd, "version", lambda: "5.0.22")

    def _boom(url, dest):
        raise urllib.error.HTTPError(url, 404, "missing", email.message.Message(), None)

    monkeypatch.setattr(fetch_openfdd, "_download", _boom)
    assert fetch_openfdd.fetch(require=False) == 0
    assert "no release asset" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no release asset"):
        fetch_openfdd.fetch(require=True)


def test_sha256_mismatch_aborts(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_openfdd, "VENDOR", tmp_path)
    monkeypatch.setattr(fetch_openfdd, "version", lambda: "5.0.22")

    def _write(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith(".sha256"):
            dest.write_text("a" * 64 + "  file\n", encoding="utf-8")
        else:
            dest.write_bytes(b"not-the-asset")

    monkeypatch.setattr(fetch_openfdd, "_download", _write)
    with pytest.raises(SystemExit, match="sha256"):
        fetch_openfdd.fetch(require=True)
