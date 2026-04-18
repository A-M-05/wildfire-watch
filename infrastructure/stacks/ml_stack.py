from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    aws_s3 as s3,
    aws_iam as iam,
    aws_glue as glue,
    aws_sagemaker as sagemaker,
    aws_kinesis as kinesis,
)
from constructs import Construct

TAGS = {"Project": "wildfire-watch", "Env": "hackathon"}


class MLStack(Stack):
    """Issue #2 — S3 ML bucket, SageMaker execution role, Glue catalog, Model Registry.

    Everything the ML pipeline (issues #12, #13, #14) needs to exist before it
    can train or deploy. No Lambda code here — just the AWS resources that house
    the model's data and artifacts.
    """

    def __init__(self, scope: Construct, id: str, fire_stream: kinesis.Stream, **kwargs):
        super().__init__(scope, id, **kwargs)

        # fire_stream comes from CoreStack. We pass it in (not import via Fn.import_value)
        # because same-app cross-stack references as constructor params are cleaner —
        # CDK handles the dependency ordering automatically.
        self._fire_stream = fire_stream

        for k, v in TAGS.items():
            Tags.of(self).add(k, v)

        self._provision_s3()
        self._provision_sagemaker_role()
        self._provision_glue()
        self._provision_model_registry()

    # ------------------------------------------------------------------
    # S3 — training data + model artifacts
    # ------------------------------------------------------------------

    def _provision_s3(self):
        # One bucket holds everything: raw training CSVs, preprocessed feature
        # files, and the model.tar.gz artifact SageMaker produces after training.
        # Versioning is on so we don't accidentally overwrite a good training run.
        self.ml_bucket = s3.Bucket(
            self, "MLDataBucket",
            bucket_name="wildfire-watch-ml-data",
            versioned=True,                         # keep old model artifacts safe
            removal_policy=RemovalPolicy.DESTROY,   # hackathon: tear it all down cleanly
            auto_delete_objects=True,               # CDK custom resource to empty before delete
        )

        CfnOutput(self, "MLBucketName",
            value=self.ml_bucket.bucket_name,
            export_name="WildfireWatch::ML::MLBucketName",
            description="Env var: WW_ML_BUCKET",
        )
        CfnOutput(self, "MLBucketArn",
            value=self.ml_bucket.bucket_arn,
            export_name="WildfireWatch::ML::MLBucketArn",
        )

    # ------------------------------------------------------------------
    # IAM — SageMaker execution role
    # ------------------------------------------------------------------

    def _provision_sagemaker_role(self):
        # SageMaker needs a role it can "assume" when running training jobs
        # or hosting endpoints. The role must trust the sagemaker.amazonaws.com
        # principal — that's what the assume_role_policy (trust policy) does.
        self.sagemaker_role = iam.Role(
            self, "SageMakerExecutionRole",
            role_name="wildfire-watch-sagemaker-execution",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            # AmazonSageMakerFullAccess covers CloudWatch logging, ECR image pulls,
            # and most SageMaker APIs. We layer on specific S3/Kinesis grants below.
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
            ],
        )

        # Grant read + write to our ML bucket (training data in, model artifact out).
        self.ml_bucket.grant_read_write(self.sagemaker_role)

        # Grant Kinesis read so SageMaker can pull live fire events for online
        # feature engineering during inference (used in #13 endpoint logic).
        self._fire_stream.grant_read(self.sagemaker_role)

        # Also allow Glue access so the training script can query the catalog
        # for feature table schemas without hardcoding column names.
        self.sagemaker_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "glue:GetDatabase",
                "glue:GetTable",
                "glue:GetPartitions",
                "glue:BatchGetPartition",
            ],
            resources=["*"],  # Glue ARNs vary by region; wildcard is fine for hackathon
        ))

        CfnOutput(self, "SageMakerRoleArn",
            value=self.sagemaker_role.role_arn,
            export_name="WildfireWatch::ML::SageMakerRoleArn",
            description="Env var: WW_SAGEMAKER_ROLE_ARN — pass to training jobs and model deploys",
        )

    # ------------------------------------------------------------------
    # Glue — feature catalog
    # ------------------------------------------------------------------

    def _provision_glue(self):
        # The Glue Data Catalog is like a schema registry for S3 data.
        # Training script #12 will register a table here pointing at
        # s3://wildfire-watch-ml-data/features/ so Athena and SageMaker
        # can query it without knowing the raw S3 layout.
        self.glue_db = glue.CfnDatabase(
            self, "GlueDatabase",
            catalog_id=self.account,  # catalog_id is always the AWS account ID
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name="wildfire_watch",   # underscore required — Glue DB names are SQL identifiers
                description="Wildfire Watch ML feature catalog: fire events, enriched features, labels",
            ),
        )

        CfnOutput(self, "GlueDatabaseName",
            value="wildfire_watch",
            export_name="WildfireWatch::ML::GlueDatabaseName",
            description="Env var: WW_GLUE_DATABASE",
        )

    # ------------------------------------------------------------------
    # SageMaker Model Registry — versioned model history
    # ------------------------------------------------------------------

    def _provision_model_registry(self):
        # A ModelPackageGroup is a named container for all versions of a model.
        # Every training run in #12 will register a new ModelPackage (version)
        # inside this group with its S3 artifact location, metrics, and approval
        # status. Issue #13 deploys whichever version is "Approved".
        self.model_package_group = sagemaker.CfnModelPackageGroup(
            self, "DispatchModelGroup",
            model_package_group_name="wildfire-watch-dispatch",
            model_package_group_description=(
                "XGBoost dispatch recommendation model. "
                "Versions registered by training job (#12); "
                "approved version deployed as endpoint (#13)."
            ),
        )

        CfnOutput(self, "ModelPackageGroupName",
            value="wildfire-watch-dispatch",
            export_name="WildfireWatch::ML::ModelPackageGroupName",
            description="Env var: WW_MODEL_PACKAGE_GROUP — used by train (#12) and deploy (#13)",
        )
