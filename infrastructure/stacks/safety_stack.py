from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    Duration,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_stepfunctions as sfn,
)
from constructs import Construct

TAGS = {"Project": "wildfire-watch", "Env": "hackathon"}

AUDIT_TABLE_NAME = "wildfire-watch-audit"


class SafetyStack(Stack):
    """Issue #3 — Audit ledger (DynamoDB hash-chain) + Step Functions safety workflow.

    Originally targeted QLDB; pivoted to DynamoDB because QLDB no longer accepts
    new ledger creations on this account. The "immutable audit" property is
    preserved by chaining each record's `prev_hash` into the next record's
    `record_hash` — the chain is verified server-side by the safety gate Lambda
    (#17) and any post-incident audit script.

    Provisions:
      * DynamoDB `wildfire-watch-audit` table (PK: prediction_id, SK: written_at)
        - Streams enabled (NEW_IMAGE) so a downstream verifier Lambda can re-hash
        - GSI `fire_id-written_at-index` to fetch all records for a fire
      * Step Functions skeleton with a confidence-gate Choice — Lambda tasks
        get wired in #19 / #21.
      * IAM role for Step Functions to invoke wildfire-watch-* Lambdas.
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        for k, v in TAGS.items():
            Tags.of(self).add(k, v)

        self._provision_audit_table()
        self._provision_step_functions_role()
        self._provision_state_machine()

    # ------------------------------------------------------------------
    # DynamoDB audit ledger (hash-chain — see SKILL.md / issue #17)
    # ------------------------------------------------------------------

    def _provision_audit_table(self):
        self.audit_table = dynamodb.Table(
            self, "AuditTable",
            table_name=AUDIT_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="prediction_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="written_at", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            stream=dynamodb.StreamViewType.NEW_IMAGE,
            point_in_time_recovery=True,
        )

        # Lookup all audit records for a given fire (used by post-incident review)
        self.audit_table.add_global_secondary_index(
            index_name="fire_id-written_at-index",
            partition_key=dynamodb.Attribute(name="fire_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="written_at", type=dynamodb.AttributeType.STRING),
        )

        CfnOutput(self, "AuditTableName",
            value=self.audit_table.table_name,
            export_name="WildfireWatch::Safety::AuditTableName",
            description="Env var: WW_AUDIT_TABLE",
        )
        CfnOutput(self, "AuditTableStreamArn",
            value=self.audit_table.table_stream_arn,
            export_name="WildfireWatch::Safety::AuditTableStreamArn",
        )

    # ------------------------------------------------------------------
    # Step Functions IAM
    # ------------------------------------------------------------------

    def _provision_step_functions_role(self):
        self.sfn_role = iam.Role(
            self, "SafetyStateMachineRole",
            role_name="wildfire-watch-safety-sfn-role",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Step Functions role - invokes wildfire-watch Lambdas and reads audit ledger",
        )

        self.sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[
                f"arn:aws:lambda:{self.region}:{self.account}:function:wildfire-watch-*"
            ],
        ))

        # Read-only on the audit ledger (the safety gate Lambda is the writer)
        self.audit_table.grant_read_data(self.sfn_role)

        CfnOutput(self, "SafetyStateMachineRoleArn",
            value=self.sfn_role.role_arn,
            export_name="WildfireWatch::Safety::StateMachineRoleArn",
        )

    # ------------------------------------------------------------------
    # Step Functions state machine skeleton (logic wired in #19)
    # ------------------------------------------------------------------

    def _provision_state_machine(self):
        # Replaced by LambdaInvoke tasks in #19 / #21.
        safety_gate_placeholder = sfn.Pass(
            self, "SafetyGatePlaceholder",
            comment="Replaced by safety gate Lambda in #21",
            result=sfn.Result.from_object({"action": "APPROVED", "confidence": 1.0}),
        )

        auto_approve = sfn.Pass(
            self, "AutoApprove",
            comment="confidence >= 0.65 — proceed to alert sender (wired in #22)",
        )

        human_review = sfn.Pass(
            self, "HumanReviewRequired",
            comment="confidence < 0.65 — pause for dispatcher approval (wired in #19)",
        )

        # Confidence gate — non-negotiable rule from CLAUDE.md
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
