"""Unit tests for the alert sender Lambda (#22)."""

import importlib
import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import boto3
import pytest
from moto import mock_aws

RESIDENTS_TABLE = "wildfire-watch-residents-test"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("WW_DYNAMODB_RESIDENTS_TABLE", RESIDENTS_TABLE)
    monkeypatch.setenv("WW_SNS_ALERT_TOPIC_ARN", "arn:aws:sns:us-west-2:123456789012:wildfire-watch-alerts")
    monkeypatch.setenv("WW_AUDIT_TABLE", "wildfire-watch-audit-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("WW_DRY_RUN", "true")


@pytest.fixture
def residents_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        table = ddb.create_table(
            TableName=RESIDENTS_TABLE,
            KeySchema=[{"AttributeName": "resident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "resident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table


def _seed_resident(table, resident_id, lat, lon, phone="+15550001234"):
    table.put_item(Item={
        "resident_id": resident_id,
        "phone": phone,
        "lat": Decimal(str(lat)),
        "lon": Decimal(str(lon)),
        "alert_radius_km": Decimal("10"),
    })


def _load_sender():
    import functions.alert.sender as m
    importlib.reload(m)
    return m


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def test_haversine_same_point():
    m = _load_sender()
    assert m._haversine_km(34.0, -118.0, 34.0, -118.0) == 0.0


def test_haversine_known_distance():
    m = _load_sender()
    # LA to San Diego ≈ 179 km
    dist = m._haversine_km(34.0522, -118.2437, 32.7157, -117.1611)
    assert 170 < dist < 190


def test_bounding_box_symmetry():
    m = _load_sender()
    min_lat, max_lat, min_lon, max_lon = m._bounding_box(34.0, -118.0, 10.0)
    assert min_lat < 34.0 < max_lat
    assert min_lon < -118.0 < max_lon


# ---------------------------------------------------------------------------
# Radius query
# ---------------------------------------------------------------------------

def test_get_residents_in_radius_includes_nearby(residents_table):
    with mock_aws():
        _seed_resident(residents_table, "r1", lat=34.05, lon=-118.25)  # ~5 km from centre
        m = _load_sender()
        results = m.get_residents_in_radius(34.0522, -118.2437, radius_km=10.0)
    assert any(r["resident_id"] == "r1" for r in results)


def test_get_residents_in_radius_excludes_distant(residents_table):
    with mock_aws():
        _seed_resident(residents_table, "far", lat=37.77, lon=-122.41)  # SF, ~560 km
        m = _load_sender()
        results = m.get_residents_in_radius(34.0522, -118.2437, radius_km=10.0)
    assert not any(r["resident_id"] == "far" for r in results)


def test_get_residents_empty_table(residents_table):
    with mock_aws():
        m = _load_sender()
        results = m.get_residents_in_radius(34.0, -118.0, radius_km=10.0)
    assert results == []


# ---------------------------------------------------------------------------
# send_alerts — dry run
# ---------------------------------------------------------------------------

def test_send_alerts_dry_run_returns_count(residents_table):
    with mock_aws():
        _seed_resident(residents_table, "r1", lat=34.05, lon=-118.25)
        _seed_resident(residents_table, "r2", lat=34.06, lon=-118.26)
        m = _load_sender()

        fire = {"fire_id": "fire-001", "lat": 34.0522, "lon": -118.2437, "risk_radius_km": 10.0}
        advisory = {"sms": "Evacuate now.", "brief": "Fire nearby."}
        result = m.send_alerts(fire, advisory, prediction_id="pred-001")

    assert result["residents_alerted"] == 2
    assert result["dry_run"] is True
    assert result["fire_id"] == "fire-001"


def test_send_alerts_dry_run_skips_sns(residents_table):
    with mock_aws():
        _seed_resident(residents_table, "r1", lat=34.05, lon=-118.25)
        m = _load_sender()

        with patch.object(m, "_sns") as mock_sns_fn:
            fire = {"fire_id": "fire-002", "lat": 34.0522, "lon": -118.2437, "risk_radius_km": 10.0}
            m.send_alerts(fire, {"sms": "Alert.", "brief": ""}, "pred-002")
            mock_sns_fn.assert_not_called()


def test_send_alerts_no_residents_returns_zero(residents_table):
    with mock_aws():
        m = _load_sender()
        fire = {"fire_id": "fire-003", "lat": 34.0522, "lon": -118.2437, "risk_radius_km": 10.0}
        result = m.send_alerts(fire, {"sms": "Alert.", "brief": ""}, "pred-003")
    assert result["residents_alerted"] == 0


# ---------------------------------------------------------------------------
# send_alerts — live SNS (mock)
# ---------------------------------------------------------------------------

def test_send_alerts_publishes_sms_per_resident(residents_table, monkeypatch):
    monkeypatch.setenv("WW_DRY_RUN", "false")
    with mock_aws():
        _seed_resident(residents_table, "r1", lat=34.05, lon=-118.25, phone="+15550001111")
        _seed_resident(residents_table, "r2", lat=34.06, lon=-118.26, phone="+15550002222")
        m = _load_sender()

        mock_client = MagicMock()
        mock_client.publish.return_value = {"MessageId": "msg-1"}
        with patch.object(m, "_sns", return_value=mock_client), \
             patch.object(m, "mark_alert_sent"):
            fire = {"fire_id": "fire-004", "lat": 34.0522, "lon": -118.2437, "risk_radius_km": 10.0}
            result = m.send_alerts(fire, {"sms": "Evacuate.", "brief": ""}, "pred-004")

    # 2 per-resident SMS + 1 broadcast topic publish = 3 calls
    assert mock_client.publish.call_count == 3
    assert result["residents_alerted"] == 2


def test_send_alerts_calls_mark_alert_sent(residents_table, monkeypatch):
    monkeypatch.setenv("WW_DRY_RUN", "false")
    with mock_aws():
        m = _load_sender()
        mock_client = MagicMock()
        mock_client.publish.return_value = {"MessageId": "msg-1"}
        with patch.object(m, "_sns", return_value=mock_client), \
             patch.object(m, "mark_alert_sent") as mock_mark:
            fire = {"fire_id": "fire-005", "lat": 34.0522, "lon": -118.2437, "risk_radius_km": 10.0}
            m.send_alerts(fire, {"sms": "Alert.", "brief": ""}, "pred-005")
        mock_mark.assert_called_once_with("fire-005", "pred-005", alert_id="fire-005-alert")


def test_send_alerts_sms_failure_does_not_halt(residents_table, monkeypatch):
    """A single SNS failure is logged and counted but doesn't abort the batch."""
    from botocore.exceptions import ClientError
    monkeypatch.setenv("WW_DRY_RUN", "false")
    with mock_aws():
        _seed_resident(residents_table, "r1", lat=34.05, lon=-118.25, phone="+15550001111")
        _seed_resident(residents_table, "r2", lat=34.06, lon=-118.26, phone="+15550002222")
        m = _load_sender()

        def _raise_on_first(**kwargs):
            if kwargs.get("PhoneNumber") == "+15550001111":
                raise ClientError({"Error": {"Code": "InvalidParameter", "Message": "bad"}}, "Publish")
            return {"MessageId": "ok"}

        mock_client = MagicMock()
        mock_client.publish.side_effect = _raise_on_first
        with patch.object(m, "_sns", return_value=mock_client), \
             patch.object(m, "mark_alert_sent"):
            fire = {"fire_id": "fire-006", "lat": 34.0522, "lon": -118.2437, "risk_radius_km": 10.0}
            result = m.send_alerts(fire, {"sms": "Alert.", "brief": ""}, "pred-006")

    assert result["residents_alerted"] == 1
    assert result["residents_failed"] == 1


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def test_handler_invokes_send_alerts(residents_table):
    with mock_aws():
        m = _load_sender()
        event = {
            "fire_event": {"fire_id": "fire-007", "lat": 34.0, "lon": -118.0, "risk_radius_km": 10.0},
            "advisory": {"sms": "Alert.", "brief": ""},
            "prediction_id": "pred-007",
            "action": "APPROVED",
        }
        with patch.object(m, "send_alerts", return_value={"fire_id": "fire-007", "residents_alerted": 0, "residents_failed": 0, "dry_run": True, "alert_id": "x"}) as mock_send:
            m.handler(event)
        mock_send.assert_called_once_with(
            event["fire_event"], event["advisory"], event["prediction_id"]
        )
