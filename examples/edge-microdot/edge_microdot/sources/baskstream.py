"""Read-only baskStream polling source for edge ingest."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from bask_223p.bacnet.baskstream import (
    BaskStreamError,
    ReadOnlyBaskStreamClient,
    discover_station_snapshot,
    load_env_credentials,
)
from bask_223p.bacnet.refs import normalize_object_type

from edge_microdot.models import PointSample, PointTarget, make_point_ref, utc_now_iso


def _ts_from_ms(ms: Any) -> str:
    if isinstance(ms, (int, float)) and ms > 0:
        dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return utc_now_iso()


def _status_from_entry(entry: dict[str, Any]) -> str:
    if entry.get("ok") is False or entry.get("error"):
        return f"error:{entry.get('error') or 'read_failed'}"
    raw = str(entry.get("status") or "{ok}").strip()
    if raw in {"{ok}", "ok", ""}:
        return "ok"
    return f"status:{raw}"


class BaskStreamSource:
    def __init__(
        self,
        *,
        station: str | None = None,
        targets: Sequence[PointTarget] = (),
        env_file: Path | None = None,
        username: str | None = None,
        password: str | None = None,
        insecure: bool = False,
        send_auth_header: bool = False,
        base_ord: str = "slot:/Drivers/BacnetNetwork",
        site: str = "demo-site",
        timeout: int = 30,
        read_batch_size: int = 50,
        client_factory: Callable[[], ReadOnlyBaskStreamClient] | None = None,
    ) -> None:
        self.station = (station or os.environ.get("NIAGARA_URL") or "").rstrip("/")
        if not self.station:
            raise ValueError("Niagara station URL required (NIAGARA_URL or --station)")
        self.targets = list(targets)
        self.env_file = env_file
        self.username = username or os.environ.get("NIAGARA_USER")
        self.password = password or os.environ.get("NIAGARA_PASS")
        self.insecure = insecure
        self.send_auth_header = send_auth_header
        self.base_ord = base_ord
        self.site = site
        self.timeout = timeout
        self.read_batch_size = max(1, int(read_batch_size))
        self.client_factory = client_factory
        self._ord_to_target: dict[str, PointTarget] = {}
        self._reindex_targets()

    def _reindex_targets(self) -> None:
        self._ord_to_target = {t.ord_path: t for t in self.targets if t.ord_path}

    def _credentials(self) -> tuple[str, str]:
        if self.username and self.password:
            return self.username, self.password
        return load_env_credentials(self.env_file)

    def _open_client(self) -> ReadOnlyBaskStreamClient:
        if self.client_factory is not None:
            return self.client_factory()
        if self.insecure:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        user, pw = self._credentials()
        client = ReadOnlyBaskStreamClient(
            self.station,
            insecure=self.insecure,
            timeout=self.timeout,
            send_auth_header=self.send_auth_header,
        )
        client.login(user, pw)
        client.connect()
        client.capabilities()
        return client

    def discover_targets(self) -> list[PointTarget]:
        if self.insecure:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        user, pw = self._credentials()
        snap = discover_station_snapshot(
            self.station,
            base=self.base_ord,
            username=user,
            password=pw,
            insecure=self.insecure,
            timeout=self.timeout,
            ws_auth_header=self.send_auth_header,
        )
        discovered: list[PointTarget] = []
        for dev in snap.get("devices") or []:
            dev_inst = dev.get("device_instance")
            dev_label = str(dev_inst if dev_inst is not None else dev.get("name") or "")
            for pt in dev.get("points") or []:
                ord_path = str(pt.get("ord") or "")
                if not ord_path:
                    continue
                bac = pt.get("bacnet") or {}
                obj_id = str(bac.get("object_identifier") or "")
                if not obj_id and bac.get("object_type") and bac.get("object_instance") is not None:
                    obj_id = f"{normalize_object_type(str(bac['object_type']))},{bac['object_instance']}"
                label = str(pt.get("name") or "")
                if not obj_id:
                    obj_id = label or ord_path.rsplit("/", 1)[-1]
                raw_prop = str(bac.get("property_identifier") or "present-value")
                raw_prop = raw_prop.split("@", 1)[0].strip()
                prop_id = "present-value" if raw_prop in {"85", "present-value", ""} else raw_prop
                units = str((pt.get("facets") or {}).get("units") or "")
                ref = make_point_ref(
                    device=dev_label,
                    object_id=obj_id,
                    ord_path=ord_path,
                    point_name=label,
                )
                discovered.append(
                    PointTarget(
                        site=self.site,
                        device=dev_label,
                        object_id=obj_id,
                        property_id=prop_id,
                        units=units,
                        label=label,
                        ord_path=ord_path,
                        ref=ref,
                    )
                )
        self.targets = discovered
        self._reindex_targets()
        return discovered

    def _ensure_targets(self, client: ReadOnlyBaskStreamClient) -> list[PointTarget]:
        if self.targets and all(t.ord_path for t in self.targets):
            return self.targets
        if not self.targets:
            return self.discover_targets()
        if any(not t.ord_path for t in self.targets):
            discovered = self.discover_targets()
            by_key = {(t.device, t.object_id): t.ord_path for t in discovered}
            for t in self.targets:
                if not t.ord_path and (t.device, t.object_id) in by_key:
                    t.ord_path = by_key[(t.device, t.object_id)]
            self._reindex_targets()
        return self.targets

    def _entries_to_samples(
        self,
        entries: Sequence[dict[str, Any]],
        *,
        source_label: str,
        fallback_ts_ms: Any = None,
    ) -> list[PointSample]:
        samples: list[PointSample] = []
        for entry in entries:
            ord_path = str(entry.get("point") or "")
            t = self._ord_to_target.get(ord_path)
            site = t.site if t else self.site
            device = t.device if t else (ord_path.split("/")[3] if len(ord_path.split("/")) > 3 else "niagara")
            obj = t.object_id if t else ord_path.rsplit("/", 1)[-1]
            prop = t.property_id if t else "present-value"
            units = t.units if t else ""
            label = t.label if t else ""
            ref = t.ref if t else make_point_ref(device=device, object_id=obj, ord_path=ord_path, point_name=label)
            val = entry.get("value")
            samples.append(
                PointSample(
                    timestamp_utc=_ts_from_ms(entry.get("timestamp") or fallback_ts_ms),
                    site=site,
                    source=source_label,
                    device=device,
                    object=obj,
                    property=prop,
                    value=val,
                    units=units,
                    status=_status_from_entry(entry),
                    ref=ref,
                )
            )
        return samples

    def poll_once(self) -> list[PointSample]:
        with self._open_client() as client:
            targets = self._ensure_targets(client)
            ords = [t.ord_path for t in targets if t.ord_path]
            if not ords:
                return []
            samples: list[PointSample] = []
            for i in range(0, len(ords), self.read_batch_size):
                batch = ords[i : i + self.read_batch_size]
                resp = client.read(batch)
                samples.extend(
                    self._entries_to_samples(resp.get("points") or [], source_label="baskstream")
                )
            return samples

    async def poll_once_async(self) -> list[PointSample]:
        import asyncio

        return await asyncio.to_thread(self.poll_once)

    async def close(self) -> None:
        return None

    def stream_cov(
        self,
        on_samples: Callable[[list[PointSample]], None],
        *,
        stop_event: threading.Event | None = None,
        duration_s: float | None = None,
        poll_timeout_s: float = 1.0,
    ) -> int:
        total = 0
        deadline = (time.monotonic() + duration_s) if duration_s is not None else None
        with self._open_client() as client:
            targets = self._ensure_targets(client)
            ords = [t.ord_path for t in targets if t.ord_path]
            if not ords:
                return 0
            for i in range(0, len(ords), self.read_batch_size):
                sub_resp = client.subscribe(ords[i : i + self.read_batch_size])
                initial = self._entries_to_samples(sub_resp.get("points") or [], source_label="baskstream")
                if initial:
                    total += len(initial)
                    on_samples(initial)
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    evt = client.recv_event(timeout=poll_timeout_s)
                    if evt is None:
                        continue
                    op = evt.get("op")
                    if op == "cov":
                        batch = self._entries_to_samples(
                            evt.get("points") or [],
                            source_label="baskstream-cov",
                            fallback_ts_ms=evt.get("timestamp"),
                        )
                        if batch:
                            total += len(batch)
                            on_samples(batch)
                    elif op == "resync_required":
                        for i in range(0, len(ords), self.read_batch_size):
                            resp = client.read(ords[i : i + self.read_batch_size])
                            batch = self._entries_to_samples(resp.get("points") or [], source_label="baskstream")
                            if batch:
                                total += len(batch)
                                on_samples(batch)
            finally:
                try:
                    client.unsubscribe(ords)
                except BaskStreamError:
                    pass
        return total