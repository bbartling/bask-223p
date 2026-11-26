"""Static checks for the Caddy / compose wiring (no Docker needed)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_caddyfile_issues_certs_for_ip_clients():
    text = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "{$EDGE_TLS_HOST::443}" in text
    assert "on_demand" in text  # a bare ':443' with plain tls internal fails for IP clients
    assert "reverse_proxy 127.0.0.1:8080" in text


def test_caddyfile_has_no_hard_coded_lan_ips():
    import re

    text = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    ips = re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)
    assert set(ips) <= {"127.0.0.1"}


def test_niagara_port_is_loopback_only_and_container_listens_on_all_interfaces():
    svc = _compose()["services"]["edge-niagara"]
    assert svc["ports"] == ["127.0.0.1:8080:8080"]
    cmd = svc["command"]
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"


def test_bacnet_service_stays_on_loopback_with_host_network():
    svc = _compose()["services"]["edge-bacnet"]
    assert svc["network_mode"] == "host"
    cmd = svc["command"]
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"


def test_caddy_receives_tls_host_with_catch_all_default():
    env = _compose()["services"]["caddy"]["environment"]
    assert env["EDGE_TLS_HOST"] == "${EDGE_TLS_HOST:-:443}"
