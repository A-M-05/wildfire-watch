"""Bedrock Guardrails wrapper for evacuation advisory validation.

Two layers of protection:

1. **In-process confidence-consistency check** — Bedrock guardrails can't see
   the SageMaker confidence score, so this layer flags advisories that claim
   certainty ("you are safe", "no danger") while the dispatch model's
   confidence is below the safety threshold.

2. **Bedrock ``apply_guardrail`` call** — enforces PII anonymization (phone
   numbers, addresses, names) and a "FalseCertainty" denied-topic policy
   configured in ``provision_guardrail.py``.

The safety gate Lambda (#21) calls ``validate_advisory(text, confidence)``
and only proceeds if ``passed`` is True.
"""

import os
import re

import boto3

DEFAULT_CONFIDENCE_THRESHOLD = float(os.environ.get("WW_CONFIDENCE_THRESHOLD", "0.65"))

# Phrases that imply absolute safety. If the dispatch model's confidence is
# below threshold and the advisory contains any of these, we treat it as a
# contradiction the Bedrock guardrail cannot detect on its own (it has no
# access to the SageMaker confidence score).
#
# Word boundaries are required so "you are safer than before" does NOT
# trigger the "you are safe" rule — that's a real false positive risk
# under naive substring matching.
CERTAINTY_PHRASES = (
    "you are safe",
    "you're safe",
    "definitely safe",
    "completely safe",
    "guaranteed safe",
    "no danger",
    "no risk",
    "no threat",
    "nothing to worry about",
    "everything is fine",
)

_CERTAINTY_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
    for phrase in CERTAINTY_PHRASES
)

# Map each Bedrock policy key to the list-of-findings keys it can contain.
# Keeps _summarize_assessments honest about what fields exist where.
_POLICY_LIST_KEYS = {
    "topicPolicy": ("topics",),
    "contentPolicy": ("filters",),
    "sensitiveInformationPolicy": ("piiEntities", "regexes"),
    "wordPolicy": ("customWords", "managedWordLists"),
}


def _check_confidence_consistency(
    text: str,
    confidence: float | None,
    threshold: float,
) -> str | None:
    """Return a human-readable reason if low-confidence text claims certainty."""
    if confidence is None or confidence >= threshold:
        return None
    for pattern in _CERTAINTY_PATTERNS:
        m = pattern.search(text)
        if m:
            return (
                f"confidence {confidence:.2f} below threshold {threshold:.2f} "
                f"but advisory claims certainty: '{m.group(0).lower()}'"
            )
    return None


def _summarize_assessments(assessments: list) -> str:
    """Flatten Bedrock's nested assessment payload into a one-line reason."""
    reasons = []
    for assessment in assessments:
        for policy_key, list_keys in _POLICY_LIST_KEYS.items():
            policy = assessment.get(policy_key, {})
            for list_key in list_keys:
                for filt in policy.get(list_key, []):
                    name = filt.get("name") or filt.get("type") or filt.get("match")
                    action = filt.get("action")
                    if name and action and action != "NONE":
                        reasons.append(f"{policy_key}:{name}({action})")
    return "; ".join(reasons) or "guardrail intervened (no detail)"


def validate_advisory(
    text: str,
    confidence: float | None = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict:
    """Validate an advisory before it reaches residents.

    Returns:
        {
          "passed": bool,
          "action": "NONE" | "GUARDRAIL_INTERVENED" | "CONFIDENCE_MISMATCH",
          "blocked_reason": str | None,
        }
    """
    contradiction = _check_confidence_consistency(text, confidence, threshold)
    if contradiction:
        return {
            "passed": False,
            "action": "CONFIDENCE_MISMATCH",
            "blocked_reason": contradiction,
        }

    bedrock = boto3.client("bedrock-runtime")
    response = bedrock.apply_guardrail(
        guardrailIdentifier=os.environ["WW_BEDROCK_GUARDRAIL_ID"],
        guardrailVersion=os.environ.get("WW_BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
        source="OUTPUT",
        content=[{"text": {"text": text}}],
    )
    passed = response.get("action") == "NONE"
    return {
        "passed": passed,
        "action": response.get("action", "UNKNOWN"),
        "blocked_reason": None if passed else _summarize_assessments(response.get("assessments", [])),
    }
