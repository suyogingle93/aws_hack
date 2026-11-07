"""DynamoDB audit store, mirroring, and CloudWatch metrics, against moto (no real AWS)."""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

import harness.store.dynamodb as dynamodb_module
from harness.events import Event, EventBus
from harness.main import Backend, Settings
from harness.metrics import CloudWatchMetrics, classify
from harness.store import RunRecord, SqliteStore
from harness.store.dynamodb import AuditTableMissing, DynamoDBStore, ensure_table
from harness.store.mirror import MirroredStore

REGION = "us-east-1"
TABLE = "audit-test"
T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch):
    """Fake credentials + moto for every test: these tests can never reach a real account."""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield


@pytest.fixture
def dyn():
    ensure_table(TABLE, region=REGION)
    return DynamoDBStore(TABLE, region=REGION)


def ev(run_id="r1", **kw):
    return Event(run_id=run_id, **kw)


def raw_items():
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE).scan()["Items"]


# --- table setup ------------------------------------------------------------------

def test_ensure_table_is_idempotent_and_enables_ttl():
    assert ensure_table(TABLE, region=REGION) is True
    assert ensure_table(TABLE, region=REGION) is False
    client = boto3.client("dynamodb", region_name=REGION)
    desc = client.describe_table(TableName=TABLE)["Table"]
    assert desc["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST"
    assert desc["GlobalSecondaryIndexes"][0]["IndexName"] == "runs-by-start"
    ttl = client.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]
    assert ttl["TimeToLiveStatus"] == "ENABLED" and ttl["AttributeName"] == "expires_at"


def test_missing_table_gives_an_actionable_error():
    with pytest.raises(AuditTableMissing, match="harness aws-setup"):
        DynamoDBStore("nope", region=REGION).check()


# --- events -----------------------------------------------------------------------

def test_event_roundtrip_and_response_update_is_an_upsert(dyn):
    e = ev(host="api.github.com", decision="allowed", headers={"Accept": "*/*"}, ts=T0)
    dyn.put_event(e)
    dyn.put_event(e.model_copy(update={"status_code": 200}))
    got = dyn.list_events("r1")
    assert len(got) == 1 and got[0].status_code == 200 and got[0].headers == {"Accept": "*/*"}


def test_items_are_readable_in_the_console(dyn):
    dyn.put_event(ev(host="pastebin.com", method="POST", decision="blocked", rule_id="block-paste-sites", status_code=403))
    item = raw_items()[0]
    assert item["pk"] == "RUN#r1" and item["sk"].startswith("EVT#")
    assert item["decision"] == "blocked" and item["host"] == "pastebin.com" and item["rule_id"] == "block-paste-sites"
    assert "expires_at" not in item  # no retention configured


def test_time_range_query_and_ordering(dyn):
    for i in range(6):
        dyn.put_event(ev(host=f"h{i}", ts=T0 + timedelta(seconds=i)))
    dyn.put_event(ev("r2", host="other", ts=T0))
    assert [e.host for e in dyn.list_events("r1")] == [f"h{i}" for i in range(6)]
    mid = dyn.list_events("r1", since=T0 + timedelta(seconds=1), until=T0 + timedelta(seconds=3))
    assert [e.host for e in mid] == ["h1", "h2", "h3"]  # both ends inclusive


def test_queries_paginate_up_to_the_limit(dyn, monkeypatch):
    monkeypatch.setattr(dynamodb_module, "_PAGE", 3)
    for i in range(10):
        dyn.put_event(ev(host=f"h{i}", ts=T0 + timedelta(seconds=i)))
    assert [e.host for e in dyn.list_events("r1", limit=7)] == [f"h{i}" for i in range(7)]
    assert len(dyn.list_events("r1", limit=100)) == 10


def test_retention_sets_ttl_attribute():
    ensure_table(TABLE, region=REGION)
    store = DynamoDBStore(TABLE, region=REGION, retention_days=30)
    store.put_event(ev())
    expires = int(raw_items()[0]["expires_at"])
    assert abs(expires - (time.time() + 30 * 86400)) < 60


# --- runs -------------------------------------------------------------------------

def test_runs_roundtrip_newest_first_and_update(dyn):
    older = RunRecord(id="a", command=["x"], cwd="/", started_at=T0)
    newer = RunRecord(id="b", command=["y"], cwd="/", started_at=T0 + timedelta(minutes=1))
    dyn.put_run(older)
    dyn.put_run(newer)
    dyn.put_run(older.model_copy(update={"status": "stopped", "exit_code": 143}))
    assert [r.id for r in dyn.list_runs()] == ["b", "a"]
    assert dyn.get_run("a").status == "stopped"
    assert dyn.get_run("missing") is None


# --- mirroring --------------------------------------------------------------------

def test_mirror_writes_to_both_and_reads_locally(dyn):
    local = SqliteStore(":memory:")
    store = MirroredStore(local, dyn)
    run = RunRecord(id="r1", command=["agent"], cwd="/", started_at=T0)
    store.put_run(run)
    store.put_event(ev(host="x.com", ts=T0))
    assert store.flush()
    assert dyn.get_run("r1") is not None and len(dyn.list_events("r1")) == 1
    assert store.mirrored == 2

    dyn.list_events = lambda *a, **k: pytest.fail("reads must not go to DynamoDB")
    assert [e.host for e in store.list_events("r1")] == ["x.com"]
    store.close()


class SlowFlaky(SqliteStore):
    def __init__(self, fail_times=0, delay=0.0):
        super().__init__(":memory:")
        self.fail_times, self.delay = fail_times, delay

    def put_event(self, event):
        time.sleep(self.delay)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("throttled")
        super().put_event(event)


def test_slow_mirror_never_blocks_the_caller():
    store = MirroredStore(SqliteStore(":memory:"), SlowFlaky(delay=0.3))
    start = time.monotonic()
    for i in range(5):
        store.put_event(ev(host=f"h{i}"))
    assert time.monotonic() - start < 0.1  # the gateway/kill switch path stays fast
    assert len(store.list_events("r1")) == 5  # and the local copy is immediate
    store.close()  # flushes the queue
    assert store.mirrored == 5


def test_mirror_retries_then_gives_up_without_losing_the_local_copy():
    flaky = SlowFlaky(fail_times=2)
    store = MirroredStore(SqliteStore(":memory:"), flaky, retries=3, backoff=0.01)
    store.put_event(ev(host="retried"))
    assert store.flush()
    assert store.mirrored == 1 and store.failed == 0

    broken = SlowFlaky(fail_times=99)
    store2 = MirroredStore(SqliteStore(":memory:"), broken, retries=2, backoff=0.01)
    store2.put_event(ev(host="lost-remotely"))
    assert store2.flush()
    assert store2.failed == 1
    assert [e.host for e in store2.list_events("r1")] == ["lost-remotely"]
    store.close()
    store2.close()


def test_mirror_preserves_write_order(dyn):
    store = MirroredStore(SqliteStore(":memory:"), dyn)
    e = ev(host="x", decision="allowed", ts=T0)
    store.put_event(e)
    store.put_event(e.model_copy(update={"status_code": 200}))  # the update must win
    store.close()
    assert dyn.list_events("r1")[0].status_code == 200


def test_mirror_drops_instead_of_blocking_when_far_behind():
    gate = threading.Event()

    class Stuck(SqliteStore):
        def put_event(self, event):
            gate.wait(5)

    store = MirroredStore(SqliteStore(":memory:"), Stuck(":memory:"), maxsize=2)
    for i in range(6):
        store.put_event(ev(host=f"h{i}"))
    assert store.dropped >= 1
    assert len(store.list_events("r1")) == 6
    gate.set()
    store.close()


# --- metrics ----------------------------------------------------------------------

def test_each_request_is_counted_once():
    sent = ev(decision="allowed")
    assert classify(sent) == [("Requests", None), ("Requests", "allowed")]
    assert classify(sent.model_copy(update={"status_code": 200})) == []  # the response update
    assert classify(ev(decision="blocked", status_code=403)) == [("Requests", None), ("Requests", "blocked")]
    assert ("AuthFailures", None) in classify(ev(decision="blocked", status_code=407))


def test_lifecycle_metrics():
    assert classify(ev(kind="run_started")) == [("RunsStarted", None)]
    assert classify(ev(kind="run_finished")) == [("RunsFinished", None)]
    assert classify(ev(kind="run_stopped", reason="operator: demo")) == [("RunsStopped", None)]
    assert ("PolicyTerminations", None) in classify(ev(kind="run_stopped", reason="policy: rule 'x' requires termination"))
    assert classify(ev(kind="policy_reloaded", run_id="system")) == []


def _sum(cw, name, decision=None):
    dims = [{"Name": "Decision", "Value": decision}] if decision else []
    now = datetime.now(timezone.utc)
    points = cw.get_metric_statistics(
        Namespace="AgentHarnessTest", MetricName=name, Dimensions=dims,
        StartTime=now - timedelta(minutes=10), EndTime=now + timedelta(minutes=10),
        Period=60, Statistics=["Sum"],
    )["Datapoints"]
    return sum(p["Sum"] for p in points)


class SpyClient:
    """Records what is sent, and forwards it to the (moto) CloudWatch client."""

    def __init__(self, inner):
        self.inner, self.sent = inner, {}

    def put_metric_data(self, Namespace, MetricData):
        for d in MetricData:
            dims = d.get("Dimensions")
            key = (d["MetricName"], dims[0]["Value"] if dims else None)
            self.sent[key] = self.sent.get(key, 0) + d["Value"]
        return self.inner.put_metric_data(Namespace=Namespace, MetricData=MetricData)


async def test_metrics_are_aggregated_and_published_to_cloudwatch():
    bus = EventBus()
    cw = boto3.client("cloudwatch", region_name=REGION)
    spy = SpyClient(cw)
    metrics = CloudWatchMetrics(bus, "AgentHarnessTest", interval=3600, client=spy)
    metrics.start()
    sent = ev(decision="allowed")
    for event in [
        ev(kind="run_started"),
        sent, sent.model_copy(update={"status_code": 200}),
        ev(decision="allowed"),
        ev(decision="blocked", status_code=403),
        ev(kind="run_stopped", reason="policy: rule 'block-paste-sites' requires termination"),
    ]:
        bus.publish(event)
    await asyncio.sleep(0.05)
    await metrics.stop()  # final flush

    # Exactly what CloudWatch received: each request once, the response update not again.
    assert spy.sent == {
        ("Requests", None): 3, ("Requests", "allowed"): 2, ("Requests", "blocked"): 1,
        ("RunsStarted", None): 1, ("RunsStopped", None): 1, ("PolicyTerminations", None): 1,
    }
    # And it was accepted by the CloudWatch API. (Only dimensioned queries here: moto's
    # dimensionless GetMetricStatistics sums every series, unlike real CloudWatch.)
    assert _sum(cw, "Requests", "allowed") == 2
    assert _sum(cw, "Requests", "blocked") == 1
    names = {m["MetricName"] for m in cw.list_metrics(Namespace="AgentHarnessTest")["Metrics"]}
    assert {"Requests", "RunsStarted", "RunsStopped", "PolicyTerminations"} <= names
    assert metrics.published == 6


async def test_failed_publish_keeps_counts_for_the_next_interval():
    class Failing:
        calls = 0

        def put_metric_data(self, **kwargs):
            Failing.calls += 1
            if Failing.calls == 1:
                raise RuntimeError("throttled")
            self.sent = kwargs

    client = Failing()
    bus = EventBus()
    metrics = CloudWatchMetrics(bus, "T", interval=3600, client=client)
    metrics.start()
    bus.publish(ev(decision="blocked", status_code=403))
    await asyncio.sleep(0.05)
    await metrics.flush()  # fails
    await metrics.flush()  # retried
    values = {(d["MetricName"], (d.get("Dimensions") or [{}])[0].get("Value")): d["Value"] for d in client.sent["MetricData"]}
    assert values[("Requests", "blocked")] == 1.0 and values[("Requests", None)] == 1.0
    await metrics.stop()


# --- backend wiring ---------------------------------------------------------------

POLICY = "default: deny\n"


async def test_backend_mirrors_runs_and_events_to_dynamodb_and_publishes_metrics(tmp_path):
    ensure_table(TABLE, region=REGION)
    policy = tmp_path / "p.yaml"
    policy.write_text(POLICY)
    backend = Backend(Settings(
        policy_path=policy, home=tmp_path / "home", proxy_port=0, api_port=0,
        dynamodb_table=TABLE, cloudwatch_namespace="AgentHarnessTest", aws_region=REGION, metrics_interval=3600,
    ))
    await backend.start()
    assert "DynamoDB" in backend.audit_description
    rec, _ = backend.runs.register(["agent"], "/")
    await asyncio.sleep(0.1)  # let the store writer see run_started
    backend.runs.finish(rec.id, 0)
    await backend.stop()  # flushes the writer, metrics, and the mirror

    remote = DynamoDBStore(TABLE, region=REGION)
    assert remote.get_run(rec.id).status == "finished"
    assert [e.kind for e in remote.list_events(rec.id)] == ["run_started", "run_finished"]
    cw = boto3.client("cloudwatch", region_name=REGION)
    names = {m["MetricName"] for m in cw.list_metrics(Namespace="AgentHarnessTest")["Metrics"]}
    assert {"RunsStarted", "RunsFinished"} <= names


async def test_backend_refuses_to_start_without_the_table(tmp_path):
    policy = tmp_path / "p.yaml"
    policy.write_text(POLICY)
    backend = Backend(Settings(policy_path=policy, home=tmp_path / "home", proxy_port=0, api_port=0,
                               dynamodb_table="missing-table", aws_region=REGION))
    with pytest.raises(AuditTableMissing):
        await backend.start()
    assert not (tmp_path / "home" / "operator_token").exists()
