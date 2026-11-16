import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { SSM_PARAMS } from '../ssm-parameters';

/**
 * Props for SecretsConstruct
 */
export interface SecretsConstructProps {
  /** Deployment environment */
  environment: string;
  /** Project name for resource naming */
  projectName: string;
}

/**
 * Secrets infrastructure construct.
 * Creates Secrets Manager secrets for API keys with KMS encryption.
 *
 * Outputs are stored in SSM Parameters for cross-stack reference.
 */
export class SecretsConstruct extends Construct {
  /** Secret containing API keys */
  public readonly apiKeySecret: secretsmanager.ISecret;
  /** Secret used to authenticate requests to the webhook /start endpoint */
  public readonly webhookSecret: secretsmanager.ISecret;
  /** KMS key for secret encryption */
  public readonly encryptionKey: kms.IKey;

  constructor(scope: Construct, id: string, props: SecretsConstructProps) {
    super(scope, id);

    const isProd = props.environment === 'prod';

    // KMS key for encrypting secrets
    this.encryptionKey = new kms.Key(this, 'SecretsKey', {
      description: `KMS key for Voice Agent secrets encryption - ${props.environment}`,
      enableKeyRotation: true,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    // Secret for API keys (Daily, Deepgram, Cartesia)
    // Actual values populated manually after deployment
    this.apiKeySecret = new secretsmanager.Secret(this, 'ApiKeySecret', {
      description: `API keys for external services (Daily, Deepgram, Cartesia) - ${props.environment}`,
      encryptionKey: this.encryptionKey,
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          DAILY_API_KEY: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
          DEEPGRAM_API_KEY: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
          CARTESIA_API_KEY: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
        }),
        generateStringKey: 'generated_field', // Required but unused
      },
    });

    // Secret for authenticating webhook /start requests (read only by the bot runner Lambda)
    // - DAILY_HMAC_SECRET: the `hmac` from Daily's pinless_dialin config, set after deployment
    // - SIP_WEBHOOK_SECRET: shared secret SIP clients send in X-Sip-Webhook-Secret (generated)
    this.webhookSecret = new secretsmanager.Secret(this, 'WebhookSecret', {
      description: `Webhook authentication secrets (Daily HMAC, SIP shared secret) - ${props.environment}`,
      encryptionKey: this.encryptionKey,
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          DAILY_HMAC_SECRET: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
        }),
        generateStringKey: 'SIP_WEBHOOK_SECRET',
        excludePunctuation: true,
        passwordLength: 48,
      },
    });

    // Store outputs in SSM Parameters for cross-stack reference
    new ssm.StringParameter(this, 'ApiKeySecretArnParam', {
      parameterName: SSM_PARAMS.API_KEY_SECRET_ARN,
      stringValue: this.apiKeySecret.secretArn,
      description: 'Voice Agent API Keys Secret ARN',
    });

    new ssm.StringParameter(this, 'WebhookSecretArnParam', {
      parameterName: SSM_PARAMS.WEBHOOK_SECRET_ARN,
      stringValue: this.webhookSecret.secretArn,
      description: 'Voice Agent Webhook Authentication Secret ARN',
    });

    new ssm.StringParameter(this, 'EncryptionKeyArnParam', {
      parameterName: SSM_PARAMS.ENCRYPTION_KEY_ARN,
      stringValue: this.encryptionKey.keyArn,
      description: 'Voice Agent KMS Encryption Key ARN',
    });
  }
}
