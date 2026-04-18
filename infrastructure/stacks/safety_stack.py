from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    Duration,
    aws_qldb as qldb,
    aws_iam as iam,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from constructs import Construct

TAGS = {"Project": "wildfire-watch", "Env": "hackathon"}

LEDGER_NAME = "wildfire-watch-audit"


class SafetyStack(Stack):
    """Issue #3 — QLDB ledger + Step Functions safety workflow skeleton.

    Provisions:
      * QLDB ledger (`predictions` and `alerts` tables are created by the
        safety gate Lambda on first write — see issue #17).
      * Step Functions state machine skeleton with a confidence-gate Choice
        state. The Lambda tasks are wired in issue #19 / #21.
      * IAM role for Step Functions to invoke safety + alert Lambdas.
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        for k, v in TAGS.items():
            Tags.of(self).add(k, v)

        self._provision_qldb()
        self._provision_step_functions_role()
        self._provision_state_machine()

    # ------------------------------------------------------------------
    # QLDB
    # ------------------------------------------------------------------

    def _provision_qldb(self):
        self.ledger = qldb.CfnLedger(
            self, "AuditLedger",
            name=LEDGER_NAME,
            permissions_mode="STANDARD",
            deletion_protection=False,
            tags=[{"key": k, "value": v} for k, v in TAGS.items()],
        )
        self.ledger.apply_removal_policy(RemovalPolicy.DESTROY)

        CfnOutput(self, "QldbLedgerName",
            value=self.ledger.name,
            export_name="WildfireWatch::Safety::QldbLedgerName",
            description="Env var: WW_QLDB_LEDGER",
        )

    # ------------------------------------------------------------------
    # Step Functions IAM
    # ------------------------------------------------------------------

    def _provision_step_functions_role(self):
        self.sfn_role = iam.Role(
            self, "SafetyStateMachineRole",
            role_name="wildfire-watch-safety-sfn-role",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Step Functions role for safety workflow — invokes safety gate + alert Lambdas",
        )

        # Lambdas are added to this stack in #19 / #21. Granting invoke on all
        # project Lambdas now so cross-stack wiring doesn't require role edits.
        self.sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[
                f"arn:aws:lambda:{self.region}:{self.account}:function:wildfire-watch-*"
            ],
        ))

        # QLDB send-command for reading prediction records inside the workflow
        self.sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["qldb:SendCommand"],
            resources=[
                f"arn:aws:qldb:{self.region}:{self.account}:ledger/{LEDGER_NAME}"
            ],
        ))

        CfnOutput(self, "SafetyStateMachineRoleArn",
            value=self.sfn_role.role_arn,
            export_name="WildfireWatch::Safety::StateMachineRoleArn",
        )

    # ------------------------------------------------------------------
    # Step Functions state machine skeleton (logic wired in #19)
    # ------------------------------------------------------------------

    def _provision_state_machine(self):
        # Placeholder Pass states — replaced by LambdaInvoke tasks in #19 / #21.
        safety_gate_placeholder = sfn.Pass(
            self, "SafetyGatePlaceholder",
            comment="Replaced by safety gate Lambda in #21",
            result=sfn.Result.from_object({"action": "APPROVED", "confidence": 1.0}),
        )

        auto_approve = sfn.Pass(
            self, "AutoApprove",
            comment="Confidence >= 0.65 — proceed to alert sender (wired in #22)",
        )

        human_review = sfn.Pass(
            self, "HumanReviewRequired",
            comment="Confidence < 0.65 — pause for dispatcher approval (wired in #19)",
        )

        # Confidence gate — the non-negotiable rule from CLAUDE.md
        confidence_choice = sfn.Choice(self, "ConfidenceGate") \
            .when(
                sfn.Condition.number_greater_than_equals("$.confidence", 0.65),
                auto_approve,
            ) \
            .otherwise(human_review)

        definition = safety_gate_placeholder.next(confidence_choice)

        self.state_machine = sfn.StateMachine(
            self, "SafetyStateMachine",
            state_machine_name="wildfire-watch-safety",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            role=self.sfn_role,
            timeout=Duration.minutes(15),
            comment="Safety workflow skeleton — full logic in #19/#21",
        )

        CfnOutput(self, "SafetyStateMachineArn",
            value=self.state_machine.state_machine_arn,
            export_name="WildfireWatch::Safety::StateMachineArn",
            description="Env var: WW_STEP_FUNCTIONS_ARN",
        )
