"""Load demo customers and orders (data/seed.json) into the stage's DynamoDB tables.

Usage: python scripts/seed_data.py [--stage dev] [--reset-refunds]
"""

from __future__ import annotations

import argparse
import json

from _common import REPO_ROOT, client, load_outputs, log
from boto3.dynamodb.types import TypeSerializer

_serializer = TypeSerializer()


def _item(data: dict) -> dict:
    return {key: _serializer.serialize(value) for key, value in data.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    parser.add_argument(
        "--reset-refunds", action="store_true", help="Zero refunded_cents so demos can be re-run"
    )
    args = parser.parse_args()

    outputs = load_outputs(args.stage)
    if outputs["Stage"] == "prod":
        raise SystemExit("Refusing to seed demo data into prod")
    ddb = client("dynamodb", outputs)
    seed = json.loads((REPO_ROOT / "data" / "seed.json").read_text(encoding="utf-8"))

    for customer in seed["customers"]:
        ddb.put_item(TableName=outputs["CustomersTable"], Item=_item(customer))
    for order in seed["orders"]:
        if not args.reset_refunds:
            existing = ddb.get_item(
                TableName=outputs["OrdersTable"], Key=_item({"order_id": order["order_id"]})
            )
            if "Item" in existing:
                order = {
                    **order,
                    "refunded_cents": int(existing["Item"].get("refunded_cents", {"N": "0"})["N"]),
                }
        ddb.put_item(TableName=outputs["OrdersTable"], Item=_item(order))

    log.info("Seeded %d customers and %d orders", len(seed["customers"]), len(seed["orders"]))


if __name__ == "__main__":
    main()
