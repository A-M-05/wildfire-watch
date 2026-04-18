# Infra Agent

**Owns:** CDK stacks, all AWS provisioning
**Issues:** #1, #2, #3, #4, #5

## Responsibilities

- Write and deploy all CDK stacks in `infrastructure/stacks/`
- Provision every AWS resource before other agents need it
- Output ARNs and resource names as CDK exports so other stacks can import them
- Keep `CLAUDE.md` environment variable list in sync with what's actually provisioned

## Stack layout

```
infrastructure/
├── app.py                         ← CDK app entry point
└── stacks/
    ├── core_stack.py              ← Kinesis, IoT Core, DynamoDB, Timestream (#1)
    ├── ml_stack.py                ← SageMaker, S3, Glue (#2)
    ├── safety_stack.py            ← QLDB, Step Functions (#3)
    ├── messaging_stack.py         ← SNS, Pinpoint, SES (#4)
    └── frontend_stack.py          ← Amplify, API Gateway, Cognito (#5)
```

## Conventions

- All resource names prefixed with `wildfire-watch-`
- All CDK exports use pattern: `WildfireWatch::<StackName>::<ResourceName>`
- Tags on every resource: `Project=wildfire-watch`, `Env=hackathon`
- Use `RemovalPolicy.DESTROY` for the hackathon (we're not protecting prod data)

## Issue #1 checklist

- [ ] Kinesis stream: `wildfire-watch-fire-events` (2 shards)
- [ ] DynamoDB tables: `fires`, `resources`, `residents`, `alerts` (PAY_PER_REQUEST)
- [ ] Timestream DB: `wildfire-watch` + table `fire-metrics`
- [ ] IoT Core: thing type + policy for sensor feeds
- [ ] EventBridge rule skeleton (logic added in #10)

## Issue #2 checklist

- [ ] S3 bucket: `wildfire-watch-ml-data`
- [ ] SageMaker execution role with S3 + Kinesis read
- [ ] Glue database: `wildfire_watch`
- [ ] SageMaker Model Registry config

## Issue #3 checklist

- [ ] QLDB ledger: `wildfire-watch-audit`
- [ ] QLDB tables: `predictions`, `alerts`
- [ ] Step Functions state machine skeleton (logic added in #19)
- [ ] IAM role for Step Functions → Lambda invocations

## Issue #4 checklist

- [ ] SNS topic: `wildfire-watch-alerts`
- [ ] Pinpoint app: `wildfire-watch`
- [ ] Pinpoint SMS channel enabled
- [ ] SES identity verified (for dispatcher email fallback)

## Issue #5 checklist

- [ ] Cognito user pool: `wildfire-watch-users` (residents)
- [ ] Cognito user pool: `wildfire-watch-dispatchers` (fire department staff)
- [ ] API Gateway REST API + WebSocket API
- [ ] Amplify app wired to `frontend/` directory

## Verification

After deploying each stack:
```bash
aws cloudformation describe-stacks --stack-name WildfireWatchCore --query 'Stacks[0].StackStatus'
# Should return: "CREATE_COMPLETE"
```
