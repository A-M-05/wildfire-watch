import aws_cdk as cdk
from stacks.core_stack import CoreStack
from stacks.safety_stack import SafetyStack
from stacks.messaging_stack import MessagingStack
from stacks.frontend_stack import FrontendStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-west-2",
)

CoreStack(app, "WildfireWatchCore",
    env=env,
    description="Wildfire Watch — core data infrastructure (issue #1)",
)

SafetyStack(app, "WildfireWatchSafety",
    env=env,
    description="Wildfire Watch — QLDB ledger + Step Functions safety workflow (issue #3)",
)

MessagingStack(app, "WildfireWatchMessaging",
    env=env,
    description="Wildfire Watch — SNS, Pinpoint, SES (issue #4)",
)

FrontendStack(app, "WildfireWatchFrontend",
    env=env,
    description="Wildfire Watch — Cognito, API Gateway, Amplify (issue #5)",
)

app.synth()
