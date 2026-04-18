import aws_cdk as cdk
from stacks.core_stack import CoreStack
from stacks.scraper_stack import ScraperStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-west-2",
)

core = CoreStack(app, "WildfireWatchCore",
    env=env,
    description="Wildfire Watch — core data infrastructure (issue #1)",
)

ScraperStack(app, "WildfireWatchScraper",
    env=env,
    fire_stream=core.fire_stream,
    description="Wildfire Watch — FIRMS poller Lambda + EventBridge schedule (issue #6)",
)

app.synth()
