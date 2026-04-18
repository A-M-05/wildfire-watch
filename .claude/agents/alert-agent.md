# Alert Agent

**Owns:** SMS alert sender, resident registration, watershed alerts
**Issues:** #22 (alert sender), #23 (registration), #24 (watershed alerts)

## Responsibilities

- Send SMS alerts to registered residents via `sns.publish(PhoneNumber=...)`, filtered by GPS radius in this Lambda (Pinpoint is unavailable on this account — SCP blocks `mobiletargeting:CreateApp`)
- Build the resident registration flow (Cognito + DynamoDB + location)
- Send watershed/reservoir water-level evacuation alerts via USGS + Bedrock + SNS

## File layout

```
functions/alert/
├── sender.py              ← Issue #22: Pinpoint SMS sender
├── register.py            ← Issue #23: resident registration handler
├── watershed_alert.py     ← Issue #24: watershed/reservoir water-level evacuation alert
└── requirements.txt
```

## Issue #22 — Alert sender

Triggered by Step Functions (after safety gate approves).

```python
def send_alert(fire_event, advisory, prediction_id):
    sns = boto3.client('sns')

    # Get residents in risk radius from DynamoDB
    residents = get_residents_in_radius(
        lat=fire_event['lat'],
        lon=fire_event['lon'],
        radius_km=fire_event['risk_radius_km'],
    )

    # Per-resident SMS — direct publish (no topic). Lambda execution role
    # needs `sns:Publish` on `*`.
    for resident in residents:
        sns.publish(
            PhoneNumber=resident['phone'],
            Message=advisory['sms'],
            MessageAttributes={
                'AWS.SNS.SMS.SMSType': {
                    'DataType': 'String',
                    'StringValue': 'Transactional',
                },
            },
        )

    # Append "alert_sent" event-row to the audit hash-chain (see ai-safety SKILL.md)
    mark_alert_sent(fire_event['fire_id'], prediction_id, alert_id=fire_event['fire_id'])
```

**NEVER log phone numbers to CloudWatch.** Log resident count only.

**Cost guardrail:** `sns.publish(PhoneNumber=...)` costs ~$0.00645 per US SMS. Set `WW_DRY_RUN=true` for local testing to skip the publish call.

## Issue #23 — Resident registration

API Gateway endpoint: `POST /residents/register`

```python
def register(event, context):
    body = json.loads(event['body'])
    # Validate via Cognito token
    user = verify_cognito_token(event['headers']['Authorization'])

    # Geocode address → lat/lon via Location Service
    location = geocode_address(body['address'])

    # Store in DynamoDB
    dynamodb.put_item(
        TableName=os.environ['WW_DYNAMODB_RESIDENTS_TABLE'],
        Item={
            'user_id': user['sub'],
            'phone': body['phone'],  # encrypted at rest
            'lat': Decimal(str(location['lat'])),
            'lon': Decimal(str(location['lon'])),
            'alert_radius_km': Decimal('10'),
            'registered_at': datetime.utcnow().isoformat()
        }
    )
```

## Issue #24 — Watershed/reservoir water-level evacuation alert

Triggered when USGS detects anomalous gauge height, discharge, or reservoir storage change on a watershed/reservoir near an active fire — signals downstream evacuation risk (dam stress, reservoir drawdown for firefighting, post-burn flood potential).

Flow:
1. USGS water-services poller reads gauge height (`00065`), discharge (`00060`), reservoir storage (`00054`) for sites near active fires
2. Level/flow anomaly detector flags drops/spikes outside historical baseline
3. Identify downstream threatened areas + population at risk
4. Generate advisory via Bedrock (threatened areas + evacuation guidance)
5. Pass through safety gate (#21)
6. Send to residents in threatened zones via `sns.publish(PhoneNumber=...)`

The earlier contamination-risk variant (EPA TRI + Comprehend) is backlogged — levels/flow give a direct evacuation signal aligned with the 60-second fire-to-SMS demo.

## Verification

```bash
# Test alert sender with a dry run (doesn't actually send SMS)
WW_DRY_RUN=true python functions/alert/sender.py \
  --fire-id test-001 \
  --lat 34.2 \
  --lon -118.5 \
  --radius-km 10

# Should print: "Would send to N residents" without calling sns.publish
```
