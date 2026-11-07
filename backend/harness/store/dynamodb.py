"""DynamoDB audit store.

Single-table design:

    pk = "RUN#<run_id>"
      sk = "META"                    -> the run record
      sk = "EVT#<ts>#<event_id>"     -> one event; ts is fixed width, so sk order == time order
    GSI "runs-by-start": gsi1pk = "RUNS", gsi1sk = "<started_at>#<run_id>"  (run records only)

Events for a run in a time range are a single Query. Events and runs are upserts, so the
gateway's response update for a request overwrites the same item.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from ..events import Event, format_ts
from .base import AuditStore, RunRecord

RUNS_INDEX = "runs-by-start"
_AFTER_ANY_TS = "~"  # sorts after every character that can appear in a timestamp or id
_PAGE = 1000


class AuditTableMissing(RuntimeError):
    pass


def table_definition(name: str) -> dict[str, Any]:
    return {
        "TableName": name,
        "BillingMode": "PAY_PER_REQUEST",  # no capacity planning; pennies at demo scale
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi1pk", "AttributeType": "S"},
            {"AttributeName": "gsi1sk", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": RUNS_INDEX,
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    }


def ensure_table(name: str, region: str | None = None, session: boto3.Session | None = None) -> bool:
    """Create the audit table (and enable TTL on `expires_at`) if needed. Returns True if created."""
    client = (session or boto3.Session()).client("dynamodb", region_name=region)
    created = False
    try:
        client.describe_table(TableName=name)
    except client.exceptions.ResourceNotFoundException:
        client.create_table(**table_definition(name))
        client.get_waiter("table_exists").wait(TableName=name)
        created = True
    try:
        client.update_time_to_live(
            TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"}
        )
    except ClientError as e:
        if "already enabled" not in str(e).lower():
            raise
    return created


class DynamoDBStore(AuditStore):
    def __init__(
        self,
        table_name: str,
        region: str | None = None,
        retention_days: int | None = None,
        session: boto3.Session | None = None,
    ) -> None:
        session = session or boto3.Session()
        self.table_name = table_name
        self._table = session.resource("dynamodb", region_name=region).Table(table_name)
        self._ttl_seconds = retention_days * 86400 if retention_days else None

    def check(self) -> None:
        """Fail fast with an actionable message if the table is missing or unreachable."""
        try:
            self._table.load()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                raise AuditTableMissing(
                    f"DynamoDB table '{self.table_name}' does not exist. "
                    f"Create it with: harness aws-setup --table {self.table_name}"
                ) from None
            raise

    def _with_expiry(self, item: dict[str, Any]) -> dict[str, Any]:
        if self._ttl_seconds:
            item["expires_at"] = int(time.time()) + self._ttl_seconds
        return item

    # --- events -------------------------------------------------------------------

    def put_event(self, event: Event) -> None:
        item: dict[str, Any] = {
            "pk": f"RUN#{event.run_id}",
            "sk": f"EVT#{format_ts(event.ts)}#{event.id}",
            "kind": event.kind,
            "data": event.model_dump_json(),
        }
        # Top-level copies make the table readable in the AWS console.
        for attr in ("decision", "host", "method", "rule_id", "status_code"):
            value = getattr(event, attr)
            if value is not None:
                item[attr] = value
        self._table.put_item(Item=self._with_expiry(item))

    def list_events(
        self,
        run_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        low = "EVT#" + (format_ts(since) if since else "")
        high = "EVT#" + (format_ts(until) if until else "") + _AFTER_ANY_TS
        condition = Key("pk").eq(f"RUN#{run_id}") & Key("sk").between(low, high)
        events: list[Event] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": condition}
        while len(events) < limit:
            kwargs["Limit"] = min(_PAGE, limit - len(events))
            page = self._table.query(**kwargs)
            events.extend(Event.model_validate_json(i["data"]) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return events

    # --- runs ---------------------------------------------------------------------

    def put_run(self, run: RunRecord) -> None:
        self._table.put_item(
            Item=self._with_expiry(
                {
                    "pk": f"RUN#{run.id}",
                    "sk": "META",
                    "gsi1pk": "RUNS",
                    "gsi1sk": f"{format_ts(run.started_at)}#{run.id}",
                    "status": run.status,
                    "command": " ".join(run.command),
                    "data": run.model_dump_json(),
                }
            )
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        item = self._table.get_item(Key={"pk": f"RUN#{run_id}", "sk": "META"}).get("Item")
        return RunRecord.model_validate_json(item["data"]) if item else None

    def list_runs(self, limit: int = 100) -> list[RunRecord]:
        page = self._table.query(
            IndexName=RUNS_INDEX,
            KeyConditionExpression=Key("gsi1pk").eq("RUNS"),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        return [RunRecord.model_validate_json(i["data"]) for i in page.get("Items", [])]
