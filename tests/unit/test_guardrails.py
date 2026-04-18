"""Unit tests for the Bedrock guardrails wrapper (#16).

apply_guardrail is stubbed via unittest.mock — moto's Bedrock support is
thin and we want full control over the response shapes anyway.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env():
    os.environ["WW_BEDROCK_GUARDRAIL_ID"] = "test-guardrail"
    os.environ["WW_BEDROCK_GUARDRAIL_VERSION"] = "DRAFT"
    os.environ["WW_CONFIDENCE_THRESHOLD"] = "0.65"
    yield


@pytest.fixture
def guardrails(_env):
    import importlib

    from ml.bedrock import guardrails as module

    importlib.reload(module)
    return module


def _stub_bedrock(action="NONE", assessments=None):
    client = MagicMock()
    client.apply_guardrail.return_value = {
        "action": action,
        "assessments": assessments or [],
        "outputs": [{"text": "stubbed output"}],
    }
    return client


def test_passes_when_bedrock_returns_none(guardrails):
    with patch("boto3.client", return_value=_stub_bedrock("NONE")):
        result = guardrails.validate_advisory("Evacuate east via Hwy 101.", confidence=0.9)
    assert result == {"passed": True, "action": "NONE", "blocked_reason": None}


def test_blocks_when_bedrock_intervenes(guardrails):
    assessments = [{
        "topicPolicy": {"topics": [{"name": "FalseCertainty", "type": "DENY", "action": "BLOCKED"}]},
    }]
    with patch("boto3.client", return_value=_stub_bedrock("GUARDRAIL_INTERVENED", assessments)):
        result = guardrails.validate_advisory("You are completely safe.", confidence=0.9)
    assert result["passed"] is False
    assert result["action"] == "GUARDRAIL_INTERVENED"
    assert "FalseCertainty" in result["blocked_reason"]


def test_blocks_pii_via_assessment_summary(guardrails):
    assessments = [{
        "sensitiveInformationPolicy": {
            "piiEntities": [{"type": "PHONE", "action": "ANONYMIZED"}],
        },
    }]
    with patch("boto3.client", return_value=_stub_bedrock("GUARDRAIL_INTERVENED", assessments)):
        result = guardrails.validate_advisory("Call resident at 555-0100 immediately.", confidence=0.9)
    assert result["passed"] is False
    assert "PHONE" in result["blocked_reason"]


def test_low_confidence_with_certainty_phrase_short_circuits(guardrails):
    # Bedrock should never be called — the in-process check fires first.
    with patch("boto3.client") as mock_client:
        result = guardrails.validate_advisory("You are definitely safe.", confidence=0.3)
    mock_client.assert_not_called()
    assert result["passed"] is False
    assert result["action"] == "CONFIDENCE_MISMATCH"
    assert "0.30" in result["blocked_reason"]
    assert "definitely safe" in result["blocked_reason"]


def test_low_confidence_without_certainty_phrase_still_calls_bedrock(guardrails):
    # Low confidence on its own is fine — the consistency check only fires
    # when the text ALSO claims certainty. Bedrock still arbitrates the rest.
    with patch("boto3.client", return_value=_stub_bedrock("NONE")) as mock_client:
        result = guardrails.validate_advisory("Conditions are uncertain — monitor updates.", confidence=0.3)
    mock_client.assert_called_once()
    assert result["passed"] is True


def test_high_confidence_with_certainty_phrase_defers_to_bedrock(guardrails):
    # High confidence + safety language is internally consistent, so the
    # in-process check passes; Bedrock's denied-topic policy is the backstop.
    assessments = [{
        "topicPolicy": {"topics": [{"name": "FalseCertainty", "type": "DENY", "action": "BLOCKED"}]},
    }]
    with patch("boto3.client", return_value=_stub_bedrock("GUARDRAIL_INTERVENED", assessments)):
        result = guardrails.validate_advisory("You are completely safe.", confidence=0.95)
    assert result["passed"] is False
    assert result["action"] == "GUARDRAIL_INTERVENED"


def test_no_confidence_skips_consistency_check(guardrails):
    # Callers that don't have a confidence value (e.g. testing the guardrail
    # alone) should still get a real Bedrock validation.
    with patch("boto3.client", return_value=_stub_bedrock("NONE")) as mock_client:
        result = guardrails.validate_advisory("Evacuate now.")
    mock_client.assert_called_once()
    assert result["passed"] is True


def test_certainty_phrase_check_is_case_insensitive(guardrails):
    with patch("boto3.client") as mock_client:
        result = guardrails.validate_advisory("YOU ARE DEFINITELY SAFE.", confidence=0.4)
    mock_client.assert_not_called()
    assert result["passed"] is False


def test_word_boundary_avoids_substring_false_positive(guardrails):
    # "you are safer than yesterday" contains the literal substring "you are
    # safe" but should NOT trigger the certainty rule — that would be a false
    # positive that pre-rejects valid comparative advisories.
    with patch("boto3.client", return_value=_stub_bedrock("NONE")) as mock_client:
        result = guardrails.validate_advisory(
            "Conditions are improving — you are safer than yesterday but stay alert.",
            confidence=0.3,
        )
    mock_client.assert_called_once()
    assert result["passed"] is True


def test_word_boundary_still_catches_phrase_with_punctuation(guardrails):
    # End-of-sentence punctuation must not break detection — these are the
    # most likely real-world forms.
    for text in ("You are safe.", "You are safe!", "You are safe, for now."):
        with patch("boto3.client") as mock_client:
            result = guardrails.validate_advisory(text, confidence=0.3)
        mock_client.assert_not_called()
        assert result["passed"] is False, f"failed to block: {text!r}"


def test_intervention_with_empty_assessments_falls_back_to_generic_reason(guardrails):
    # Defensive: if Bedrock ever returns GUARDRAIL_INTERVENED with no detail,
    # we still produce a non-empty blocked_reason so the audit row is useful.
    with patch("boto3.client", return_value=_stub_bedrock("GUARDRAIL_INTERVENED", assessments=[])):
        result = guardrails.validate_advisory("Some advisory text.", confidence=0.9)
    assert result["passed"] is False
    assert result["blocked_reason"] == "guardrail intervened (no detail)"


def test_threshold_boundary_does_not_trigger_check(guardrails):
    # confidence == threshold is "high enough" — only strictly below trips.
    with patch("boto3.client", return_value=_stub_bedrock("NONE")) as mock_client:
        result = guardrails.validate_advisory("You are completely safe.", confidence=0.65)
    # No short-circuit: Bedrock got the call.
    mock_client.assert_called_once()
    assert result["passed"] is True


def test_provision_config_has_expected_structure():
    # Regression guard against future config drift — if someone deletes
    # the PHONE/EMAIL PII rules the safety story silently breaks. Topic
    # policy was intentionally removed (see provision_guardrail docstring);
    # this test enforces it stays out unless someone consciously re-adds it.
    from ml.bedrock import provision_guardrail as p

    pii_types = {e["type"] for e in p.PII_POLICY["piiEntitiesConfig"]}
    # PHONE and EMAIL only — ADDRESS/NAME excluded due to NER false
    # positives on highway names and dispatcher titles.
    assert pii_types == {"PHONE", "EMAIL"}

    # All PII actions are BLOCK (matches the wrapper's any-intervention-fails policy)
    pii_actions = {e["action"] for e in p.PII_POLICY["piiEntitiesConfig"]}
    assert pii_actions == {"BLOCK"}

    # Topic policy intentionally absent — Bedrock's semantic topic detector
    # over-triggered on benign phrasing. The in-process certainty check is
    # the canonical guard for false-certainty.
    assert not hasattr(p, "TOPIC_POLICY")
