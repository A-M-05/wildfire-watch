"""
Bedrock advisory generation for wildfire dispatch — issue #14.

Calls Claude claude-sonnet-4-6 via Bedrock to produce two outputs per fire event:
  sms   — max 160 chars, plain-English alert for residents
  brief — max 3 sentences, operational summary for dispatchers

Prompt caching is applied to the stable SYSTEM_PROMPT (which never changes)
so we only pay full tokenization cost on the first call. Subsequent calls in
the same 5-minute window hit the cache and are ~10x faster and cheaper.

The advisory is validated by Guardrails (#16) before it leaves this module —
see guardrails.validate_advisory(). Never pass the raw advisory directly to
SNS without that check.
"""

import json
import os
import boto3

MODEL_ID = "anthropic.claude-sonnet-4-6-20241022-v2:0"
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
CONFIDENCE_THRESHOLD = float(os.environ.get("WW_CONFIDENCE_THRESHOLD", "0.65"))

# The system prompt is stable across every call — only the fire data changes.
# Marking it with cache_control tells Bedrock to store the tokenized form for
# up to 5 minutes. At ~500 tokens this saves ~15ms and 500 input tokens per call.
SYSTEM_PROMPT = """You are an emergency management AI generating wildfire advisories.

Rules you must always follow:
- Be accurate, calm, and actionable. Use plain English.
- Never say residents are "definitely safe", "out of danger", or use any phrase implying certainty about safety when the situation is still evolving.
- Never name specific individuals.
- Never speculate beyond the data you are given.
- If the confidence score is below 0.65, you MUST include the phrase "PRELIMINARY ADVISORY - HUMAN REVIEW PENDING" in the SMS.
- The SMS must be 160 characters or fewer (hard limit — SMS carriers truncate longer messages).
- The brief is for fire dispatchers, not residents — include operational details.

Output valid JSON only, with exactly these two keys:
  {"sms": "<resident SMS under 160 chars>", "brief": "<dispatcher brief, max 3 sentences>"}
Do not include markdown, code blocks, or any text outside the JSON object."""

ADVISORY_PROMPT = """Fire data:
- Location: {lat}, {lon}
- Spread rate: {spread_rate_km2_per_hr} km²/hr
- Wind: {wind_speed_ms} m/s from {wind_direction_deg}°
- Population at risk: {population_at_risk}
- Containment: {containment_pct}%
- Confidence score: {confidence}
- Dispatch recommendation: {recommendation}

Nearest fire stations: {nearest_stations_summary}
Watershed sites at risk: {watershed_sites_summary}

Generate the SMS advisory and dispatch brief now."""


def _format_prompt(fire_event: dict, recommendation: dict) -> str:
    """Fill the prompt template from a normalized enriched fire event."""
    stations = fire_event.get("nearest_stations", [])
    station_summary = ", ".join(
        f"{s['station_id']} ({s['distance_km']:.1f}km, {'available' if s['available'] else 'unavailable'})"
        for s in stations[:3]  # top 3 nearest
    ) or "none on record"

    watershed = fire_event.get("watershed_sites_at_risk", [])
    watershed_summary = ", ".join(watershed[:5]) if watershed else "none"

    return ADVISORY_PROMPT.format(
        lat=fire_event.get("lat", "unknown"),
        lon=fire_event.get("lon", "unknown"),
        spread_rate_km2_per_hr=fire_event.get("spread_rate_km2_per_hr", 0),
        wind_speed_ms=fire_event.get("wind_speed_ms", 0),
        wind_direction_deg=fire_event.get("wind_direction_deg", 0),
        population_at_risk=fire_event.get("population_at_risk", 0),
        containment_pct=fire_event.get("containment_pct", 0),
        confidence=round(recommendation.get("confidence", 0), 3),
        recommendation=recommendation.get("recommendation", "unknown"),
        nearest_stations_summary=station_summary,
        watershed_sites_summary=watershed_summary,
    )


def generate_advisory(fire_event: dict, recommendation: dict) -> dict:
    """Call Bedrock to generate an SMS advisory and dispatcher brief.

    Args:
        fire_event: enriched fire event dict (see CLAUDE.md schema)
        recommendation: output of model.predict() — has 'recommendation' and 'confidence'

    Returns:
        dict with 'sms' (str, ≤160 chars) and 'brief' (str, ≤3 sentences)

    Raises:
        ValueError: if Bedrock returns malformed JSON or SMS exceeds 160 chars
        botocore.exceptions.ClientError: on Bedrock API failure
    """
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    user_prompt = _format_prompt(fire_event, recommendation)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        # System prompt uses prompt caching — stable content, only tokenized once per 5min window.
        "system": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        # User message contains the dynamic fire data — never cached.
        "messages": [{"role": "user", "content": user_prompt}],
    }

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )

    result = json.loads(response["body"].read())
    raw_text = result["content"][0]["text"].strip()

    try:
        advisory = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Bedrock returned non-JSON advisory: {raw_text!r}") from e

    if "sms" not in advisory or "brief" not in advisory:
        raise ValueError(f"Advisory missing required keys: {advisory}")

    # Enforce the 160-char SMS hard limit — carriers truncate silently beyond this.
    if len(advisory["sms"]) > 160:
        advisory["sms"] = advisory["sms"][:157] + "..."

    # If confidence is below threshold, the SMS must carry the preliminary flag.
    # This is a safety rule — the model is instructed to include it, but we
    # enforce it here as a belt-and-suspenders check.
    confidence = recommendation.get("confidence", 1.0)
    flag = "PRELIMINARY ADVISORY - HUMAN REVIEW PENDING"
    if confidence < CONFIDENCE_THRESHOLD and flag not in advisory["sms"]:
        advisory["sms"] = (advisory["sms"][:137] + " " + flag)[:160]

    return advisory
