"""
Dispatcher notification Lambda — called by Step Functions when confidence < 0.65.

Step Functions invokes this with WAIT_FOR_TASK_TOKEN integration, meaning:
  - The execution pauses after this Lambda returns
  - The Lambda must store or forward the task_token somewhere the dispatcher can use it
  - The dispatcher resumes the execution by calling sfn:SendTaskSuccess(taskToken, output)
    or sfn:SendTaskFailure(taskToken, error) — typically from a dashboard UI or Slack bot
  - If nobody responds within the heartbeat window (5 min), Step Functions raises
    States.HeartbeatTimeout and the catch handler routes to EscalateAndAlert

This Lambda does two things:
  1. Publishes an SNS alert to the dispatcher topic with fire details + task token
  2. Writes the pending review to DynamoDB so the dispatcher UI (#28) can list open reviews

The task token is the resume key — treat it like a one-time password.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
SNS_ALERT_TOPIC_ARN = os.environ.get("WW_SNS_ALERT_TOPIC_ARN", "")
FIRES_TABLE = os.environ.get("WW_DYNAMODB_FIRES_TABLE", "fires")
CONFIDENCE_THRESHOLD = float(os.environ.get("WW_CONFIDENCE_THRESHOLD", "0.65"))

_sns = None
_ddb = None


def _get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=_REGION)
    return _sns


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=_REGION)
    return _ddb


def _format_dispatcher_alert(fire: dict, recommendation: dict, task_token: str) -> str:
    """Build the SNS message body sent to the dispatcher for human review."""
    fire_id = fire.get("fire_id", "unknown")
    confidence = recommendation.get("confidence", 0)
    rec = recommendation.get("recommendation", "unknown")
    spread = fire.get("spread_rate_km2_per_hr", 0)
    population = fire.get("population_at_risk", 0)
    lat = fire.get("lat", 0)
    lon = fire.get("lon", 0)

    return (
        f"HUMAN REVIEW REQUIRED — Wildfire Dispatch\n"
        f"Fire ID: {fire_id}\n"
        f"Location: {lat:.4f}, {lon:.4f}\n"
        f"Model recommendation: {rec}\n"
        f"Confidence: {confidence:.1%} (below {CONFIDENCE_THRESHOLD:.0%} threshold)\n"
        f"Spread rate: {spread:.1f} km²/hr | Population at risk: {population:,}\n"
        f"\n"
        f"To APPROVE this dispatch, call:\n"
        f"  aws stepfunctions send-task-success \\\n"
        f"    --task-token '{task_token}' \\\n"
        f"    --task-output '{{\"approved\": true}}'\n"
        f"\n"
        f"To REJECT (stop dispatch), call:\n"
        f"  aws stepfunctions send-task-failure \\\n"
        f"    --task-token '{task_token}' \\\n"
        f"    --error 'HumanRejected' --cause 'Dispatcher rejected low-confidence dispatch'\n"
        f"\n"
        f"This review will auto-escalate in 5 minutes if no response."
    )


def _store_pending_review(fire_id: str, task_token: str, fire: dict, recommendation: dict):
    """Write the pending review to DynamoDB so the dispatcher UI can list it.

    The dispatch panel (#28) queries fires with pending_review=True so dispatchers
    see an in-app prompt instead of just the SNS message. The task token is stored
    here so the UI can call SendTaskSuccess without the dispatcher using the CLI.

    The task token is sensitive — treat like a session token, not a secret,
    but don't log it to CloudWatch (log just the fire_id instead).
    """
    table = _get_ddb().Table(FIRES_TABLE)
    table.update_item(
        Key={"fire_id": fire_id, "detected_at": fire.get("detected_at", "")},
        UpdateExpression="SET pending_review = :t, review_task_token = :tok, review_confidence = :c",
        ExpressionAttributeValues={
            ":t": True,
            ":tok": task_token,  # stored for UI-driven approval
            ":c": str(recommendation.get("confidence", 0)),
        },
    )
    logger.info(f"fire_id={fire_id} pending review written to DynamoDB")


def handler(event, context):
    """Step Functions task handler — notify dispatcher and wait for approval."""
    task_token = event.get("task_token")
    fire = event.get("fire_event", {})
    recommendation = event.get("recommendation", {})

    fire_id = fire.get("fire_id", "unknown")
    confidence = recommendation.get("confidence", 0)

    logger.info(f"fire_id={fire_id} confidence={confidence:.3f} — dispatching for human review")

    # 1. Publish SNS alert to dispatcher topic.
    if SNS_ALERT_TOPIC_ARN:
        message = _format_dispatcher_alert(fire, recommendation, task_token)
        _get_sns().publish(
            TopicArn=SNS_ALERT_TOPIC_ARN,
            Subject=f"[REVIEW REQUIRED] Wildfire dispatch — confidence {confidence:.0%}",
            Message=message,
        )
        logger.info(f"fire_id={fire_id} dispatcher SNS notification sent")
    else:
        logger.warning("WW_SNS_ALERT_TOPIC_ARN not set — SNS notification skipped")

    # 2. Store task token in DynamoDB for the dispatcher UI.
    # Do NOT log the task token itself — it's a resume credential.
    if fire_id != "unknown":
        _store_pending_review(fire_id, task_token, fire, recommendation)

    # Lambda returns immediately — Step Functions pauses here until
    # SendTaskSuccess / SendTaskFailure is called with the task_token.
    return {"status": "waiting_for_approval", "fire_id": fire_id}
