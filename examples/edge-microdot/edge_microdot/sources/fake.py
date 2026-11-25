"""Deterministic fake poll source for tests and demos."""
from __future__ import annotations

import math
import time
from typing import Sequence

from edge_microdot.models import PointSample, PointTarget, utc_now_iso


class FakePollSource:
    def __init__(self, targets: Sequence[PointTarget], *, period_s: float = 1.0) -> None:
        self.targets = list(targets)
        self.period_s = period_s
        self._tick = 0

    async def poll_once_async(self) -> list[PointSample]:
        self._tick += 1
        ts = utc_now_iso()
        out: list[PointSample] = []
        for i, t in enumerate(self.targets):
            base = 20.0 + (i % 10)
            val = base + math.sin(self._tick / self.period_s + i) * 2.5
            out.append(
                PointSample(
                    timestamp_utc=ts,
                    site=t.site,
                    source="fake",
                    device=t.device,
                    object=t.object_id,
                    property=t.property_id,
                    value=round(val, 3),
                    units=t.units or "C",
                    status="ok",
                    ref=t.ref,
                )
            )
        time.sleep(0)
        return out

    def poll_once(self) -> list[PointSample]:
        import asyncio

        return asyncio.run(self.poll_once_async())

    async def close(self) -> None:
        return None