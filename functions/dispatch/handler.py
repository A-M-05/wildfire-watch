"""
Dispatch trigger Lambda — issue #10.

Receives FireEnriched events from EventBridge and decides whether to start
the Step Functions safety workflow. Evaluates three OR-conditions:

  risk_score >= 0.6   (composite score from enrich Lambda)
  spread_rate >= 2.0 km²/hr
  population_at_risk >= 500

EventBridge can't express OR conditions across different detail fields in a
single rule, so the rule matches ALL FireEnriched events and this Lambda
acts as the threshold gate. This also gives us an explicit audit log entry
for every fire evaluated, even ones that don't trigger dispatch.

EventBridge source: wildfire-watch.enrichment / FireEnriched
Target: Step Functions wildfire-watch-safety state machine
"""

import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
STATE_MACHINE_ARN = os.environ.get("WW_STEP_FUNCTIONS_ARN", "")

# Dispatch thresholds — any one triggers. Keep in sync with pipeline-agent.md.
RISK_SCORE_TRIGGER = float(os.environ.get("WW_RISK_SCORE_TRIGGER", "0.6"))
SPREAD_RATE_TRIGGER = float(os.environ.get("WW_SPREAD_RATE_TRIGGER", "2.0"))
POPULATION_TRIGGER = int(os.environ.get("WW_POPULATION_TRIGGER", "500"))

_sfn = None


def _get_sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions", region_name=_REGION)
    return _sfn


def _should_dispatch(fire: dict) -> tuple[bool, str]:
    """Evaluate all dispatch thresholds and return (triggered, reason).

    Returns the first threshold that fired so it can be logged and audited.
    All three are checked to log the full picture even if only one triggers.
    """
    risk = float(fire.get("risk_score", 0))
    spread = float(fire.get("spread_rate_km2_per_hr", 0))
    population = int(float(fire.get("population_at_risk", 0)))

    reasons = []
    if risk >= RISK_SCORE_TRIGGER:
        reasons.append(f"risk_score={risk:.3f}>={RISK_SCORE_TRIGGER}")
    if spread >= SPREAD_RATE_TRIGGER:
        reasons.append(f"spread_rate={spread:.1f}>={SPREAD_RATE_TRIGGER}")
    if population >= POPULATION_TRIGGER:
        reasons.append(f"population={population}>={POPULATION_TRIGGER}")

    return bool(reasons), " | ".join(reasons) if reasons else "below all thresholds"


def start_dispatch(fire: dict, reason: str) -> str:
    """Start a Step Functions execution for this fire event.

    Execution name is fire_id + timestamp — must be unique per execution.
    Step Functions enforces uniqueness within 90 days.
    """
    fire_id = fire.get("fire_id", "unknown")
    # Execution names can only contain alphanumeric, -, _
    safe_id = fire_id.replace(":", "-").replace(".", "-")[:40]
    execution_name = f"dispatch-{safe_id}-{int(time.time())}"

    recommendation = fire.get("dispatch_recommendation", {})

    response = _get_sfn().start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=execution_name,
        # Step Functions receives the full fire context so the safety gate (#21)
        # has everything it needs without a DynamoDB lookup.
        input=json.dumps({
            "fire_event": fire,
            "recommendation": recommendation,
            "dispatch_trigger_reason": reason,
        }, default=str),
    )
    return response["executionArn"]


def handler(event, context):
    """EventBridge trigger — evaluate thresholds, start Step Functions if triggered."""
    fire = event.get("detail", {})
    fire_id = fire.get("fire_id", "unknown")

    triggered, reason = _should_dispatch(fire)

    logger.info(json.dumps({
        "event": "dispatch_evaluated",
        "fire_id": fire_id,
        "triggered": triggered,
        "reason": reason,
        "risk_score": fire.get("risk_score"),
        "spread_rate": fire.get("spread_rate_km2_per_hr"),
        "population": fire.get("population_at_risk"),
    }))

    if not triggered:
        return {"dispatched": False, "fire_id": fire_id, "reason": reason}

    if not STATE_MACHINE_ARN:
        logger.error("WW_STEP_FUNCTIONS_ARN not set — cannot start dispatch")
        raise EnvironmentError("WW_STEP_FUNCTIONS_ARN not configured")

    execution_arn = start_dispatch(fire, reason)
    logger.info(json.dumps({
        "event": "dispatch_started",
        "fire_id": fire_id,
        "execution_arn": execution_arn,
        "reason": reason,
    }))

    return {"dispatched": True, "fire_id": fire_id, "execution_arn": execution_arn}
