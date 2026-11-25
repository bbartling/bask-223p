"""Telemetry sources for live-ingest."""
from __future__ import annotations

from typing import Sequence

from ..models import PointSample
from .bacnet_rpm import BacnetRpmSource, DeviceInfo
from .baskstream_src import BaskStreamSource


class CompositeSource:
    """Runs multiple read-only sources (e.g. baskstream + bacnet) behind one interface."""

    def __init__(self, sources: Sequence[object]) -> None:
        self.sources = list(sources)

    async def poll_once_async(self) -> list[PointSample]:
        import asyncio

        all_samples: list[PointSample] = []
        for src in self.sources:
            if isinstance(src, BacnetRpmSource):
                all_samples.extend(await src.poll_once_async())
            else:
                all_samples.extend(await asyncio.to_thread(src.poll_once))
        return all_samples

    def poll_once(self) -> list[PointSample]:
        import asyncio

        return asyncio.run(self.poll_once_async())

    async def close(self) -> None:
        for src in self.sources:
            if isinstance(src, BacnetRpmSource):
                await src.close()


__all__ = ["BacnetRpmSource", "BaskStreamSource", "CompositeSource", "DeviceInfo"]
