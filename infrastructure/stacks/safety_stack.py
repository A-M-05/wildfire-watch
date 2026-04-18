from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    Duration,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_bedrock as bedrock,
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
        self._provision_guardrails()
        self._provision_step_functions_role()
        self._provision_dispatcher_notify_lambda()
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
    # Bedrock Guardrails — advisory content safety filter (issue #16)
    # ------------------------------------------------------------------

    def _provision_guardrails(self):
        # Guardrails is a managed filter between our Bedrock call and the response.
        # Every advisory passes through it before reaching any Lambda or resident.
        # Rules here must stay in sync with FALSE_CERTAINTY_PHRASES in guardrails.py.
        self.guardrail = bedrock.CfnGuardrail(
            self, "AdvisoryGuardrail",
            name="wildfire-watch-advisory",
            description="Blocks false-certainty advisories and strips PII from evacuation alerts",
            blocked_input_messaging="This input cannot be processed for safety reasons.",
            blocked_outputs_messaging=(
                "This advisory has been blocked for safety reasons. "
                "A human dispatcher will issue guidance shortly."
            ),
            # Word policy — block phrases that imply false certainty about resident safety.
            # These are the phrases most likely to cause harm in a real emergency.
            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                words_config=[
                    bedrock.CfnGuardrail.WordConfigProperty(text="you are definitely safe"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="you are safe"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="no danger"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="no risk"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="all clear"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="completely safe"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="no threat"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="nothing to worry"),
                ],
            ),
            # PII policy — anonymize phone numbers, addresses, and names in advisory text.
            # Residents' contact info must never appear in AI-generated output (CLAUDE.md rule #4).
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="PHONE", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="ADDRESS", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="NAME", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="EMAIL", action="ANONYMIZE"),
                ],
            ),
            # Content filters — block hate/violence at MEDIUM strength.
            # MEDIUM catches explicit content without over-blocking legitimate
            # emergency language like "fire is threatening" or "danger zone".
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="HATE", input_strength="MEDIUM", output_strength="MEDIUM"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="VIOLENCE", input_strength="LOW", output_strength="MEDIUM"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="INSULTS", input_strength="MEDIUM", output_strength="MEDIUM"
                    ),
                ],
            ),
        )

        CfnOutput(self, "GuardrailId",
            value=self.guardrail.attr_guardrail_id,
            export_name="WildfireWatch::Safety::GuardrailId",
            description="Env var: WW_BEDROCK_GUARDRAIL_ID",
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
    # Dispatcher notification Lambda (issue #19)
    # ------------------------------------------------------------------

    def _provision_dispatcher_notify_lambda(self):
        self.dispatcher_notify_fn = lambda_.Function(
            self, "DispatcherNotifyLambda",
            function_name="wildfire-watch-dispatcher-notify",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="dispatcher_notify.handler",
            code=lambda_.Code.from_asset("functions/safety"),
            timeout=Duration.seconds(30),
            environment={
                "WW_SNS_ALERT_TOPIC_ARN": "",   # set post-deploy from MessagingStack output
                "WW_DYNAMODB_FIRES_TABLE": "fires",
                "WW_CONFIDENCE_THRESHOLD": "0.65",
            },
        )

        # Allow publishing to the dispatcher SNS topic.
        self.dispatcher_notify_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sns:Publish"],
            resources=[f"arn:aws:sns:{self.region}:{self.account}:wildfire-watch-*"],
        ))

        # Allow updating the fires table to store the pending review task token.
        self.dispatcher_notify_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/fires"],
        ))

    # ------------------------------------------------------------------
    # Step Functions state machine — confidence gate (issue #19)
    # ------------------------------------------------------------------

    def _provision_state_machine(self):
        """Build the full safety workflow state machine.

        Flow (Guardrails run on ALL paths — CLAUDE.md safety rule #2):
          SafetyGate (LambdaInvoke — runs Bedrock advisory + Guardrails + audit write)
            → ActionRouter (Choice on $.Payload.action)
                 ├─ APPROVED               → AlertSender
                 ├─ HUMAN_REVIEW_REQUIRED  → NotifyDispatcherAndWait (WAIT_FOR_TASK_TOKEN, 5-min)
                 │                              ├─ approved → AlertSender
                 │                              └─ timeout  → EscalateAndAlert → AlertSender
                 └─ BLOCKED                → LogAndStop  (no SMS — advisory was unsafe)

        Guardrails run inside the safety gate Lambda (#21) before the action is
        determined, so PII stripping and false-certainty blocking apply to every
        advisory — including low-confidence ones headed for human review.
        """

        # Safety gate Lambda (#21) — the single choke point for every advisory.
        # Returns: { "action": "APPROVED"|"HUMAN_REVIEW_REQUIRED"|"BLOCKED",
        #            "prediction_id": str, "advisory": str, ... }
        # Imported by name so this stack can deploy before #21 is merged to main.
        safety_gate_fn = lambda_.Function.from_function_name(
            self, "SafetyGateFn",
            function_name="wildfire-watch-safety-gate",
        )

        safety_gate = tasks.LambdaInvoke(
            self, "SafetyGate",
            lambda_function=safety_gate_fn,
            payload=sfn.TaskInput.from_object({
                "fire_event": sfn.JsonPath.string_at("$.fire_event"),
                "recommendation": sfn.JsonPath.string_at("$.recommendation"),
            }),
            result_path="$.safety_gate_result",
        )

        # Terminal states — alert sender is a placeholder until #22 is wired.
        alert_sender = sfn.Pass(
            self, "AlertSender",
            comment="Replaced by alert sender LambdaInvoke in #22",
        )

        # Blocked — advisory failed Guardrails; no SMS, no human override allowed.
        log_and_stop = sfn.Pass(
            self, "LogAndStop",
            comment="Advisory blocked by Guardrails — no alert dispatched",
            parameters={
                "blocked": True,
                "prediction_id.$": "$.safety_gate_result.Payload.prediction_id",
                "fire_event.$": "$.fire_event",
            },
        )

        # Auto-escalate when dispatcher doesn't respond within 5 minutes.
        escalate_and_alert = sfn.Pass(
            self, "EscalateAndAlert",
            comment="Heartbeat timeout — auto-escalate: dispatch without human approval",
            parameters={
                "escalated": True,
                "reason": "Dispatcher did not respond within 5 minutes — auto-escalated",
                "fire_event.$": "$$.Execution.Input.fire_event",
            },
        ).next(alert_sender)

        # Human-in-the-loop gate — pauses until sfn:SendTaskSuccess/Failure.
        # The dispatcher_notify Lambda sends the task token to the dispatcher via SNS
        # and stores it in DynamoDB so the UI can resume without the CLI.
        notify_and_wait = tasks.LambdaInvoke(
            self, "NotifyDispatcherAndWait",
            lambda_function=self.dispatcher_notify_fn,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({
                "task_token": sfn.JsonPath.task_token,
                "fire_event": sfn.JsonPath.string_at("$.fire_event"),
                "recommendation": sfn.JsonPath.string_at("$.recommendation"),
                # Pass the Guardrails-validated advisory so the dispatcher sees
                # the same text that would be sent if they approve.
                "advisory": sfn.JsonPath.string_at("$.safety_gate_result.Payload.advisory"),
            }),
            heartbeat=Duration.minutes(5),
            result_path="$.approval_result",
        )
        notify_and_wait.add_catch(
            escalate_and_alert,
            errors=["States.HeartbeatTimeout", "States.TaskFailed", "States.ALL"],
            result_path="$.error",
        )
        notify_and_wait.next(alert_sender)

        # Route on the safety gate's action — three mutually exclusive paths.
        action_router = sfn.Choice(self, "ActionRouter") \
            .when(
                sfn.Condition.string_equals("$.safety_gate_result.Payload.action", "APPROVED"),
                alert_sender,
            ) \
            .when(
                sfn.Condition.string_equals("$.safety_gate_result.Payload.action", "HUMAN_REVIEW_REQUIRED"),
                notify_and_wait,
            ) \
            .otherwise(log_and_stop)  # BLOCKED or any unexpected value → stop

        self.state_machine = sfn.StateMachine(
            self, "SafetyStateMachine",
            state_machine_name="wildfire-watch-safety",
            definition_body=sfn.DefinitionBody.from_chainable(safety_gate.next(action_router)),
            role=self.sfn_role,
            timeout=Duration.minutes(15),
            comment="Safety workflow: Guardrails gate → APPROVED/HUMAN_REVIEW/BLOCKED routing",
        )

        # Allow Step Functions to invoke both Lambdas.
        safety_gate_fn.grant_invoke(self.sfn_role)
        self.dispatcher_notify_fn.grant_invoke(self.sfn_role)

        CfnOutput(self, "SafetyStateMachineArn",
            value=self.state_machine.state_machine_arn,
            export_name="WildfireWatch::Safety::StateMachineArn",
            description="Env var: WW_STEP_FUNCTIONS_ARN",
        )
