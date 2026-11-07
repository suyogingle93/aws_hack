"""Publish harness activity to Amazon CloudWatch.

Counts are aggregated in memory and sent every `interval` seconds, so metrics cost one
PutMetricData call per interval no matter how busy the agents are.

Metrics (namespace configurable, default "AgentHarness"):
  Requests            total requests seen by the gateway
  Requests{Decision}  split into Decision=allowed / Decision=blocked
  AuthFailures        requests rejected for missing/invalid run credentials
  RunsStarted, RunsStopped, RunsFinished
  PolicyTerminations  runs killed automatically by a terminate_on_violation rule
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from datetime import datetime, timezone

import boto3

from .events import Event, EventBus

log = logging.getLogger("harness.metrics")

MetricKey = tuple[str, str | None]  # (metric name, Decision dimension or None)
_BATCH = 20  # safe PutMetricData batch size for every region/SDK version


def classify(event: Event) -> list[MetricKey]:
    """Metrics an event contributes to. Each request is counted exactly once."""
    if event.kind == "request":
        if event.decision == "allowed" and event.status_code is not None:
            return []  # the gateway's response update for an already-counted request
        if event.decision not in ("allowed", "blocked"):
            return []
        keys: list[MetricKey] = [("Requests", None), ("Requests", event.decision)]
        if event.status_code == 407:
            keys.append(("AuthFailures", None))
        return keys
    if event.kind == "run_started":
        return [("RunsStarted", None)]
    if event.kind == "run_finished":
        return [("RunsFinished", None)]
    if event.kind == "run_stopped":
        keys = [("RunsStopped", None)]
        if (event.reason or "").startswith("policy:"):
            keys.append(("PolicyTerminations", None))
        return keys
    return []


class CloudWatchMetrics:
    def __init__(
        self,
        bus: EventBus,
        namespace: str = "AgentHarness",
        region: str | None = None,
        interval: float = 60.0,
        client=None,
    ) -> None:
        self._bus = bus
        self.namespace = namespace
        self.interval = interval
        self._client = client or boto3.Session().client("cloudwatch", region_name=region)
        self._counts: Counter[MetricKey] = Counter()
        self._sub = None
        self._tasks: list[asyncio.Task] = []
        self.published = 0  # data points successfully sent

    def start(self) -> None:
        if self._sub is not None:
            return
        self._sub = self._bus.subscribe(maxsize=10_000)
        self._tasks = [
            asyncio.create_task(self._consume(), name="metrics-consume"),
            asyncio.create_task(self._tick(), name="metrics-flush"),
        ]

    async def _consume(self) -> None:
        assert self._sub is not None
        async for event in self._sub:
            self._counts.update(classify(event))

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.flush()

    def _metric_data(self, counts: Counter[MetricKey]) -> list[dict]:
        now = datetime.now(timezone.utc)
        data = []
        for (name, decision), value in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            datum = {"MetricName": name, "Value": float(value), "Unit": "Count", "Timestamp": now}
            if decision:
                datum["Dimensions"] = [{"Name": "Decision", "Value": decision}]
            data.append(datum)
        return data

    async def flush(self) -> None:
        if not self._counts:
            return
        counts, self._counts = self._counts, Counter()
        data = self._metric_data(counts)
        for i in range(0, len(data), _BATCH):
            batch = data[i : i + _BATCH]
            try:
                await asyncio.to_thread(self._client.put_metric_data, Namespace=self.namespace, MetricData=batch)
                self.published += len(batch)
            except Exception as e:  # noqa: BLE001
                log.warning("CloudWatch put_metric_data failed, will retry next interval: %s", e)
                # Put the unsent counts back so they are not lost.
                for datum in data[i:]:
                    dims = datum.get("Dimensions")
                    key = (datum["MetricName"], dims[0]["Value"] if dims else None)
                    self._counts[key] += int(datum["Value"])
                return

    async def stop(self) -> None:
        if self._sub is None:
            return
        self._sub.close()
        consume, tick = self._tasks
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick
        await consume  # drains queued events
        self._sub = None
        await self.flush()
