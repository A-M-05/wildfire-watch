"""Unit tests for the resident registration handler (#23)."""

import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "residents-test"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("WW_DYNAMODB_RESIDENTS_TABLE", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    yield


@pytest.fixture
def residents_table(env):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "resident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "resident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE_NAME)


@pytest.fixture
def register(env):
    """Reload the module under each test so the cached dynamodb resource
    binds to the moto-mocked endpoint, not a real one from a prior test."""
    import importlib

    from functions.alert import register as module

    importlib.reload(module)
    return module


def _event(body: dict | str | None, sub: str | None = "user-abc-123") -> dict:
    return {
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
        "requestContext": {
            "authorizer": {"claims": {"sub": sub}} if sub else {}
        },
    }


def test_register_with_lat_lon_writes_row(residents_table, register):
    event = _event({"phone": "+15555550100", "lat": 37.7749, "lon": -122.4194})
    resp = register.handler(event)
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"]) == {"resident_id": "user-abc-123"}

    item = residents_table.get_item(Key={"resident_id": "user-abc-123"})["Item"]
    assert item["phone"] == "+15555550100"
    assert item["lat"] == Decimal("37.7749")
    assert item["lon"] == Decimal("-122.4194")
    assert item["alert_radius_km"] == Decimal("10")  # default
    assert "registered_at" in item


def test_register_with_address_geocodes_via_location(residents_table, register, monkeypatch):
    monkeypatch.setenv("WW_LOCATION_PLACE_INDEX", "test-index")
    fake_location = MagicMock()
    fake_location.search_place_index_for_text.return_value = {
        "Results": [
            {"Place": {"Geometry": {"Point": [-122.4194, 37.7749]}}}  # [lon, lat]
        ]
    }
    with patch.object(register, "_location", return_value=fake_location):
        event = _event({"phone": "+15555550100", "address": "1 Market St, SF, CA"})
        resp = register.handler(event)

    assert resp["statusCode"] == 201
    item = residents_table.get_item(Key={"resident_id": "user-abc-123"})["Item"]
    assert item["lat"] == Decimal("37.7749")
    assert item["lon"] == Decimal("-122.4194")
    fake_location.search_place_index_for_text.assert_called_once_with(
        IndexName="test-index", Text="1 Market St, SF, CA", MaxResults=1
    )


def test_register_address_without_place_index_env_returns_400(residents_table, register):
    # WW_LOCATION_PLACE_INDEX intentionally unset
    event = _event({"phone": "+15555550100", "address": "1 Market St"})
    resp = register.handler(event)
    assert resp["statusCode"] == 400
    assert "geocoding unavailable" in json.loads(resp["body"])["error"]


def test_register_address_no_match_returns_400(residents_table, register, monkeypatch):
    monkeypatch.setenv("WW_LOCATION_PLACE_INDEX", "test-index")
    fake_location = MagicMock()
    fake_location.search_place_index_for_text.return_value = {"Results": []}
    with patch.object(register, "_location", return_value=fake_location):
        event = _event({"phone": "+15555550100", "address": "nowhere at all"})
        resp = register.handler(event)
    assert resp["statusCode"] == 400
    assert "no geocoding match" in json.loads(resp["body"])["error"]


def test_register_rejects_missing_auth(residents_table, register):
    event = _event({"phone": "+15555550100", "lat": 37.7, "lon": -122.4}, sub=None)
    resp = register.handler(event)
    assert resp["statusCode"] == 401


def test_register_rejects_invalid_phone(residents_table, register):
    for bad in ["5555550100", "+", "+1", "not-a-phone", "", None]:
        event = _event({"phone": bad, "lat": 37.7, "lon": -122.4})
        resp = register.handler(event)
        assert resp["statusCode"] == 400, f"should reject phone={bad!r}"
        assert "E.164" in json.loads(resp["body"])["error"]


def test_register_rejects_missing_location(residents_table, register):
    event = _event({"phone": "+15555550100"})  # no address, no lat/lon
    resp = register.handler(event)
    assert resp["statusCode"] == 400
    assert "address" in json.loads(resp["body"])["error"]


def test_register_rejects_out_of_range_lat(residents_table, register):
    event = _event({"phone": "+15555550100", "lat": 91, "lon": 0})
    resp = register.handler(event)
    assert resp["statusCode"] == 400
    assert "lat out of range" in json.loads(resp["body"])["error"]


def test_register_rejects_out_of_range_lon(residents_table, register):
    event = _event({"phone": "+15555550100", "lat": 0, "lon": -181})
    resp = register.handler(event)
    assert resp["statusCode"] == 400
    assert "lon out of range" in json.loads(resp["body"])["error"]


def test_register_rejects_invalid_json(residents_table, register):
    event = _event("{not-json")
    resp = register.handler(event)
    assert resp["statusCode"] == 400
    assert "invalid JSON" in json.loads(resp["body"])["error"]


def test_register_rejects_negative_radius(residents_table, register):
    event = _event({"phone": "+15555550100", "lat": 37.7, "lon": -122.4, "alert_radius_km": -1})
    resp = register.handler(event)
    assert resp["statusCode"] == 400


def test_register_rejects_oversized_radius(residents_table, register):
    event = _event({"phone": "+15555550100", "lat": 37.7, "lon": -122.4, "alert_radius_km": 1000})
    resp = register.handler(event)
    assert resp["statusCode"] == 400


def test_register_accepts_custom_radius(residents_table, register):
    event = _event({"phone": "+15555550100", "lat": 37.7, "lon": -122.4, "alert_radius_km": 25})
    resp = register.handler(event)
    assert resp["statusCode"] == 201
    item = residents_table.get_item(Key={"resident_id": "user-abc-123"})["Item"]
    assert item["alert_radius_km"] == Decimal("25")


def test_register_does_not_log_phone(residents_table, register, capsys):
    # PII rule: phone must never appear in logs.
    phone = "+15555550199"
    event = _event({"phone": phone, "lat": 37.7, "lon": -122.4})
    resp = register.handler(event)
    assert resp["statusCode"] == 201
    captured = capsys.readouterr()
    assert phone not in captured.out
    assert phone not in captured.err
    # But the opaque resident_id IS expected in logs (for audit/debug).
    assert "user-abc-123" in captured.out


def test_register_overwrites_existing_resident(residents_table, register):
    # Re-registration should update, not duplicate. resident_id is the PK.
    event1 = _event({"phone": "+15555550100", "lat": 37.7, "lon": -122.4})
    event2 = _event({"phone": "+15555550200", "lat": 40.0, "lon": -120.0})
    register.handler(event1)
    register.handler(event2)
    item = residents_table.get_item(Key={"resident_id": "user-abc-123"})["Item"]
    assert item["phone"] == "+15555550200"
    assert item["lat"] == Decimal("40.0")
