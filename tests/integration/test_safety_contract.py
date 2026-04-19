"""Safety contract integration tests (#32).

The critical test in the repo. Verifies the hard rule from CLAUDE.md:
the audit row is committed to DynamoDB BEFORE any SMS is published.

Unlike the unit tests, these exercise the real safety_gate -> sender wiring
against moto-backed DynamoDB so the audit writes hit actual storage and the
timestamps are comparable end-to-end. Bedrock is stubbed because (a) it
costs money and (b) guardrails.py already has dedicated unit coverage —
what this file proves is the *ordering* and *chain* guarantees across the
two Lambdas that together form the pre-dispatch safety boundary.

Covers the four contracts listed in Issue #32:

  1. audit_write_timestamp < sns_send_timestamp  (the hard rule)
  2. Guardrails blocks low-confidence certainty -> no SMS ever fires
  3. Low confidence with clean advisory -> HUMAN_REVIEW_REQUIRED, no SMS
  4. Hash chain unbroken across prediction -> outcome -> alert_sent
"""

import importlib
import os
import sys
import time
import types
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

AUDIT_TABLE = "wildfire-watch-audit-test"
RESIDENTS_TABLE = "wildfire-watch-residents-test"
REGION = "us-west-2"


# ---------------------------------------------------------------------------
# Fixtures — real DynamoDB (mocked by moto), stubbed Bedrock
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("WW_AUDIT_TABLE", AUDIT_TABLE)
    monkeypatch.setenv("WW_DYNAMODB_RESIDENTS_TABLE", RESIDENTS_TABLE)
    monkeypatch.setenv(
        "WW_SNS_ALERT_TOPIC_ARN",
        "arn:aws:sns:us-west-2:123456789012:wildfire-watch-alerts",
    )
    monkeypatch.setenv("WW_CONFIDENCE_THRESHOLD", "0.65")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    # DRY_RUN=false so the SNS publish path is exercised; we still mock the
    # client. The contract test must observe real publish calls — dry-run
    # mode short-circuits them and would hide an ordering bug.
    monkeypatch.setenv("WW_DRY_RUN", "false")


@pytest.fixture(autouse=True)
def stub_advisory_prompt():
    # advisory_prompt imports boto3's bedrock-runtime at module load. We
    # never want a real client in tests, so plant a stub before safety_gate
    # imports it. Individual tests patch generate_advisory on safety_gate
    # itself to control per-test return values.
    stub = types.ModuleType("ml.bedrock.advisory_prompt")
    stub.generate_advisory = MagicMock()
    sys.modules["ml.bedrock.advisory_prompt"] = stub
    yield stub
    sys.modules.pop("ml.bedrock.advisory_prompt", None)


@pytest.fixture
def aws_tables():
    """Create audit + residents tables in moto, yield (audit, residents)."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        audit = ddb.create_table(
            TableName=AUDIT_TABLE,
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
        )
        audit.wait_until_exists()

        residents = ddb.create_table(
            TableName=RESIDENTS_TABLE,
            KeySchema=[{"AttributeName": "resident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "resident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        residents.wait_until_exists()

        yield audit, residents


@pytest.fixture
def pipeline(aws_tables, stub_advisory_prompt):
    """Reload safety_gate, audit, and sender after env + table setup.

    Reload order matters: audit first (used by both), then safety_gate and
    sender which import log_prediction/mark_alert_sent at module load.
    """
    from functions.alert import audit, safety_gate, sender
    importlib.reload(audit)
    importlib.reload(safety_gate)
    importlib.reload(sender)
    return types.SimpleNamespace(
        audit=audit,
        safety_gate=safety_gate,
        sender=sender,
        audit_table=aws_tables[0],
        residents_table=aws_tables[1],
    )


# ---------------------------------------------------------------------------
# Event fixtures
# ---------------------------------------------------------------------------


def _seed_resident(table, resident_id, lat, lon, phone="+15550001234"):
    table.put_item(Item={
        "resident_id": resident_id,
        "phone": phone,
        "lat": Decimal(str(lat)),
        "lon": Decimal(str(lon)),
    })


def _fire_event(fire_id: str = "fire-contract-001") -> dict:
    return {
        "fire_id": fire_id,
        "source": "CALFIRE",
        "lat": 34.0522,
        "lon": -118.2437,
        "detected_at": "2026-04-18T03:14:15Z",
        "confidence": 0.92,
        "risk_radius_km": 10.0,
    }


def _gate_event(confidence: float = 0.9, fire_id: str = "fire-contract-001") -> dict:
    return {
        "fire_event": _fire_event(fire_id),
        "recommendation": {
            "recommendation": "dispatch 2 engines + 1 aerial",
            "confidence": confidence,
        },
    }


def _clean_advisory() -> dict:
    return {
        "sms": "Wildfire 2mi north. Evacuate via Hwy 101 now.",
        "brief": "Active perimeter near residential zone; dispatch approved.",
    }


# ---------------------------------------------------------------------------
# Contract 1 — audit row committed before SMS publish
# ---------------------------------------------------------------------------


def test_audit_row_committed_before_sns_publish(pipeline, monkeypatch):
    """The hard rule: timestamp of the prediction audit row must precede the
    first SNS publish. Proven by instrumenting both sides with time.monotonic
    and inspecting the actual DynamoDB row after."""
    sms_publish_times: list[float] = []
    audit_put_times: list[tuple[float, str]] = []  # (monotonic, event)

    # Instrument audit puts — hook the underlying put so we capture the real
    # commit order, including ordering *between* prediction/outcome/alert_sent
    # rows. Wrapping at _put_record means we time the same call the sender and
    # safety gate rely on for durability.
    real_put = pipeline.audit._put_record

    def timed_put(record):
        result = real_put(record)
        audit_put_times.append((time.monotonic(), record.get("event", "?")))
        return result

    monkeypatch.setattr(pipeline.audit, "_put_record", timed_put)
    monkeypatch.setattr(pipeline.safety_gate, "log_prediction", pipeline.audit.log_prediction)
    monkeypatch.setattr(
        pipeline.safety_gate,
        "append_guardrail_outcome",
        pipeline.audit.append_guardrail_outcome,
    )
    monkeypatch.setattr(pipeline.sender, "mark_alert_sent", pipeline.audit.mark_alert_sent)

    # Stub advisory generation (no real Bedrock call).
    monkeypatch.setattr(
        pipeline.safety_gate, "generate_advisory", lambda *_, **__: _clean_advisory()
    )
    # Stub guardrails validate as passing so the alert path actually fires.
    monkeypatch.setattr(
        pipeline.safety_gate,
        "validate_advisory",
        lambda text, confidence=None: {"passed": True, "blocked_reason": None},
    )

    # Mock SNS: every publish records a monotonic timestamp before returning.
    def timed_publish(**kwargs):
        sms_publish_times.append(time.monotonic())
        return {"MessageId": f"msg-{len(sms_publish_times)}"}

    mock_sns = MagicMock()
    mock_sns.publish.side_effect = timed_publish

    # Seed a resident inside the 10km risk radius so the sender has someone
    # to alert — without this, send_alerts returns early and no publish
    # happens.
    _seed_resident(pipeline.residents_table, "r1", lat=34.06, lon=-118.25)

    with mock_aws(), patch.object(pipeline.sender, "_sns", return_value=mock_sns):
        # Safety gate first — as Step Functions would invoke it.
        gate_result = pipeline.safety_gate.handler(_gate_event(confidence=0.9))
        assert gate_result["action"] == "APPROVED"

        # Then the alert sender — Step Functions forwards the approved payload.
        sender_result = pipeline.sender.handler({
            "fire_event": _fire_event(),
            "advisory": gate_result["advisory"],
            "prediction_id": gate_result["prediction_id"],
            "action": "APPROVED",
        })
        assert sender_result["residents_alerted"] >= 1

    # The contract assertions.
    assert audit_put_times, "audit was never written"
    assert sms_publish_times, "no SMS was published"

    prediction_time = next(t for t, e in audit_put_times if e == "prediction")
    first_publish = min(sms_publish_times)
    assert prediction_time < first_publish, (
        f"prediction audit row ({prediction_time}) must precede first SMS "
        f"publish ({first_publish})"
    )

    # And the row really exists in DynamoDB — not just a side effect of the mock.
    scan = pipeline.audit_table.scan()["Items"]
    prediction_rows = [r for r in scan if r["event"] == "prediction"]
    assert len(prediction_rows) == 1
    assert prediction_rows[0]["fire_id"] == "fire-contract-001"


# ---------------------------------------------------------------------------
# Contract 2 — guardrails block prevents any SMS
# ---------------------------------------------------------------------------


def test_guardrails_block_prevents_sms(pipeline, monkeypatch):
    """Uses the real in-process confidence-consistency check in guardrails.py
    — a low-confidence advisory that claims certainty ('you are safe') must
    be blocked WITHOUT calling Bedrock. Proves the real guardrails wiring
    rejects unsafe advisories and the sender is never invoked."""
    _seed_resident(pipeline.residents_table, "r1", lat=34.06, lon=-118.25)

    monkeypatch.setattr(
        pipeline.safety_gate,
        "generate_advisory",
        lambda *_, **__: {"sms": "You are safe. Carry on.", "brief": "(low confidence)"},
    )
    # Real validate_advisory — the certainty-phrase check runs before any
    # Bedrock call, so no AWS client is invoked. That's exactly what makes
    # this a meaningful contract test: the real guardrails code fires.

    mock_sns = MagicMock()
    with mock_aws(), patch.object(pipeline.sender, "_sns", return_value=mock_sns):
        gate_result = pipeline.safety_gate.handler(_gate_event(confidence=0.3))

    assert gate_result["action"] == "BLOCKED"
    assert "confidence 0.30" in gate_result["blocked_reason"]
    assert "advisory" not in gate_result  # don't leak the blocked text downstream
    mock_sns.publish.assert_not_called()

    # Audit evidence: both the prediction row AND a guardrails_outcome=False
    # row must exist — the chain captures blocked attempts as forensic data.
    rows = pipeline.audit_table.scan()["Items"]
    events = sorted(r["event"] for r in rows)
    assert events == ["guardrails_outcome", "prediction"]
    outcome = next(r for r in rows if r["event"] == "guardrails_outcome")
    assert outcome["guardrails_passed"] is False


# ---------------------------------------------------------------------------
# Contract 3 — low-confidence routes to human review, SMS does not fire
# ---------------------------------------------------------------------------


def test_low_confidence_routes_to_human_review(pipeline, monkeypatch):
    """Clean advisory + confidence < threshold -> HUMAN_REVIEW_REQUIRED.
    Step Functions is responsible for routing that to the dispatcher
    notifier; the sender Lambda must NOT run on this path."""
    _seed_resident(pipeline.residents_table, "r1", lat=34.06, lon=-118.25)

    monkeypatch.setattr(
        pipeline.safety_gate, "generate_advisory", lambda *_, **__: _clean_advisory()
    )
    monkeypatch.setattr(
        pipeline.safety_gate,
        "validate_advisory",
        lambda text, confidence=None: {"passed": True, "blocked_reason": None},
    )

    mock_sns = MagicMock()
    with mock_aws(), patch.object(pipeline.sender, "_sns", return_value=mock_sns):
        gate_result = pipeline.safety_gate.handler(_gate_event(confidence=0.4))

        # Simulate the Step Functions routing: on HUMAN_REVIEW_REQUIRED we
        # do NOT invoke the sender. This test codifies that contract.
        if gate_result["action"] == "APPROVED":
            pipeline.sender.handler({
                "fire_event": _fire_event(),
                "advisory": gate_result["advisory"],
                "prediction_id": gate_result["prediction_id"],
                "action": "APPROVED",
            })

    assert gate_result["action"] == "HUMAN_REVIEW_REQUIRED"
    mock_sns.publish.assert_not_called()

    rows = pipeline.audit_table.scan()["Items"]
    events = sorted(r["event"] for r in rows)
    # No alert_sent row — the chain tells a truthful story: prediction was
    # made, guardrails passed, but nobody got an SMS yet.
    assert events == ["guardrails_outcome", "prediction"]


# ---------------------------------------------------------------------------
# Contract 4 — hash chain unbroken across the full pipeline
# ---------------------------------------------------------------------------


def test_hash_chain_unbroken_end_to_end(pipeline, monkeypatch):
    """After the full safety_gate -> sender sequence, verify_chain must
    return True. This catches ordering bugs where an audit row is written
    with the wrong prev_hash, silently breaking forensic replay."""
    _seed_resident(pipeline.residents_table, "r1", lat=34.06, lon=-118.25)

    monkeypatch.setattr(
        pipeline.safety_gate, "generate_advisory", lambda *_, **__: _clean_advisory()
    )
    monkeypatch.setattr(
        pipeline.safety_gate,
        "validate_advisory",
        lambda text, confidence=None: {"passed": True, "blocked_reason": None},
    )
    monkeypatch.setattr(pipeline.sender, "mark_alert_sent", pipeline.audit.mark_alert_sent)

    mock_sns = MagicMock()
    mock_sns.publish.return_value = {"MessageId": "ok"}
    with mock_aws(), patch.object(pipeline.sender, "_sns", return_value=mock_sns):
        gate_result = pipeline.safety_gate.handler(_gate_event(confidence=0.9))
        pipeline.sender.handler({
            "fire_event": _fire_event(),
            "advisory": gate_result["advisory"],
            "prediction_id": gate_result["prediction_id"],
            "action": "APPROVED",
        })

    assert pipeline.audit.verify_chain("fire-contract-001") is True

    rows = [r for r in pipeline.audit_table.scan()["Items"] if r["fire_id"] == "fire-contract-001"]
    events = sorted(r["event"] for r in rows)
    assert events == ["alert_sent", "guardrails_outcome", "prediction"]


def test_hash_chain_detects_tampering_after_full_run(pipeline, monkeypatch):
    """Smoke test the tamper-detection behaviour on a realistic 3-row chain.
    If a post-incident auditor edits the prediction row (e.g. to rewrite the
    confidence score), verify_chain must catch it."""
    _seed_resident(pipeline.residents_table, "r1", lat=34.06, lon=-118.25)

    monkeypatch.setattr(
        pipeline.safety_gate, "generate_advisory", lambda *_, **__: _clean_advisory()
    )
    monkeypatch.setattr(
        pipeline.safety_gate,
        "validate_advisory",
        lambda text, confidence=None: {"passed": True, "blocked_reason": None},
    )
    monkeypatch.setattr(pipeline.sender, "mark_alert_sent", pipeline.audit.mark_alert_sent)

    mock_sns = MagicMock()
    mock_sns.publish.return_value = {"MessageId": "ok"}
    with mock_aws(), patch.object(pipeline.sender, "_sns", return_value=mock_sns):
        gate_result = pipeline.safety_gate.handler(
            _gate_event(confidence=0.9, fire_id="fire-tamper-001")
        )
        pipeline.sender.handler({
            "fire_event": _fire_event("fire-tamper-001"),
            "advisory": gate_result["advisory"],
            "prediction_id": gate_result["prediction_id"],
            "action": "APPROVED",
        })

    # Rewrite the confidence on the prediction row — simulates a bad actor
    # editing DynamoDB after the fact.
    rows = pipeline.audit_table.scan()["Items"]
    prediction = next(r for r in rows if r["event"] == "prediction" and r["fire_id"] == "fire-tamper-001")
    pipeline.audit_table.update_item(
        Key={"prediction_id": prediction["prediction_id"], "written_at": prediction["written_at"]},
        UpdateExpression="SET confidence = :c",
        ExpressionAttributeValues={":c": Decimal("0.01")},
    )

    assert pipeline.audit.verify_chain("fire-tamper-001") is False
