"""Unit tests for the safety gate Lambda (#21).

We mock the three collaborators (advisory generation, audit log, guardrails)
because (a) advisory_prompt isn't merged to main yet (lives on ml-pipeline),
and (b) the gate's job is orchestration - the integration test (#32) is
where the real wiring gets exercised end-to-end.
"""

import sys
import types
from unittest.mock import MagicMock, call

import pytest


# advisory_prompt lives on the ml-pipeline branch and isn't on main yet.
# Inject a stub into sys.modules BEFORE importing the gate so the top-level
# import in safety_gate.py resolves. Tests then patch the function via this
# stub module (or directly on the safety_gate module's binding).
@pytest.fixture(autouse=True)
def stub_advisory_prompt():
    # Don't touch the parent packages - they already resolve via the real
    # ml/bedrock/ directory on disk (guardrails.py lives there). Just plant
    # the missing submodule so the top-level import in safety_gate resolves.
    stub = types.ModuleType("ml.bedrock.advisory_prompt")
    stub.generate_advisory = MagicMock()
    sys.modules["ml.bedrock.advisory_prompt"] = stub
    yield stub
    sys.modules.pop("ml.bedrock.advisory_prompt", None)


@pytest.fixture
def gate(stub_advisory_prompt, monkeypatch):
    monkeypatch.setenv("WW_CONFIDENCE_THRESHOLD", "0.65")
    # Reload to bind to the freshly-stubbed advisory_prompt module.
    import importlib

    from functions.alert import safety_gate

    importlib.reload(safety_gate)
    return safety_gate


@pytest.fixture
def mocks(gate, monkeypatch):
    """Patch the three collaborators on the loaded gate module."""
    fake_log = MagicMock(return_value="pred-123")
    fake_outcome = MagicMock(return_value="outcome-456")
    fake_validate = MagicMock(return_value={"passed": True, "blocked_reason": None})
    fake_generate = MagicMock(return_value={"sms": "Evacuate east.", "brief": "Fire near Hwy 101."})

    monkeypatch.setattr(gate, "log_prediction", fake_log)
    monkeypatch.setattr(gate, "append_guardrail_outcome", fake_outcome)
    monkeypatch.setattr(gate, "validate_advisory", fake_validate)
    monkeypatch.setattr(gate, "generate_advisory", fake_generate)

    return types.SimpleNamespace(
        log=fake_log,
        outcome=fake_outcome,
        validate=fake_validate,
        generate=fake_generate,
    )


def _event(confidence: float = 0.9) -> dict:
    return {
        "fire_event": {"fire_id": "fire-001", "lat": 34.2, "lon": -118.5},
        "recommendation": {"recommendation": "dispatch 2 engines", "confidence": confidence},
    }


# ---------------------------------------------------------------------------
# Happy path + routing
# ---------------------------------------------------------------------------


def test_high_confidence_passing_guardrails_returns_approved(gate, mocks):
    resp = gate.handler(_event(confidence=0.9))
    assert resp["action"] == "APPROVED"
    assert resp["prediction_id"] == "pred-123"
    assert resp["advisory"] == {"sms": "Evacuate east.", "brief": "Fire near Hwy 101."}


def test_low_confidence_passing_guardrails_returns_human_review(gate, mocks):
    resp = gate.handler(_event(confidence=0.4))
    assert resp["action"] == "HUMAN_REVIEW_REQUIRED"
    assert resp["prediction_id"] == "pred-123"
    assert "advisory" in resp


def test_failing_guardrails_returns_blocked_with_reason(gate, mocks):
    mocks.validate.return_value = {
        "passed": False,
        "blocked_reason": "sensitiveInformationPolicy:PHONE(BLOCKED)",
    }
    resp = gate.handler(_event(confidence=0.9))
    assert resp["action"] == "BLOCKED"
    assert resp["blocked_reason"] == "sensitiveInformationPolicy:PHONE(BLOCKED)"
    assert "advisory" not in resp  # don't expose blocked content downstream


def test_blocked_takes_priority_over_low_confidence(gate, mocks):
    # If guardrails block AND confidence is low, BLOCKED wins. There's no
    # human review of unsafe content - the advisory itself is the problem.
    mocks.validate.return_value = {"passed": False, "blocked_reason": "topicPolicy:X"}
    resp = gate.handler(_event(confidence=0.3))
    assert resp["action"] == "BLOCKED"


def test_threshold_boundary_is_inclusive_at_high_end(gate, mocks):
    # confidence == threshold counts as "high enough" - matches guardrails.py
    resp = gate.handler(_event(confidence=0.65))
    assert resp["action"] == "APPROVED"


def test_threshold_overridable_via_env(gate, mocks, monkeypatch):
    monkeypatch.setenv("WW_CONFIDENCE_THRESHOLD", "0.9")
    resp = gate.handler(_event(confidence=0.8))
    assert resp["action"] == "HUMAN_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# Order-of-operations contract - the safety story depends on this
# ---------------------------------------------------------------------------


def test_log_prediction_called_before_validate(gate, mocks):
    # The hard rule from CLAUDE.md: audit row before any safety-relevant
    # downstream action. validate_advisory IS safety-relevant.
    order = []
    mocks.log.side_effect = lambda *a, **kw: order.append("log") or "pred-123"
    mocks.validate.side_effect = lambda *a, **kw: order.append("validate") or {
        "passed": True, "blocked_reason": None
    }
    mocks.outcome.side_effect = lambda *a, **kw: order.append("outcome") or "out-1"

    gate.handler(_event(confidence=0.9))
    assert order == ["log", "validate", "outcome"]


def test_outcome_row_links_to_prediction_id(gate, mocks):
    gate.handler(_event(confidence=0.9))
    args, kwargs = mocks.outcome.call_args
    assert args[0] == "fire-001"        # fire_id
    assert args[1] == "pred-123"        # prediction_id (the link)
    assert kwargs == {"passed": True, "reason": None}


def test_outcome_row_written_even_when_blocked(gate, mocks):
    # The audit chain captures blocked outcomes too - that's the whole point
    # of an audit log: blocked attempts are evidence, not noise.
    mocks.validate.return_value = {"passed": False, "blocked_reason": "PII"}
    gate.handler(_event(confidence=0.9))
    mocks.outcome.assert_called_once()
    _, kwargs = mocks.outcome.call_args
    assert kwargs == {"passed": False, "reason": "PII"}


# ---------------------------------------------------------------------------
# Failure semantics - what gets called (or not) on collaborator failure
# ---------------------------------------------------------------------------


def test_advisory_generation_failure_writes_no_audit_row(gate, mocks):
    mocks.generate.side_effect = RuntimeError("Bedrock 5xx")
    with pytest.raises(RuntimeError, match="Bedrock 5xx"):
        gate.handler(_event(confidence=0.9))
    mocks.log.assert_not_called()
    mocks.validate.assert_not_called()
    mocks.outcome.assert_not_called()


def test_log_prediction_failure_halts_before_guardrails(gate, mocks):
    # Hard rule: never proceed past the audit write. If it raises, halt -
    # the system's safety story depends on the audit row being committed.
    mocks.log.side_effect = RuntimeError("DynamoDB throttled")
    with pytest.raises(RuntimeError, match="DynamoDB throttled"):
        gate.handler(_event(confidence=0.9))
    mocks.validate.assert_not_called()
    mocks.outcome.assert_not_called()


def test_guardrails_validation_called_with_sms_and_confidence(gate, mocks):
    # Must pass the SMS text (the actual delivery payload) and the model
    # confidence (so guardrails' in-process certainty check can run).
    gate.handler(_event(confidence=0.42))
    mocks.validate.assert_called_once_with("Evacuate east.", confidence=0.42)
