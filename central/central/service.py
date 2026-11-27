"""Pull, store, merge, and report. No path in this module opens a controller."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import zipfile
from datetime import datetime, timezone
from typing import Mapping

from .config import CentralConfig, EdgeConfig
from .fetch import FetchError, pull_export, safe_error
from .merge import Merged, merge_models, stable_nquads, stable_trig, stable_turtle
from .reports import reconcile_edges, validate_union
from .store import (
    iter_latest,
    latest_model,
    list_snapshots,
    load_idempotency,
    save_snapshot,
    store_idempotency,
)
from .upload import UploadError, model_from_body

LOG = logging.getLogger("central")


class IdempotencyConflict(Exception):
    """The same Idempotency-Key was already used for a different body."""


def env_token(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise LookupError(f"{name} is unset")
    return value


def _zip_model(bundle: bytes, limit: int) -> bytes:
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile as exc:
        raise FetchError("export.zip is not a zip file") from exc
    with archive:
        names = [name for name in archive.namelist() if name == "model.ttl" or name.endswith("/model.ttl")]
        if len(names) != 1 or ".." in names[0].split("/"):
            raise FetchError("export.zip must contain one model.ttl")
        if archive.getinfo(names[0]).file_size > limit:
            raise FetchError("model.ttl exceeds max_upload_bytes")
        return archive.read(names[0])


class Collector:
    def __init__(self, cfg: CentralConfig, environ: Mapping[str, str] | None = None, *, sleep=None, urlopen=None):
        self.cfg = cfg
        self.environ = os.environ if environ is None else environ
        self.sleep = sleep
        self.urlopen = urlopen
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.merged: Merged | None = None
        self.reconcile_report: dict = {"pairs": []}
        self.validation_report: dict = {}
        self.errors: dict[tuple[str, str], str] = {}
        self.idempotency = load_idempotency(cfg.data_dir)

    def edge_by_key(self, site: str, name: str) -> EdgeConfig | None:
        for edge in self.cfg.edges:
            if edge.site == site and edge.name == name:
                return edge
        return None

    def rebuild(self) -> None:
        parts = iter_latest(self.cfg.data_dir)
        with self.lock:
            if not parts:
                self.merged = None
                self.reconcile_report = {"join_key": "", "pairs": []}
                self.validation_report = {"references": None, "shacl": {"status": "skipped", "reason": "no exports"}}
                return
            merged = merge_models(parts)
            self.merged = merged
            self.reconcile_report = reconcile_edges(merged)
            self.validation_report = validate_union(
                merged.union, shapes=self.cfg.shapes, ontology_dir=self.cfg.ontology_dir
            )
            self._write_reports()

    def _write_reports(self) -> None:
        folder = self.cfg.data_dir / "_reports"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "reconcile.json").write_text(
            json.dumps(self.reconcile_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (folder / "validation.json").write_text(
            json.dumps(self.validation_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def edge_status(self, edge: EdgeConfig) -> dict:
        current = latest_model(self.cfg.data_dir, edge.site, edge.name)
        snapshots = list_snapshots(self.cfg.data_dir, edge.site, edge.name)
        triples = None
        if self.merged is not None:
            for site, name, graph in self.merged.named:
                if site == edge.site and name == edge.name:
                    triples = len(graph)
        last_seen = None
        snapshot = None
        if current is not None:
            _path, meta = current
            last_seen = meta.get("received_at")
            snapshot = meta.get("snapshot")
        return {
            "name": edge.name,
            "site": edge.site,
            "base_url": edge.base_url,
            "last_seen": last_seen,
            "snapshot": snapshot,
            "snapshots_kept": len(snapshots),
            "triples": triples,
            "last_error": self.errors.get(edge.key),
        }

    def list_edges(self) -> dict:
        return {"edges": [self.edge_status(edge) for edge in self.cfg.edges]}

    def model_bytes(self, site: str, edge: str) -> bytes | None:
        current = latest_model(self.cfg.data_dir, site, edge)
        if current is None:
            return None
        path, _meta = current
        return path.read_bytes()

    def pull_edge(self, edge: EdgeConfig) -> dict:
        try:
            token = env_token(edge.token_env, self.environ)
        except LookupError as exc:
            self.errors[edge.key] = str(exc)
            raise
        kwargs = {
            "attempts": self.cfg.pull_attempts,
            "backoff_s": self.cfg.pull_backoff_s,
            "verify": edge.tls_verify,
        }
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        if self.urlopen is not None:
            kwargs["urlopen"] = self.urlopen
        try:
            bundle = pull_export(edge.base_url, token, **kwargs)
            model = _zip_model(bundle, self.cfg.max_upload_bytes)
        except FetchError as exc:
            self.errors[edge.key] = safe_error(exc)
            LOG.warning("pull %s/%s failed: %s", edge.site, edge.name, self.errors[edge.key])
            raise
        saved = save_snapshot(
            self.cfg.data_dir,
            edge.site,
            edge.name,
            model,
            bundle=bundle,
            source="pull",
            keep=self.cfg.keep,
        )
        self.errors.pop(edge.key, None)
        LOG.info("pull %s/%s snapshot %s unchanged=%s", edge.site, edge.name, saved.name, saved.unchanged)
        return {
            "site": edge.site,
            "edge": edge.name,
            "snapshot": saved.name,
            "unchanged": saved.unchanged,
            "model_sha256": saved.model_sha256,
        }

    def pull_all(self) -> dict:
        rows = []
        for edge in self.cfg.edges:
            try:
                rows.append(self.pull_edge(edge))
            except (FetchError, LookupError) as exc:
                rows.append({"site": edge.site, "edge": edge.name, "error": safe_error(exc)})
        self.rebuild()
        return {"pulled": rows}

    def accept_push(
        self,
        site: str,
        edge: str,
        body: bytes,
        content_type: str,
        idempotency_key: str | None,
    ) -> dict:
        if self.edge_by_key(site, edge) is None:
            raise KeyError(f"{site}/{edge}")
        model, bundle = model_from_body(body, content_type, max_bytes=self.cfg.max_upload_bytes)
        digest = hashlib.sha256(model).hexdigest()
        if idempotency_key:
            prior = self.idempotency.get(idempotency_key)
            if prior and prior.get("sha256") != digest:
                raise IdempotencyConflict(idempotency_key)
        saved = save_snapshot(
            self.cfg.data_dir,
            site,
            edge,
            model,
            bundle=bundle,
            source="push",
            keep=self.cfg.keep,
        )
        if idempotency_key:
            self.idempotency[idempotency_key] = {"sha256": digest, "snapshot": saved.name, "site": site, "edge": edge}
            store_idempotency(self.cfg.data_dir, self.idempotency)
        self.errors.pop((site, edge), None)
        self.rebuild()
        LOG.info("push %s/%s snapshot %s unchanged=%s", site, edge, saved.name, saved.unchanged)
        return {
            "site": site,
            "edge": edge,
            "snapshot": saved.name,
            "unchanged": saved.unchanged,
            "model_sha256": digest,
        }

    def turtle(self) -> bytes:
        if self.merged is None:
            return b""
        return stable_turtle(self.merged.union).encode("utf-8")

    def trig(self) -> bytes:
        if self.merged is None:
            return b""
        return stable_trig(self.merged).encode("utf-8")

    def nquads(self) -> bytes:
        if self.merged is None:
            return b""
        return stable_nquads(self.merged).encode("utf-8")

    def query(self, sparql: str):
        if not self.cfg.sparql:
            raise PermissionError("sparql is off")
        if self.merged is None:
            raise RuntimeError("no graph")
        import re

        if re.search(r"\b(insert|delete|load|clear|drop|create|copy|move|add)\b", sparql, re.I):
            raise ValueError("SPARQL updates are off")
        with self.lock:
            return self.merged.dataset.query(sparql)

    def start_schedule(self) -> None:
        if self._thread is not None:
            return

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    self.pull_all()
                except Exception as exc:
                    LOG.warning("scheduled pull failed: %s", safe_error(exc))
                self._stop.wait(self.cfg.interval_s)

        self._thread = threading.Thread(target=_loop, name="central-pull", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
