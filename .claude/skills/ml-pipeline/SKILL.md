# Skill: ML Pipeline (SageMaker + Bedrock)

Read this before writing any SageMaker training script, endpoint invocation, or Bedrock call.

## SageMaker training script pattern

```python
# ml/dispatch_model/train.py
import argparse, os, json
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split

def train():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-depth', type=int, default=6)
    parser.add_argument('--n-estimators', type=int, default=100)
    parser.add_argument('--model-dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--train', type=str, default=os.environ['SM_CHANNEL_TRAIN'])
    args = parser.parse_args()

    df = pd.read_csv(os.path.join(args.train, 'train.csv'))
    X = df.drop('label', axis=1)
    y = df['label']

    model = xgb.XGBClassifier(max_depth=args.max_depth, n_estimators=args.n_estimators)
    model.fit(X, y)
    model.save_model(os.path.join(args.model_dir, 'model.xgb'))

if __name__ == '__main__':
    train()
```

## SageMaker endpoint invocation pattern

```python
import boto3, json

sm_runtime = boto3.client('sagemaker-runtime')

def get_dispatch_recommendation(features: list) -> dict:
    response = sm_runtime.invoke_endpoint(
        EndpointName=os.environ['WW_SAGEMAKER_ENDPOINT'],
        ContentType='application/json',
        Accept='application/json',
        Body=json.dumps({'features': features})
    )
    result = json.loads(response['Body'].read())
    return {
        'recommendation': result['prediction'],
        'confidence': result['probability']
    }
```

## Bedrock advisory generation pattern

```python
import boto3, json

bedrock = boto3.client('bedrock-runtime', region_name='us-east-1')

def generate_advisory(fire_event: dict, recommendation: dict) -> dict:
    prompt = ADVISORY_PROMPT.format(**fire_event, **recommendation)

    response = bedrock.invoke_model(
        modelId='anthropic.claude-sonnet-4-6-20241022-v2:0',
        contentType='application/json',
        accept='application/json',
        body=json.dumps({
            'anthropic_version': 'bedrock-2023-05-31',
            'max_tokens': 512,
            'messages': [{'role': 'user', 'content': prompt}]
        })
    )

    result = json.loads(response['body'].read())
    advisory = json.loads(result['content'][0]['text'])
    return advisory  # {'sms': '...', 'brief': '...'}
```

## Prompt caching (use for advisory prompt — it's long and stable)

```python
body=json.dumps({
    'anthropic_version': 'bedrock-2023-05-31',
    'max_tokens': 512,
    'system': [
        {
            'type': 'text',
            'text': SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'}  # cache the stable system prompt
        }
    ],
    'messages': [{'role': 'user', 'content': prompt}]  # only the dynamic part varies
})
```

## Feature vector for dispatch model

Order must match training data:
```python
features = [
    fire_event['lat'],
    fire_event['lon'],
    fire_event['spread_rate_km2_per_hr'],
    fire_event['population_at_risk'],
    fire_event['nearest_stations'][0]['distance_km'],
    fire_event['wind_speed_ms'],
    fire_event['radiative_power'],
    hour_of_day,
    is_weekend
]
```
