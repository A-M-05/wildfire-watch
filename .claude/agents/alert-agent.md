# Alert Agent

**Owns:** SMS alert sender, resident registration, watershed alerts
**Issues:** #22 (alert sender), #23 (registration), #24 (watershed alerts)

## Responsibilities

- Send SMS alerts to registered residents via Pinpoint, segmented by GPS radius
- Build the resident registration flow (Cognito + DynamoDB + location)
- Send watershed contamination alerts via Comprehend + Bedrock + Pinpoint

## File layout

```
functions/alert/
├── sender.py              ← Issue #22: Pinpoint SMS sender
├── register.py            ← Issue #23: resident registration handler
├── watershed_alert.py     ← Issue #24: watershed contamination alert
└── requirements.txt
```

## Issue #22 — Alert sender

Triggered by Step Functions (after safety gate approves).

```python
def send_alert(fire_event, advisory, prediction_id):
    pinpoint = boto3.client('pinpoint')

    # Get residents in risk radius from DynamoDB
    residents = get_residents_in_radius(
        lat=fire_event['lat'],
        lon=fire_event['lon'],
        radius_km=fire_event['risk_radius_km']
    )

    # Send to each resident
    for resident in residents:
        pinpoint.send_messages(
            ApplicationId=os.environ['WW_PINPOINT_APP_ID'],
            MessageRequest={
                'Addresses': {resident['phone']: {'ChannelType': 'SMS'}},
                'MessageConfiguration': {
                    'SMSMessage': {
                        'Body': advisory['sms'],
                        'MessageType': 'TRANSACTIONAL'
                    }
                }
            }
        )

    # Update QLDB — alert sent
    mark_alert_sent(prediction_id, alert_id=fire_event['fire_id'])
```

**NEVER log phone numbers to CloudWatch.** Log resident count only.

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

## Issue #24 — Watershed contamination alert

Triggered when USGS detects elevated turbidity or flow anomaly near a fire perimeter.

Flow:
1. USGS poller detects anomaly at a monitoring site
2. Pull EPA TRI data for chemical sites within 10km of that site
3. Feed to Comprehend to extract threat entities from news/scanner feeds
4. Generate advisory via Bedrock (same prompt template, different context)
5. Pass through safety gate (#21)
6. Send to residents downstream of the watershed

## Verification

```bash
# Test alert sender with a dry run (doesn't actually send SMS)
WW_DRY_RUN=true python functions/alert/sender.py \
  --fire-id test-001 \
  --lat 34.2 \
  --lon -118.5 \
  --radius-km 10

# Should print: "Would send to N residents" without calling Pinpoint
```
