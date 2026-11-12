import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { VoiceAgentConfig } from '../config';
import { SSM_PARAMS } from '../ssm-parameters';
import { WebhookApiConstruct } from '../constructs';

/**
 * Props for BotRunnerStack
 */
export interface BotRunnerStackProps extends cdk.StackProps {
  readonly config: VoiceAgentConfig;
}

/**
 * Bot Runner infrastructure stack.
 * Thin wrapper that delegates to WebhookApiConstruct.
 *
 * Creates Lambda function and API Gateway for Daily webhooks.
 * Reads all dependencies from SSM Parameters.
 */
export class BotRunnerStack extends cdk.Stack {
  /** Webhook API construct containing Lambda and API Gateway */
  public readonly webhookConstruct: WebhookApiConstruct;

  constructor(scope: Construct, id: string, props: BotRunnerStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Read dependencies from SSM Parameters
    const vpcId = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.VPC_ID);
    const lambdaSgId = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.LAMBDA_SG_ID);
    const privateSubnetIds = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.PRIVATE_SUBNET_IDS
    );
    const apiKeySecretArn = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.API_KEY_SECRET_ARN
    );
    const webhookSecretArn = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.WEBHOOK_SECRET_ARN
    );
    const encryptionKeyArn = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.ENCRYPTION_KEY_ARN
    );
    // ECS parameters
    const ecsClusterArn = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.ECS_CLUSTER_ARN);
    const ecsTaskDefinitionArn = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.ECS_TASK_DEFINITION_ARN
    );
    const ecsTaskSecurityGroupId = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.ECS_TASK_SG_ID
    );
    const ecsServiceEndpoint = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.ECS_SERVICE_ENDPOINT
    );

    // Import VPC and security group
    const vpc = ec2.Vpc.fromLookup(this, 'ImportedVpc', { vpcId });
    const lambdaSg = ec2.SecurityGroup.fromSecurityGroupId(this, 'ImportedLambdaSG', lambdaSgId);

    // Delegate to WebhookApiConstruct
    this.webhookConstruct = new WebhookApiConstruct(this, 'WebhookApi', {
      environment: config.environment,
      projectName: config.projectName,
      vpc,
      lambdaSecurityGroup: lambdaSg,
      apiKeySecretArn,
      webhookSecretArn,
      encryptionKeyArn,
      ecsClusterArn,
      ecsTaskDefinitionArn,
      ecsTaskSecurityGroupId,
      ecsServiceEndpoint,
      privateSubnetIds,
    });

    // CloudFormation outputs (for console visibility)
    new cdk.CfnOutput(this, 'WebhookEndpoint', {
      value: this.webhookConstruct.apiEndpoint,
      description: 'Daily webhook URL for pinless dial-in configuration',
    });
  }
}
