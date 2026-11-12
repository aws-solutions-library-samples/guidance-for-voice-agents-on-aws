import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { NetworkStack } from '../src/stacks/network-stack';
import { StorageStack } from '../src/stacks/storage-stack';
import { SSM_PARAMS } from '../src/ssm-parameters';
import { TEST_CONFIG, TEST_ENV } from './helpers';

describe('NetworkStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new NetworkStack(app, 'TestNetworkStack', {
      env: TEST_ENV,
      config: TEST_CONFIG,
    });
    template = Template.fromStack(stack);
  });

  it('should create a VPC', () => {
    template.resourceCountIs('AWS::EC2::VPC', 1);
  });

  it('should create VPC with correct CIDR', () => {
    template.hasResourceProperties('AWS::EC2::VPC', {
      CidrBlock: '10.0.0.0/16',
    });
  });

  it('should create NAT Gateways', () => {
    template.resourceCountIs('AWS::EC2::NatGateway', 2);
  });

  it('should create public, private, and isolated subnets', () => {
    // 3 AZs * 3 subnet types = 9 subnets
    template.resourceCountIs('AWS::EC2::Subnet', 9);
  });

  it('should create security groups', () => {
    // SageMaker SG + Lambda SG + VPC Endpoint SG
    template.resourceCountIs('AWS::EC2::SecurityGroup', 3);
  });

  it('should create SSM parameters for VPC ID', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.VPC_ID,
      Type: 'String',
    });
  });

  it('should create SSM parameters for subnet IDs', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.PRIVATE_SUBNET_IDS,
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.ISOLATED_SUBNET_IDS,
    });
  });

  it('should create SSM parameters for security group IDs', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.SAGEMAKER_SG_ID,
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.LAMBDA_SG_ID,
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.VPC_ENDPOINT_SG_ID,
    });
  });

  it('should create S3 Gateway endpoint', () => {
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      ServiceName: Match.objectLike({
        'Fn::Join': Match.arrayWith([Match.arrayWith([Match.stringLikeRegexp('s3')])]),
      }),
      VpcEndpointType: 'Gateway',
    });
  });

  it('should create SageMaker Runtime interface endpoint', () => {
    // PrivateDnsEnabled is true — both standard InvokeEndpoint (443) and
    // BiDi streaming (HTTP/2 on 8443) work through the VPC endpoint.
    // Port 8443 ingress is configured on the VPC endpoint SG in ecs-stack.
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      ServiceName: Match.stringLikeRegexp('sagemaker\\.runtime'),
      VpcEndpointType: 'Interface',
      PrivateDnsEnabled: true,
    });
  });

  it('should create Secrets Manager interface endpoint', () => {
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      ServiceName: Match.stringLikeRegexp('secretsmanager'),
      VpcEndpointType: 'Interface',
      PrivateDnsEnabled: true,
    });
  });

  it('should create Bedrock Runtime interface endpoint', () => {
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      ServiceName: Match.stringLikeRegexp('bedrock-runtime'),
      VpcEndpointType: 'Interface',
      PrivateDnsEnabled: true,
    });
  });

  it('should create CloudWatch Logs interface endpoint', () => {
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      ServiceName: Match.stringLikeRegexp('\\.logs$'),
      VpcEndpointType: 'Interface',
      PrivateDnsEnabled: true,
    });
  });

  it('should output VPC ID', () => {
    template.hasOutput('VpcId', {});
  });
});

describe('StorageStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new StorageStack(app, 'TestStorageStack', {
      env: TEST_ENV,
      config: TEST_CONFIG,
    });
    template = Template.fromStack(stack);
  });

  it('should create a KMS key', () => {
    template.resourceCountIs('AWS::KMS::Key', 1);
  });

  it('should create KMS key with rotation enabled', () => {
    template.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  it('should create API key and webhook Secrets Manager secrets', () => {
    template.resourceCountIs('AWS::SecretsManager::Secret', 2);
  });

  it('should generate the SIP webhook secret and leave the Daily HMAC as a placeholder', () => {
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      GenerateSecretString: Match.objectLike({
        GenerateStringKey: 'SIP_WEBHOOK_SECRET',
        SecretStringTemplate: JSON.stringify({
          DAILY_HMAC_SECRET: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
        }),
      }),
    });
  });

  it('should create SSM parameter for webhook secret ARN', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.WEBHOOK_SECRET_ARN,
    });
  });

  it('should create SSM parameter for secret ARN', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.API_KEY_SECRET_ARN,
    });
  });

  it('should create SSM parameter for encryption key ARN', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.ENCRYPTION_KEY_ARN,
    });
  });

  it('should output secret ARN', () => {
    template.hasOutput('ApiKeySecretArn', {});
  });
});

describe('SSM Parameter Names', () => {
  it('should have unique parameter names', () => {
    const paramValues = Object.values(SSM_PARAMS);
    const uniqueValues = new Set(paramValues);
    expect(uniqueValues.size).toBe(paramValues.length);
  });

  it('should follow naming convention', () => {
    const paramValues = Object.values(SSM_PARAMS);
    for (const param of paramValues) {
      expect(param).toMatch(/^\/voice-agent\//);
    }
  });

  it('should include Knowledge Base parameters', () => {
    expect(SSM_PARAMS.KNOWLEDGE_BASE_ID).toBeDefined();
    expect(SSM_PARAMS.KNOWLEDGE_BASE_ARN).toBeDefined();
    expect(SSM_PARAMS.KNOWLEDGE_BASE_BUCKET).toBeDefined();
  });

  it('should include A2A namespace parameters', () => {
    expect(SSM_PARAMS.A2A_NAMESPACE_ID).toBeDefined();
    expect(SSM_PARAMS.A2A_NAMESPACE_NAME).toBeDefined();
    expect(SSM_PARAMS.A2A_NAMESPACE_ID).toBe('/voice-agent/a2a/namespace-id');
    expect(SSM_PARAMS.A2A_NAMESPACE_NAME).toBe('/voice-agent/a2a/namespace-name');
  });
});
