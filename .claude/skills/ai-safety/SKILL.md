# Skill: AI Safety (QLDB + Guardrails + Clarify + Model Monitor)

Read this before writing any safety-related code. These patterns are non-negotiable.

## QLDB — The hard rule

**QLDB write must complete before any downstream action (SMS, Step Functions, etc.).**
If QLDB throws, catch the exception and halt — do not proceed to alerting.

```python
from pyqldb.driver.qldb_driver import QldbDriver
from amazon.ion.simple_types import IonPyNull

driver = QldbDriver(ledger_name=os.environ['WW_QLDB_LEDGER'])

def log_prediction(fire_id: str, recommendation: dict, advisory: dict) -> str:
    def write_txn(txn):
        cursor = txn.execute_statement(
            "INSERT INTO predictions ?",
            {
                'fire_id': fire_id,
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'recommendation': recommendation,
                'advisory_text': advisory.get('brief', ''),
                'sms_text': advisory.get('sms', ''),
                'confidence': recommendation['confidence'],
                'guardrails_passed': None,
                'alert_sent': False,
                'blocked_reason': None
            }
        )
        return list(cursor)[0]

    result = driver.execute_lambda(write_txn)
    return str(result['documentId'])  # return doc ID for later reference
```

## Bedrock Guardrails — Validation pattern

```python
bedrock = boto3.client('bedrock-runtime')

def validate_advisory(text: str, confidence: float) -> dict:
    response = bedrock.apply_guardrail(
        guardrailIdentifier=os.environ['WW_BEDROCK_GUARDRAIL_ID'],
        guardrailVersion='DRAFT',
        source='OUTPUT',
        content=[{'text': {'text': text}}]
    )
    passed = response['action'] == 'NONE'
    return {
        'passed': passed,
        'action': response['action'],
        'blocked_reason': response.get('outputs', [{}])[0].get('text') if not passed else None
    }
```

After validation, update QLDB:
```python
def update_guardrail_result(doc_id: str, passed: bool, reason: str = None):
    driver.execute_lambda(
        lambda txn: txn.execute_statement(
            "UPDATE predictions SET guardrails_passed = ?, blocked_reason = ? WHERE documentId = ?",
            passed, reason, doc_id
        )
    )
```

## Step Functions — Confidence gate integration

The safety gate Lambda must return one of:
```python
return {'action': 'APPROVED', 'prediction_id': doc_id, 'advisory': advisory}
# or
return {'action': 'HUMAN_REVIEW_REQUIRED', 'prediction_id': doc_id}
```

Step Functions checks `$.action` to route to the correct path.

## SageMaker Clarify — Bias audit pattern

```python
from sagemaker import clarify

clarify_processor = clarify.SageMakerClarifyProcessor(
    role=role,
    instance_count=1,
    instance_type='ml.m5.xlarge',
    sagemaker_session=session
)

bias_config = clarify.BiasConfig(
    label_values_or_threshold=[1],          # 1 = fast response dispatched
    facet_name='income_bracket',            # the sensitive feature
    facet_values_or_threshold=[0]           # 0 = low income
)

clarify_processor.run_bias(
    data_config=data_config,
    bias_config=bias_config,
    model_config=model_config,
    pre_training_methods='all',
    post_training_methods='all'
)
```

## Model Monitor — Drift detection pattern

```python
from sagemaker.model_monitor import DefaultModelMonitor, CronExpressionGenerator

monitor = DefaultModelMonitor(
    role=role,
    instance_count=1,
    instance_type='ml.m5.xlarge',
    volume_size_in_gb=20,
    max_runtime_in_seconds=3600
)

monitor.suggest_baseline(
    baseline_dataset=f's3://{bucket}/baseline/train.csv',
    dataset_format=DatasetFormat.csv(header=True)
)

monitor.create_monitoring_schedule(
    monitor_schedule_name='wildfire-watch-monitor',
    endpoint_input=predictor.endpoint_name,
    output_s3_uri=f's3://{bucket}/monitor-output/',
    statistics=monitor.baseline_statistics(),
    constraints=monitor.suggested_constraints(),
    schedule_cron_expression=CronExpressionGenerator.hourly()
)
```
