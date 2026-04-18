# Skill: AI Safety (DynamoDB hash chain + Guardrails + Clarify + Model Monitor)

Read this before writing any safety-related code. These patterns are non-negotiable.

> **Architecture note (2026-04-18):** QLDB was the original audit store, but AWS no longer accepts new ledger creations on this account. We use a DynamoDB table (`wildfire-watch-audit`) with a Lambda-computed SHA-256 hash chain instead. The "immutable audit" property is preserved — each record commits the hash of its predecessor, and a verifier can detect any tampering by replaying the chain.

## DynamoDB hash-chain audit — The hard rule

**The audit write must complete before any downstream action (SMS, Step Functions, etc.).**
If the `PutItem` throws, catch the exception and halt — do not proceed to alerting.

Each audit record stores:

| Field | Type | Notes |
|---|---|---|
| `prediction_id` | S (PK) | uuid4 |
| `written_at`    | S (SK) | ISO-8601 UTC |
| `fire_id`       | S (GSI PK) | links record to its fire |
| `recommendation`| M | full SageMaker dispatch response |
| `advisory_text` | S | the brief shown to dispatchers |
| `sms_text`      | S | the body that would go out |
| `confidence`    | N | float 0-1 |
| `guardrails_passed` | BOOL? | nullable until validation runs |
| `alert_sent`    | BOOL | flipped to true by the alert sender |
| `blocked_reason`| S? | populated if Guardrails blocks |
| `prev_hash`     | S | SHA-256 hex of the prior record's `record_hash` (or `"0" * 64` for the very first record per fire) |
| `record_hash`   | S | SHA-256 hex of canonical JSON of all fields above except `record_hash` itself |

```python
import os, json, hashlib, uuid, boto3
from datetime import datetime
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["WW_AUDIT_TABLE"])

GENESIS_HASH = "0" * 64

def _canonical_hash(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()

def _latest_hash_for_fire(fire_id: str) -> str:
    resp = table.query(
        IndexName="fire_id-written_at-index",
        KeyConditionExpression=Key("fire_id").eq(fire_id),
        ScanIndexForward=False,  # newest first
        Limit=1,
        ProjectionExpression="record_hash",
    )
    items = resp.get("Items", [])
    return items[0]["record_hash"] if items else GENESIS_HASH

def log_prediction(fire_id: str, recommendation: dict, advisory: dict) -> str:
    prediction_id = str(uuid.uuid4())
    record = {
        "prediction_id": prediction_id,
        "written_at": datetime.utcnow().isoformat() + "Z",
        "fire_id": fire_id,
        "recommendation": recommendation,
        "advisory_text": advisory.get("brief", ""),
        "sms_text": advisory.get("sms", ""),
        "confidence": recommendation["confidence"],
        "guardrails_passed": None,
        "alert_sent": False,
        "blocked_reason": None,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    record["record_hash"] = _canonical_hash(record)
    # ConditionExpression guards against accidental overwrite of an existing PK+SK
    table.put_item(
        Item=record,
        ConditionExpression="attribute_not_exists(prediction_id)",
    )
    return prediction_id
```

## Bedrock Guardrails — Validation pattern

```python
bedrock = boto3.client("bedrock-runtime")

def validate_advisory(text: str) -> dict:
    response = bedrock.apply_guardrail(
        guardrailIdentifier=os.environ["WW_BEDROCK_GUARDRAIL_ID"],
        guardrailVersion="DRAFT",
        source="OUTPUT",
        content=[{"text": {"text": text}}],
    )
    passed = response["action"] == "NONE"
    return {
        "passed": passed,
        "action": response["action"],
        "blocked_reason": response.get("outputs", [{}])[0].get("text") if not passed else None,
    }
```

After validation, append a *new* audit record (do not mutate the prior one — that would break the hash chain). The verifier expects each row to be append-only.

```python
def append_guardrail_outcome(fire_id: str, prediction_id: str, passed: bool, reason: str | None):
    record = {
        "prediction_id": str(uuid.uuid4()),     # new row, links to original prediction
        "written_at": datetime.utcnow().isoformat() + "Z",
        "fire_id": fire_id,
        "linked_prediction_id": prediction_id,
        "event": "guardrails_outcome",
        "guardrails_passed": passed,
        "blocked_reason": reason,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    record["record_hash"] = _canonical_hash(record)
    table.put_item(Item=record, ConditionExpression="attribute_not_exists(prediction_id)")
```

## Verifying the chain (post-incident or `/safety-audit`)

```python
def verify_chain(fire_id: str) -> bool:
    resp = table.query(
        IndexName="fire_id-written_at-index",
        KeyConditionExpression=Key("fire_id").eq(fire_id),
        ScanIndexForward=True,  # oldest first
    )
    expected_prev = GENESIS_HASH
    for item in resp["Items"]:
        if item["prev_hash"] != expected_prev:
            return False
        recomputed = _canonical_hash({k: v for k, v in item.items() if k != "record_hash"})
        if recomputed != item["record_hash"]:
            return False
        expected_prev = item["record_hash"]
    return True
```

## Step Functions — Confidence gate integration

The safety gate Lambda must return one of:
```python
return {"action": "APPROVED", "prediction_id": pid, "advisory": advisory}
# or
return {"action": "HUMAN_REVIEW_REQUIRED", "prediction_id": pid}
```

Step Functions checks `$.action` to route to the correct path.

## SageMaker Clarify — Bias audit pattern

```python
from sagemaker import clarify

clarify_processor = clarify.SageMakerClarifyProcessor(
    role=role,
    instance_count=1,
    instance_type="ml.m5.xlarge",
    sagemaker_session=session,
)

bias_config = clarify.BiasConfig(
    label_values_or_threshold=[1],          # 1 = fast response dispatched
    facet_name="income_bracket",            # the sensitive feature
    facet_values_or_threshold=[0],          # 0 = low income
)

clarify_processor.run_bias(
    data_config=data_config,
    bias_config=bias_config,
    model_config=model_config,
    pre_training_methods="all",
    post_training_methods="all",
)
```

## Model Monitor — Drift detection pattern

```python
from sagemaker.model_monitor import DefaultModelMonitor, CronExpressionGenerator

monitor = DefaultModelMonitor(
    role=role,
    instance_count=1,
    instance_type="ml.m5.xlarge",
    volume_size_in_gb=20,
    max_runtime_in_seconds=3600,
)

monitor.suggest_baseline(
    baseline_dataset=f"s3://{bucket}/baseline/train.csv",
    dataset_format=DatasetFormat.csv(header=True),
)

monitor.create_monitoring_schedule(
    monitor_schedule_name="wildfire-watch-monitor",
    endpoint_input=predictor.endpoint_name,
    output_s3_uri=f"s3://{bucket}/monitor-output/",
    statistics=monitor.baseline_statistics(),
    constraints=monitor.suggested_constraints(),
    schedule_cron_expression=CronExpressionGenerator.hourly(),
)
```
