from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    aws_sns as sns,
    aws_pinpoint as pinpoint,
    aws_ses as ses,
)
from constructs import Construct

TAGS = {"Project": "wildfire-watch", "Env": "hackathon"}

SES_DISPATCHER_IDENTITY = "dispatch@wildfire-watch.local"


class MessagingStack(Stack):
    """Issue #4 — SNS topic, Pinpoint app (SMS), SES identity.

    Unblocks alert sender (#22), resident registration (#23), watershed alert (#24).

    Notes:
      * Pinpoint's SMS channel must be enabled for two-way messaging from the
        console after first deploy (AWS CLI can't toggle it). See README below.
      * SES identity starts in the sandbox — only verified recipients can receive.
        Fine for hackathon demo (we'll verify our own phones/emails).
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        for k, v in TAGS.items():
            Tags.of(self).add(k, v)

        self._provision_sns()
        self._provision_pinpoint()
        self._provision_ses()

    # ------------------------------------------------------------------
    # SNS — broadcast alert topic
    # ------------------------------------------------------------------

    def _provision_sns(self):
        self.alert_topic = sns.Topic(
            self, "AlertTopic",
            topic_name="wildfire-watch-alerts",
            display_name="Wildfire Watch Alerts",
        )
        self.alert_topic.apply_removal_policy(RemovalPolicy.DESTROY)

        CfnOutput(self, "AlertTopicArn",
            value=self.alert_topic.topic_arn,
            export_name="WildfireWatch::Messaging::AlertTopicArn",
            description="Env var: WW_SNS_ALERT_TOPIC_ARN",
        )

    # ------------------------------------------------------------------
    # Pinpoint — SMS to residents by GPS radius
    # ------------------------------------------------------------------

    def _provision_pinpoint(self):
        self.pinpoint_app = pinpoint.CfnApp(
            self, "PinpointApp",
            name="wildfire-watch",
            tags=TAGS,
        )
        self.pinpoint_app.apply_removal_policy(RemovalPolicy.DESTROY)

        # SMS channel — must also be enabled in the Pinpoint console on first
        # deploy for two-way messaging + sender ID. CfnSMSChannel below turns
        # on outbound SMS.
        self.pinpoint_sms = pinpoint.CfnSMSChannel(
            self, "PinpointSmsChannel",
            application_id=self.pinpoint_app.ref,
            enabled=True,
        )
        self.pinpoint_sms.add_dependency(self.pinpoint_app)

        CfnOutput(self, "PinpointAppId",
            value=self.pinpoint_app.ref,
            export_name="WildfireWatch::Messaging::PinpointAppId",
            description="Env var: WW_PINPOINT_APP_ID",
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

        CfnOutput(self, "SesDispatcherIdentity",
            value=SES_DISPATCHER_IDENTITY,
            export_name="WildfireWatch::Messaging::SesDispatcherIdentity",
            description="Verify this address in the SES console before sending",
        )
