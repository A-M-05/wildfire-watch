import aws_cdk as cdk
from stacks.core_stack import CoreStack
from stacks.safety_stack import SafetyStack

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

app.synth()
