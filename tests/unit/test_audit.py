"""Unit tests for the audit hash-chain (#17). Uses moto to mock DynamoDB."""

import importlib
import os
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

TABLE = "wildfire-watch-audit-test"


@pytest.fixture
def audit():
    os.environ["WW_AUDIT_TABLE"] = TABLE
    os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "prediction_id", "KeyType": "HASH"},
                {"AttributeName": "written_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "prediction_id", "AttributeType": "S"},
                {"AttributeName": "written_at", "AttributeType": "S"},
                {"AttributeName": "fire_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "fire_id-written_at-index",
                "KeySchema": [
                    {"AttributeName": "fire_id", "KeyType": "HASH"},
                    {"AttributeName": "written_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()

        # import after the moto context + env is set so the module-level
        # boto3 resource binds to the mocked region
        from functions.alert import audit as audit_module
        importlib.reload(audit_module)
        yield audit_module


def _rec(confidence=0.9):
    return (
        {"action": "DISPATCH", "confidence": confidence, "stations": ["s1"]},
        {"brief": "advisory brief", "sms": "evacuate now"},
    )


def test_first_record_uses_genesis_hash(audit):
    pid = audit.log_prediction("fire-1", *_rec())
    table = boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE)
    item = table.scan()["Items"][0]
    assert item["prediction_id"] == pid
    assert item["prev_hash"] == audit.GENESIS_HASH
    assert len(item["record_hash"]) == 64


def test_chain_links_across_events(audit):
    pid = audit.log_prediction("fire-2", *_rec())
    audit.append_guardrail_outcome("fire-2", pid, passed=True, reason=None)
    audit.mark_alert_sent("fire-2", pid, alert_id="alert-xyz")
    assert audit.verify_chain("fire-2") is True


def test_separate_fires_have_independent_chains(audit):
    audit.log_prediction("fire-A", *_rec())
    audit.log_prediction("fire-B", *_rec())
    assert audit.verify_chain("fire-A") is True
    assert audit.verify_chain("fire-B") is True


def test_verify_chain_detects_field_tampering(audit):
    pid = audit.log_prediction("fire-3", *_rec(confidence=0.9))
    audit.append_guardrail_outcome("fire-3", pid, passed=True, reason=None)

    # Tamper: rewrite the confidence on the original prediction row.
    table = boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE)
    items = table.scan()["Items"]
    target = next(i for i in items if i["prediction_id"] == pid)
    table.update_item(
        Key={"prediction_id": target["prediction_id"], "written_at": target["written_at"]},
        UpdateExpression="SET confidence = :c",
        ExpressionAttributeValues={":c": Decimal("0.1")},
    )

    assert audit.verify_chain("fire-3") is False


def test_verify_chain_detects_broken_prev_hash(audit):
    pid = audit.log_prediction("fire-4", *_rec())
    second = audit.append_guardrail_outcome("fire-4", pid, passed=True, reason=None)

    table = boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE)
    items = table.scan()["Items"]
    target = next(i for i in items if i["prediction_id"] == second)
    table.update_item(
        Key={"prediction_id": target["prediction_id"], "written_at": target["written_at"]},
        UpdateExpression="SET prev_hash = :p",
        ExpressionAttributeValues={":p": "f" * 64},
    )

    assert audit.verify_chain("fire-4") is False


def test_empty_fire_chain_is_valid(audit):
    assert audit.verify_chain("nonexistent-fire") is True
