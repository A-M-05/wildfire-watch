from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    aws_cognito as cognito,
    aws_apigateway as apigw,
    aws_apigatewayv2 as apigwv2,
    aws_amplify as amplify,
)
from constructs import Construct

TAGS = {"Project": "wildfire-watch", "Env": "hackathon"}


class FrontendStack(Stack):
    """Issue #5 — Cognito user pools, API Gateway (REST + WebSocket), Amplify app.

    Unblocks all frontend issues (#25-#30) and resident registration (#23).

    Notes:
      * Two Cognito pools — residents (self-signup) and dispatchers (admin-created).
      * REST API has no resources yet — Lambda integrations land in their own
        feature issues. WebSocket API gives the map a push channel for live
        DynamoDB stream updates (wired in #30).
      * Amplify app is a shell — frontend dev connects the GitHub repo and
        sets the build spec from the Amplify console (or `amplify push` from
        the frontend dir). Doing it in CDK requires a GitHub OAuth token in
        Secrets Manager, which is overkill for the hackathon.
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        for k, v in TAGS.items():
            Tags.of(self).add(k, v)

        self._provision_cognito()
        self._provision_rest_api()
        self._provision_websocket_api()
        self._provision_amplify()

    # ------------------------------------------------------------------
    # Cognito — resident + dispatcher pools
    # ------------------------------------------------------------------

    def _provision_cognito(self):
        # Residents — self sign-up via email + phone for SMS alerts
        self.residents_pool = cognito.UserPool(
            self, "ResidentsPool",
            user_pool_name="wildfire-watch-users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True, phone=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True, phone=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False),
                phone_number=cognito.StandardAttribute(required=True, mutable=True),
                address=cognito.StandardAttribute(required=False, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_digits=True,
                require_symbols=False,
                require_uppercase=False,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.residents_client = self.residents_pool.add_client(
            "ResidentsWebClient",
            user_pool_client_name="wildfire-watch-residents-web",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,  # browser client
        )

        # Dispatchers — fire department staff, admin-created only
        self.dispatchers_pool = cognito.UserPool(
            self, "DispatchersPool",
            user_pool_name="wildfire-watch-dispatchers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False),
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.dispatchers_client = self.dispatchers_pool.add_client(
            "DispatchersWebClient",
            user_pool_client_name="wildfire-watch-dispatchers-web",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
        )

        for logical_id, value, env_var in [
            ("ResidentsPoolId", self.residents_pool.user_pool_id, "WW_COGNITO_RESIDENTS_POOL_ID"),
            ("ResidentsClientId", self.residents_client.user_pool_client_id, "WW_COGNITO_RESIDENTS_CLIENT_ID"),
            ("DispatchersPoolId", self.dispatchers_pool.user_pool_id, "WW_COGNITO_DISPATCHERS_POOL_ID"),
            ("DispatchersClientId", self.dispatchers_client.user_pool_client_id, "WW_COGNITO_DISPATCHERS_CLIENT_ID"),
        ]:
            CfnOutput(self, logical_id,
                value=value,
                export_name=f"WildfireWatch::Frontend::{logical_id}",
                description=f"Env var: {env_var}",
            )

    # ------------------------------------------------------------------
    # REST API — Cognito-authenticated, resources added per-feature
    # ------------------------------------------------------------------

    def _provision_rest_api(self):
        self.rest_api = apigw.RestApi(
            self, "RestApi",
            rest_api_name="wildfire-watch-api",
            description="REST API for wildfire-watch — resources added in feature stacks",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                throttling_burst_limit=100,
                throttling_rate_limit=50,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
            ),
        )

        # Feature stacks that need Cognito auth create their own authorizer
        # referencing the exported pool ID — authorizers must be attached to a
        # route at creation time, which this stack doesn't have yet.

        CfnOutput(self, "RestApiUrl",
            value=self.rest_api.url,
            export_name="WildfireWatch::Frontend::RestApiUrl",
            description="Env var: WW_API_GATEWAY_URL",
        )
        CfnOutput(self, "RestApiId",
            value=self.rest_api.rest_api_id,
            export_name="WildfireWatch::Frontend::RestApiId",
        )

    # ------------------------------------------------------------------
    # WebSocket API — live map updates (route integrations wired in #30)
    # ------------------------------------------------------------------

    def _provision_websocket_api(self):
        self.ws_api = apigwv2.CfnApi(
            self, "WebSocketApi",
            name="wildfire-watch-ws",
            protocol_type="WEBSOCKET",
            route_selection_expression="$request.body.action",
            description="WebSocket channel for live fire/perimeter pushes — routes wired in #30",
        )

        self.ws_stage = apigwv2.CfnStage(
            self, "WebSocketStage",
            api_id=self.ws_api.ref,
            stage_name="prod",
            auto_deploy=True,
        )
        self.ws_stage.add_dependency(self.ws_api)

        ws_url = f"wss://{self.ws_api.ref}.execute-api.{self.region}.amazonaws.com/prod"
        CfnOutput(self, "WebSocketUrl",
            value=ws_url,
            export_name="WildfireWatch::Frontend::WebSocketUrl",
            description="Env var: WW_WEBSOCKET_URL",
        )

    # ------------------------------------------------------------------
    # Amplify — hosting shell (connect repo from console)
    # ------------------------------------------------------------------

    def _provision_amplify(self):
        self.amplify_app = amplify.CfnApp(
            self, "AmplifyApp",
            name="wildfire-watch-frontend",
            description="React + Mapbox frontend — connect GitHub repo from Amplify console",
            platform="WEB",
            tags=[{"key": k, "value": v} for k, v in TAGS.items()],
        )

        CfnOutput(self, "AmplifyAppId",
            value=self.amplify_app.attr_app_id,
            export_name="WildfireWatch::Frontend::AmplifyAppId",
        )
