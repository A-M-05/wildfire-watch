# /safety-audit — Full AI safety checklist

Usage: `/safety-audit`

## What this command does

Verifies all four safety mechanisms are wired correctly before any demo or deploy.

## Checklist

### 1. QLDB — Immutable audit trail
- [ ] QLDB ledger `wildfire-watch-audit` exists and is ACTIVE
  ```bash
  aws qldb describe-ledger --name wildfire-watch-audit --query 'State'
  ```
- [ ] `predictions` and `alerts` tables exist in the ledger
- [ ] Safety contract test passes (QLDB written before alert):
  ```bash
  python -m pytest tests/integration/test_safety_contract.py::test_qldb_written_before_alert -v
  ```
- [ ] `mark_alert_sent()` is called after Pinpoint confirms delivery

### 2. Bedrock Guardrails — Advisory validation
- [ ] Guardrail ID is set in `WW_BEDROCK_GUARDRAIL_ID` env var
- [ ] Guardrails test passes (false certainty is blocked):
  ```bash
  python -m pytest tests/integration/test_safety_contract.py::test_guardrails_blocks_false_certainty -v
  ```
- [ ] Advisory containing "you are definitely safe" returns `action: GUARDRAIL_INTERVENED`
- [ ] Blocked advisories are logged to QLDB with `blocked_reason`

### 3. Human-in-the-loop gate — Confidence threshold
- [ ] Step Functions state machine exists and is ACTIVE
  ```bash
  aws stepfunctions describe-state-machine --state-machine-arn $WW_STEP_FUNCTIONS_ARN --query 'status'
  ```
- [ ] Low-confidence event (< 0.65) triggers human review path
  ```bash
  python tests/integration/trigger_low_confidence_event.py
  # Should show state machine execution in WAITING state
  ```
- [ ] `WW_CONFIDENCE_THRESHOLD` is set to 0.65 (do not lower without team consensus)

### 4. SageMaker Clarify — Bias audit
- [ ] Clarify bias report exists in S3:
  ```bash
  aws s3 ls s3://wildfire-watch-ml-data/clarify-output/
  ```
- [ ] No group shows >15% slower recommended response time
- [ ] Audit date is within the last 7 days

### 5. Model Monitor — Distribution shift detection
- [ ] Monitoring schedule is ACTIVE:
  ```bash
  aws sagemaker list-monitoring-schedules --query 'MonitoringScheduleSummaries[?MonitoringScheduleStatus==`Scheduled`]'
  ```
- [ ] CloudWatch alarm exists for divergence > 0.3

## Output

Report pass/fail for each item. Any FAIL blocks demo deployment.
