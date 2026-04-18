"""
Validate the Bedrock advisory prompt template without calling the live API.

Tests the prompt formatting, output parsing, and safety rule enforcement
(confidence flag injection, SMS length cap) using a mock Bedrock response.
The live API round-trip is tested separately in scripts/test_endpoint.py.

Run: python ml/bedrock/test_prompts.py
"""

import json
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from advisory_prompt import generate_advisory, _format_prompt, CONFIDENCE_THRESHOLD

# A representative enriched fire event (Thousand Oaks scenario).
SAMPLE_FIRE = {
    "fire_id": "test-fire-001",
    "source": "CALFIRE",
    "lat": 34.1705,
    "lon": -118.8376,
    "spread_rate_km2_per_hr": 2.1,
    "wind_speed_ms": 6.2,
    "wind_direction_deg": 270,
    "population_at_risk": 1200,
    "containment_pct": 0.0,
    "radiative_power": 450.0,
    "detected_at": "2026-04-18T14:00:00Z",
    "nearest_stations": [
        {"station_id": "station-001", "distance_km": 8.5, "available": True},
        {"station_id": "station-002", "distance_km": 12.1, "available": False},
    ],
    "watershed_sites_at_risk": ["USGS-001", "USGS-002"],
}

SAMPLE_RECOMMENDATION_HIGH = {
    "dispatch_level": 1,
    "recommendation": "MUTUAL_AID",
    "confidence": 0.93,
    "probabilities": {"LOCAL": 0.02, "MUTUAL_AID": 0.93, "AERIAL": 0.05},
}

SAMPLE_RECOMMENDATION_LOW_CONFIDENCE = {
    "dispatch_level": 1,
    "recommendation": "MUTUAL_AID",
    "confidence": 0.45,  # below threshold — must trigger preliminary flag
    "probabilities": {"LOCAL": 0.30, "MUTUAL_AID": 0.45, "AERIAL": 0.25},
}


def _mock_bedrock_response(sms: str, brief: str):
    """Build a mock boto3 Bedrock response wrapping the given advisory."""
    body_bytes = json.dumps({
        "content": [{"text": json.dumps({"sms": sms, "brief": brief})}]
    }).encode()
    mock_stream = MagicMock()
    mock_stream.read.return_value = body_bytes
    return {"body": mock_stream}


class TestPromptFormatting(unittest.TestCase):
    def test_prompt_contains_fire_data(self):
        prompt = _format_prompt(SAMPLE_FIRE, SAMPLE_RECOMMENDATION_HIGH)
        self.assertIn("34.1705", prompt)
        self.assertIn("2.1", prompt)      # spread rate
        self.assertIn("1200", prompt)     # population
        self.assertIn("MUTUAL_AID", prompt)

    def test_station_summary_truncates_at_three(self):
        fire = {**SAMPLE_FIRE, "nearest_stations": [
            {"station_id": f"s-{i}", "distance_km": float(i), "available": True}
            for i in range(5)
        ]}
        prompt = _format_prompt(fire, SAMPLE_RECOMMENDATION_HIGH)
        # Only first 3 stations should appear
        self.assertNotIn("s-3", prompt)
        self.assertNotIn("s-4", prompt)


class TestAdvisoryGeneration(unittest.TestCase):
    @patch("advisory_prompt.boto3.client")
    def test_high_confidence_advisory(self, mock_boto):
        mock_boto.return_value.invoke_model.return_value = _mock_bedrock_response(
            sms="WILDFIRE ALERT: Evacuate Thousand Oaks hills area immediately. Drive north on Hwy 23.",
            brief="A spreading fire in the Thousand Oaks hills threatens 1,200 residents. Mutual aid units dispatched. Wind conditions may accelerate spread toward residential zones.",
        )
        advisory = generate_advisory(SAMPLE_FIRE, SAMPLE_RECOMMENDATION_HIGH)

        self.assertIn("sms", advisory)
        self.assertIn("brief", advisory)
        self.assertLessEqual(len(advisory["sms"]), 160, "SMS must not exceed 160 chars")
        # High confidence — preliminary flag should NOT be present
        self.assertNotIn("PRELIMINARY", advisory["sms"])

    @patch("advisory_prompt.boto3.client")
    def test_low_confidence_injects_preliminary_flag(self, mock_boto):
        # Simulate model returning advisory WITHOUT the flag (we enforce it ourselves)
        mock_boto.return_value.invoke_model.return_value = _mock_bedrock_response(
            sms="WILDFIRE ALERT: Monitor situation in Thousand Oaks area.",
            brief="Preliminary assessment indicates possible fire spread.",
        )
        advisory = generate_advisory(SAMPLE_FIRE, SAMPLE_RECOMMENDATION_LOW_CONFIDENCE)

        self.assertIn("PRELIMINARY ADVISORY - HUMAN REVIEW PENDING", advisory["sms"],
                      "Low-confidence advisory must carry the preliminary flag")
        self.assertLessEqual(len(advisory["sms"]), 160)

    @patch("advisory_prompt.boto3.client")
    def test_sms_truncated_if_over_160_chars(self, mock_boto):
        long_sms = "A" * 200  # deliberately over limit
        mock_boto.return_value.invoke_model.return_value = _mock_bedrock_response(
            sms=long_sms, brief="Short brief."
        )
        advisory = generate_advisory(SAMPLE_FIRE, SAMPLE_RECOMMENDATION_HIGH)
        self.assertLessEqual(len(advisory["sms"]), 160)
        self.assertTrue(advisory["sms"].endswith("..."))

    @patch("advisory_prompt.boto3.client")
    def test_malformed_json_raises(self, mock_boto):
        body_bytes = json.dumps({
            "content": [{"text": "This is not JSON"}]
        }).encode()
        mock_stream = MagicMock()
        mock_stream.read.return_value = body_bytes
        mock_boto.return_value.invoke_model.return_value = {"body": mock_stream}

        with self.assertRaises(ValueError, msg="Should raise on non-JSON Bedrock response"):
            generate_advisory(SAMPLE_FIRE, SAMPLE_RECOMMENDATION_HIGH)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2)
    sys.exit(0 if result.result.wasSuccessful() else 1)
