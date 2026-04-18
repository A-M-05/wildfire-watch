from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    aws_sns as sns,
    aws_ses as ses,
)
from constructs import Construct

TAGS = {"Project": "wildfire-watch", "Env": "hackathon"}

SES_DISPATCHER_IDENTITY = "spammyrobot@gmail.com"


class MessagingStack(Stack):
    """Issue #4 — SNS topic + SES identity (Pinpoint dropped, see notes).

    Unblocks alert sender (#22), resident registration (#23), watershed alert (#24).

    Notes:
      * Pinpoint was originally planned for native GPS-radius SMS targeting,
        but this account's SCP blocks `mobiletargeting:CreateApp`. The alert
        sender Lambda (#22) instead queries the residents DynamoDB table for
        residents within radius and calls `sns.publish(PhoneNumber=...)` per
        resident. This needs the IAM permission `sns:Publish` on `*` (no
        topic ARN), which #22 will add to its execution role.
      * SES identity starts in the sandbox — only verified recipients can
        receive. Fine for the demo (we'll verify our own emails).
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        self._provision_sns()
        self._provision_ses()

    # ------------------------------------------------------------------
    # SNS — broadcast alert topic (per-resident SMS goes via sns.publish PhoneNumber direct)
    # ------------------------------------------------------------------

    def _provision_sns(self):
        self.alert_topic = sns.Topic(
            self, "AlertTopic",
            topic_name="wildfire-watch-alerts",
            display_name="Wildfire Watch Alerts",
        )
        self.alert_topic.apply_removal_policy(RemovalPolicy.DESTROY)
        for k, v in TAGS.items():
            Tags.of(self.alert_topic).add(k, v)

        CfnOutput(self, "AlertTopicArn",
            value=self.alert_topic.topic_arn,
            export_name="WildfireWatch::Messaging::AlertTopicArn",
            description="Env var: WW_SNS_ALERT_TOPIC_ARN",
        )

    # ------------------------------------------------------------------
    # SES — dispatcher email fallback
    # ------------------------------------------------------------------

    def _provision_ses(self):
        self.ses_identity = ses.EmailIdentity(
            self, "DispatcherIdentity",
            identity=ses.Identity.email(SES_DISPATCHER_IDENTITY),
        )
        self.ses_identity.apply_removal_policy(RemovalPolicy.DESTROY)
        for k, v in TAGS.items():
            Tags.of(self.ses_identity).add(k, v)

        CfnOutput(self, "SesDispatcherIdentity",
            value=SES_DISPATCHER_IDENTITY,
            export_name="WildfireWatch::Messaging::SesDispatcherIdentity",
            description="Verify this address in the SES console before sending",
        )
