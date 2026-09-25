"""Idempotency store for money-moving operations.

State machine of one record (partition key ``<customer_id>#<idempotency_key>``)::

    (absent) --claim--> IN_PROGRESS --commit--> COMPLETED   (terminal; replays return the stored result)
                            |
                            +--release (pre-payment failure)--> (absent)   (a retry may run again)
                            +--lock expires (crashed worker) --> may be re-claimed

* A retry of a COMPLETED operation with the same payload returns the stored result
  (``idempotent_replay: true``) — no second refund is created.
* The same key with a *different* payload is rejected (IDEMPOTENCY_KEY_REUSED).
* A concurrent retry while the first attempt is still running gets OPERATION_IN_PROGRESS
  (retryable), so the caller backs off instead of double-executing.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from .errors import IdempotencyKeyReused, OperationInProgress
from .repository import error_code, from_item, raise_if_throttled, to_item

IN_PROGRESS = "IN_PROGRESS"
COMPLETED = "COMPLETED"


def request_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Claim:
    pk: str
    replayed_result: dict[str, Any] | None = None

    @property
    def is_replay(self) -> bool:
        return self.replayed_result is not None


class IdempotencyStore:
    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        lock_seconds: int,
        ttl_days: int,
        clock: Any = time.time,
    ) -> None:
        self._ddb = client
        self._table = table_name
        self._lock_seconds = lock_seconds
        self._ttl_seconds = ttl_days * 86_400
        self._clock = clock

    @staticmethod
    def scoped_key(customer_id: str, idempotency_key: str) -> str:
        # Scoping by customer prevents one customer's key from colliding with another's.
        return f"{customer_id}#{idempotency_key}"

    def claim(self, pk: str, fingerprint: str) -> Claim:
        now = int(self._clock())
        try:
            self._ddb.put_item(
                TableName=self._table,
                Item=to_item(
                    {
                        "idempotency_key": pk,
                        "status": IN_PROGRESS,
                        "fingerprint": fingerprint,
                        "lock_expires_at": now + self._lock_seconds,
                        "expires_at": now + self._ttl_seconds,
                    }
                ),
                ConditionExpression=(
                    "attribute_not_exists(idempotency_key) OR (#status = :in_progress AND lock_expires_at < :now)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=to_item({":in_progress": IN_PROGRESS, ":now": now}),
            )
            return Claim(pk=pk)
        except ClientError as error:
            if error_code(error) != "ConditionalCheckFailedException":
                raise_if_throttled(error)
                raise
        return self._resolve_existing(pk, fingerprint)

    def release(self, pk: str) -> None:
        """Drop an IN_PROGRESS lock so that a retry can execute the operation again."""
        try:
            self._ddb.delete_item(
                TableName=self._table,
                Key=to_item({"idempotency_key": pk}),
                ConditionExpression="#status = :in_progress",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=to_item({":in_progress": IN_PROGRESS}),
            )
        except ClientError as error:
            if error_code(error) != "ConditionalCheckFailedException":
                raise

    def _resolve_existing(self, pk: str, fingerprint: str) -> Claim:
        response = self._ddb.get_item(
            TableName=self._table, Key=to_item({"idempotency_key": pk}), ConsistentRead=True
        )
        item = response.get("Item")
        if not item:
            # Lost a race with a release(); let the caller retry with backoff.
            raise OperationInProgress("Operation state changed concurrently, retry")
        record = from_item(item)
        if record["fingerprint"] != fingerprint:
            raise IdempotencyKeyReused(
                "This idempotency key was already used for a different refund request",
                details={"idempotency_key": pk.split("#", 1)[-1]},
            )
        if record["status"] == COMPLETED:
            return Claim(pk=pk, replayed_result=json.loads(record["result"]))
        raise OperationInProgress("A refund with this idempotency key is already being processed")
