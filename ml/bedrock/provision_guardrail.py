"""Provision the wildfire-watch Bedrock Guardrail.

One-shot script — run once per environment, capture the returned
``guardrailId`` into ``WW_BEDROCK_GUARDRAIL_ID``.

Usage:
    python ml/bedrock/provision_guardrail.py --region us-west-2

The guardrail enforces PII blocking for phone numbers and email.

Two things were intentionally NOT delegated to Bedrock:

  * ADDRESS and NAME PII detection — Bedrock's NER classifies highway
    references ("Hwy 101") as ADDRESS and dispatcher titles ("Captain
    Smith") as NAME, both of which appear in every legitimate evacuation
    advisory. False-positive rate would block real demos.
  * False-certainty detection — Bedrock's topic-policy classifier is
    semantic and over-triggers on benign comparative phrasing
    ("you are safer than yesterday"). The in-process word-boundary
    check in ``guardrails.py`` has perfect precision over a curated
    phrase list and is the right tool for that job.

The wrapper rejects any guardrail intervention, so BLOCK (not ANONYMIZE)
keeps the Bedrock-side config aligned with end-to-end behavior.

This is idempotent-by-name: if a guardrail with the same name already
exists the script prints the existing ID and exits without modifying it.
"""

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError

NAME = "wildfire-watch-advisory"

BLOCKED_INPUT_MESSAGING = "Input contains content that violates the wildfire-watch advisory policy."
BLOCKED_OUTPUT_MESSAGING = "This advisory was blocked: it contains personal data that must not be sent in a broadcast SMS."

PII_POLICY = {
    "piiEntitiesConfig": [
        {"type": "PHONE", "action": "BLOCK"},
        {"type": "EMAIL", "action": "BLOCK"},
    ],
}

READY_TIMEOUT_SECONDS = 180
READY_POLL_INTERVAL = 3


def find_existing(client, name: str) -> str | None:
    paginator = client.get_paginator("list_guardrails")
    for page in paginator.paginate():
        for g in page.get("guardrails", []):
            if g.get("name") == name:
                return g.get("id")
    return None


def wait_until_ready(client, guardrail_id: str) -> None:
    """Poll get_guardrail until status is READY (or fail/timeout)."""
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        resp = client.get_guardrail(guardrailIdentifier=guardrail_id)
        status = resp.get("status")
        if status == "READY":
            return
        if status == "FAILED":
            reasons = resp.get("statusReasons") or resp.get("failureRecommendations") or "unknown"
            raise RuntimeError(f"Guardrail creation failed: {reasons}")
        time.sleep(READY_POLL_INTERVAL)
    raise TimeoutError(
        f"Guardrail {guardrail_id} did not reach READY within {READY_TIMEOUT_SECONDS}s"
    )


def main():
    parser = argparse.ArgumentParser(description="Provision the wildfire-watch Bedrock Guardrail.")
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()

    client = boto3.client("bedrock", region_name=args.region)

    existing = find_existing(client, NAME)
    if existing:
        print(f"Guardrail '{NAME}' already exists: {existing}")
        print(f"Set WW_BEDROCK_GUARDRAIL_ID={existing}")
        return

    try:
        resp = client.create_guardrail(
            name=NAME,
            description="Validates wildfire-watch evacuation advisories before SMS dispatch.",
            blockedInputMessaging=BLOCKED_INPUT_MESSAGING,
            blockedOutputsMessaging=BLOCKED_OUTPUT_MESSAGING,
            sensitiveInformationPolicyConfig=PII_POLICY,
        )
    except ClientError as e:
        print(f"Failed to create guardrail: {e}", file=sys.stderr)
        sys.exit(1)

    guardrail_id = resp["guardrailId"]
    print(f"Created guardrail '{NAME}': {guardrail_id}")

    print(f"Waiting for guardrail to reach READY status (up to {READY_TIMEOUT_SECONDS}s)...")
    try:
        wait_until_ready(client, guardrail_id)
    except (RuntimeError, TimeoutError) as e:
        print(f"Guardrail created but not ready: {e}", file=sys.stderr)
        print(f"Set WW_BEDROCK_GUARDRAIL_ID={guardrail_id} once status reaches READY.")
        sys.exit(2)

    print("Guardrail READY.")
    print(f"Set WW_BEDROCK_GUARDRAIL_ID={guardrail_id}")


if __name__ == "__main__":
    main()
