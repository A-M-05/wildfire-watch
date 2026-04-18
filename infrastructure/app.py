import aws_cdk as cdk
from stacks.core_stack import CoreStack
from stacks.safety_stack import SafetyStack
from stacks.messaging_stack import MessagingStack
from stacks.frontend_stack import FrontendStack
from stacks.scraper_stack import ScraperStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-west-2",
)

core = CoreStack(app, "WildfireWatchCore",
    env=env,
    description="Wildfire Watch - core data infrastructure (issue #1)",
)

SafetyStack(app, "WildfireWatchSafety",
    env=env,
    description="Wildfire Watch - DynamoDB audit hash-chain + Step Functions safety workflow (issue #3)",
)

MessagingStack(app, "WildfireWatchMessaging",
    env=env,
    description="Wildfire Watch - SNS broadcast topic + SES dispatcher identity (issue #4)",
)

FrontendStack(app, "WildfireWatchFrontend",
    env=env,
    description="Wildfire Watch - Cognito, API Gateway, Amplify (issue #5)",
)

ScraperStack(app, "WildfireWatchScraper",
    env=env,
    fire_stream=core.fire_stream,
    fires_table=core.fires_table,
    description="Wildfire Watch - FIRMS + CAL FIRE poller Lambdas + EventBridge schedules (issues #6, #7)",
)

app.synth()
