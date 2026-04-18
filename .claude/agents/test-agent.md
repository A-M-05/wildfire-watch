# Test Agent

**Owns:** Unit tests, integration tests, safety contract tests
**Issues:** #31 (unit tests), #32 (integration + safety contract)

## Responsibilities

- Unit test every Lambda handler
- Write the safety contract test that verifies QLDB is written before any alert fires
- Set up CI with GitHub Actions

## File layout

```
tests/
├── unit/
│   ├── test_ingest.py          ← normalize Lambda
│   ├── test_enrich.py          ← enrichment Lambda
│   ├── test_dispatch.py        ← EventBridge trigger
│   ├── test_alert_sender.py    ← Pinpoint sender
│   └── test_safety_gate.py     ← safety gate Lambda
├── integration/
│   └── test_safety_contract.py ← Issue #32: the critical contract test
├── fixtures/
│   ├── firms_raw.json          ← sample NASA FIRMS payload
│   ├── calfire_raw.json        ← sample CAL FIRE GeoJSON
│   ├── enriched_event.json     ← sample enriched fire event
│   └── advisory_response.json  ← sample Bedrock advisory
└── conftest.py                 ← shared fixtures, mock AWS clients
```

## Issue #31 — Unit tests

Use `moto` to mock AWS calls. Do not hit real AWS in unit tests.

```python
# tests/unit/test_ingest.py
from moto import mock_dynamodb, mock_kinesis
from functions.ingest.handler import normalize, handler

@mock_dynamodb
@mock_kinesis
def test_normalize_firms_record():
    raw = load_fixture('firms_raw.json')
    result = normalize(raw)
    assert result['source'] == 'FIRMS'
    assert 'fire_id' in result
    assert isinstance(result['lat'], float)
    assert isinstance(result['confidence'], float)

def test_normalize_preserves_schema():
    """Every field in CLAUDE.md fire event schema must be present."""
    raw = load_fixture('firms_raw.json')
    result = normalize(raw)
    required_fields = ['fire_id', 'source', 'lat', 'lon', 'detected_at', 'confidence']
    for field in required_fields:
        assert field in result, f"Missing field: {field}"
```

## Issue #32 — Safety contract test

This is the most important test in the repo. It verifies the hard rule from CLAUDE.md:
**QLDB must be written before any alert fires.**

```python
# tests/integration/test_safety_contract.py

def test_qldb_written_before_alert():
    """
    Contract: a QLDB prediction record must exist and have alert_sent=False
    at the moment the alert sender Lambda is invoked.
    """
    qldb_writes = []
    pinpoint_sends = []

    # Instrument both calls with timestamps
    with patch('functions.alert.safety_gate.log_prediction') as mock_log, \
         patch('functions.alert.sender.pinpoint_client.send_messages') as mock_send:

        mock_log.side_effect = lambda *args, **kwargs: qldb_writes.append(time.time())
        mock_send.side_effect = lambda *args, **kwargs: pinpoint_sends.append(time.time())

        # Trigger full pipeline with test fire event
        trigger_test_fire_event()

    assert len(qldb_writes) > 0, "QLDB was never written"
    assert len(pinpoint_sends) > 0, "No SMS was sent"
    assert qldb_writes[0] < pinpoint_sends[0], \
        f"QLDB write ({qldb_writes[0]}) must precede SMS send ({pinpoint_sends[0]})"

def test_guardrails_blocks_false_certainty():
    """Advisory claiming 'you are definitely safe' must be blocked."""
    advisory = "You are definitely safe. No need to evacuate."
    result = validate_advisory(advisory, confidence=0.3)
    assert not result['passed']
```

## CI setup (.github/workflows/ci.yml)

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements-dev.txt
      - run: python -m pytest tests/unit/ -v
      - run: python -m pytest tests/integration/ -v --timeout=30
```
