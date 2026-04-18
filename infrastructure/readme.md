Provisions a Python 3.11 Lambda pointing at functions/scraper/firms_poller.py
Reads the FIRMS map key from SSM Parameter Store at /wildfire-watch/firms-map-key (you'll need to set this once: aws ssm put-parameter --name /wildfire-watch/firms-map-key --value <your_key> --type SecureString)
Sets WW_KINESIS_STREAM_NAME from the CoreStack's fire stream
Grants kinesis:PutRecord to the Lambda via fire_stream.grant_write()
Creates an EventBridge rate(3 hours) schedule targeting the Lambda