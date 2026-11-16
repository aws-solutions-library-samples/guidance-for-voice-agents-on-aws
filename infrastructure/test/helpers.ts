/**
 * Shared test helpers -- SSM context and config used across test files.
 *
 * Each test file synthesizes only the stacks it needs. This avoids
 * redundant synthesis while allowing Jest to parallelize across files.
 */
import * as cdk from 'aws-cdk-lib';
import { VoiceAgentConfig } from '../src/config';
import { SSM_PARAMS } from '../src/ssm-parameters';

export const TEST_CONFIG: VoiceAgentConfig = {
  environment: 'poc',
  region: 'us-east-1',
  projectName: 'test-voice-agent',
  vpcCidr: '10.0.0.0/16',
  maxAzs: 3,
  natGateways: 2,
  minCapacity: 1,
  maxCapacity: 12,
  targetSessionsPerTask: 3,
  sessionCapacityPerTask: 10,
};

export const TEST_ENV: cdk.Environment = {
  account: '123456789012',
  region: 'us-east-1',
};

/**
 * SSM context entries needed by stacks that call valueFromLookup().
 * Pass this to `new cdk.App({ context: { ...ECS_SSM_CONTEXT } })`.
 */
export const ECS_SSM_CONTEXT: Record<string, string> = {
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.VPC_ID}:region=${TEST_ENV.region}`]:
    'vpc-12345678',
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.PRIVATE_SUBNET_IDS}:region=${TEST_ENV.region}`]:
    'subnet-1,subnet-2',
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.API_KEY_SECRET_ARN}:region=${TEST_ENV.region}`]:
    'arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret',
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.ENCRYPTION_KEY_ARN}:region=${TEST_ENV.region}`]:
    'arn:aws:kms:us-east-1:123456789012:key/test-key',
};

export const BOTRUNNER_SSM_CONTEXT: Record<string, string> = {
  ...ECS_SSM_CONTEXT,
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.WEBHOOK_SECRET_ARN}:region=${TEST_ENV.region}`]:
    'arn:aws:secretsmanager:us-east-1:123456789012:secret:test-webhook-secret',
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.LAMBDA_SG_ID}:region=${TEST_ENV.region}`]:
    'sg-lambda12345',
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.ECS_CLUSTER_ARN}:region=${TEST_ENV.region}`]:
    'arn:aws:ecs:us-east-1:123456789012:cluster/test-cluster',
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.ECS_TASK_DEFINITION_ARN}:region=${TEST_ENV.region}`]:
    'arn:aws:ecs:us-east-1:123456789012:task-definition/test-task:1',
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.ECS_TASK_SG_ID}:region=${TEST_ENV.region}`]:
    'sg-ecs12345',
};

export const AGENT_SSM_CONTEXT: Record<string, string> = {
  [`ssm:account=${TEST_ENV.account}:parameterName=${SSM_PARAMS.VPC_ID}:region=${TEST_ENV.region}`]:
    'vpc-12345678',
};

export const DEPENDENCY_SSM_CONTEXT: Record<string, string> = {
  'voice-agent:environment': 'poc',
  'voice-agent:region': 'us-east-1',
  'voice-agent:projectName': 'test-voice-agent',
  'voice-agent:vpcCidr': '10.0.0.0/16',
  'voice-agent:maxAzs': '3',
  'voice-agent:natGateways': '2',
  ...ECS_SSM_CONTEXT,
};
