# /demo-prep — Pre-demo verification checklist

Usage: `/demo-prep`

## What this command does

Verifies the system is ready for the live demo. Runs through every dependency in the demo critical path and confirms the 5 pre-seeded scenarios are loaded.

## Checklist

### Infrastructure
- [ ] All 5 CDK stacks are CREATE_COMPLETE:
  ```bash
  for stack in WildfireWatchCore WildfireWatchML WildfireWatchSafety WildfireWatchMessaging WildfireWatchFrontend; do
    echo "$stack: $(aws cloudformation describe-stacks --stack-name $stack --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo 'NOT FOUND')"
  done
  ```

### Data pipeline
- [ ] Kinesis stream `wildfire-watch-fire-events` is ACTIVE
- [ ] Push test event and verify it lands in DynamoDB within 30s:
  ```bash
  python scripts/test_pipeline.py --fire-id demo-check-001
  ```

### ML
- [ ] SageMaker endpoint is InService:
  ```bash
  aws sagemaker describe-endpoint --endpoint-name wildfire-watch-dispatch --query 'EndpointStatus'
  ```
- [ ] Bedrock returns an advisory for the Thousand Oaks scenario:
  ```bash
  python ml/scripts/test_bedrock.py --scenario thousand-oaks
  ```

### Safety
- [ ] Run `/safety-audit` — all items must pass

### Demo scenarios
- [ ] 5 demo scenarios pre-seeded in DynamoDB:
  ```bash
  aws dynamodb get-item --table-name fires --key '{"fire_id": {"S": "demo-thousand-oaks"}}'
  ```
  Scenarios: `demo-thousand-oaks`, `demo-malibu`, `demo-inland-empire`, `demo-big-bear`, `demo-san-fernando`

### Frontend
- [ ] Map loads and shows CA with fire stations
- [ ] Clicking demo-thousand-oaks fire shows dispatch panel with advisory
- [ ] SafetyBadge shows confidence score and Guardrails status
- [ ] QLDB link in SafetyBadge opens audit record

### Alert
- [ ] Send a test SMS to your own phone:
  ```bash
  python scripts/test_alert.py --phone YOUR_PHONE --fire-id demo-thousand-oaks --dry-run false
  ```

## Demo trigger command

When ready to demo:
```bash
python scripts/trigger_demo.py --scenario thousand-oaks
# Pushes fire event → pipeline runs → map updates → SMS fires
# Target: SMS received within 60 seconds
```

## If anything fails

1. Check CloudWatch logs for the failing Lambda
2. Check Step Functions execution for stuck states
3. Check QLDB for the prediction record (helps diagnose where pipeline stopped)
4. If SageMaker endpoint is cold, invoke it once to warm it up before demo
