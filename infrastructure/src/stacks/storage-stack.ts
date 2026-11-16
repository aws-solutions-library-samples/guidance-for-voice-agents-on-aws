import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { VoiceAgentConfig } from '../config';
import { SecretsConstruct } from '../constructs';

/**
 * Props for StorageStack
 */
export interface StorageStackProps extends cdk.StackProps {
  readonly config: VoiceAgentConfig;
}

/**
 * Storage infrastructure stack.
 * Thin wrapper that delegates to SecretsConstruct.
 *
 * Creates Secrets Manager secrets for API keys with KMS encryption.
 */
export class StorageStack extends cdk.Stack {
  /** Secrets construct containing KMS and Secrets Manager resources */
  public readonly secretsConstruct: SecretsConstruct;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Delegate to SecretsConstruct
    this.secretsConstruct = new SecretsConstruct(this, 'Secrets', {
      environment: config.environment,
      projectName: config.projectName,
    });

    // CloudFormation outputs (for console visibility)
    new cdk.CfnOutput(this, 'ApiKeySecretArn', {
      value: this.secretsConstruct.apiKeySecret.secretArn,
      description: 'API Keys Secret ARN',
    });

    new cdk.CfnOutput(this, 'WebhookSecretArn', {
      value: this.secretsConstruct.webhookSecret.secretArn,
      description: 'Webhook Authentication Secret ARN',
    });
  }
}
