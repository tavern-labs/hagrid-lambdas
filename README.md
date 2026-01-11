# Hagrid - Slack Access Request Bot

## Overview

Hagrid is an AI-powered Slack bot for Okta access management. It provides a conversational UI for requesting access to Okta groups and implements configurable approval workflows to streamline the access provisioning process.

## Architecture

The system consists of five Lambda functions that work together to handle access requests:

- **event-handler**: Slack webhook receiver that validates and routes incoming events
- **conversation-manager**: AI/NLP processing engine for intent detection and conversation flow
- **approval-manager**: Sends approval DMs to designated approvers and handles button responses
- **okta-provisioner**: Adds users to Okta groups upon approval
- **catalog-builder**: Syncs Okta group catalog daily for searchable access options

## Deployment

The project uses GitHub Actions for continuous deployment:

- **Trigger**: Pushes to the `main` branch
- **Path-based deployment**: Only changed functions are deployed
- **Security**: OIDC federation for AWS authentication (no long-lived credentials)
- **Process**: Each function's dependencies are installed, zipped with code, and deployed via `aws lambda update-function-code`

To deploy changes:
1. Make changes to a function in the `functions/` directory
2. Commit and push to `main`
3. GitHub Actions will automatically detect and deploy only the changed functions

## Local Development

### Prerequisites
- Python 3.12
- AWS CLI configured with appropriate credentials
- Access to Slack workspace for testing

### Testing Functions Locally
(Placeholder - local testing setup coming soon)

## Security Considerations (Production)

### Current Security Measures
- **OIDC Federation**: GitHub Actions uses OIDC to authenticate with AWS, eliminating the need for long-lived IAM credentials
- **Least-Privilege Deployment**: The deployment role only has `lambda:UpdateFunctionCode` permissions

### TODO: Additional Security Hardening
- **Branch Protection**: Enable required reviews and deployment gates for `main` branch
- **Code Signing**: Implement AWS Lambda code signing to ensure code integrity
- **Artifact Verification**: Add checksum verification for deployment packages
- **Runtime Secrets**: Pull sensitive credentials from AWS SSM Parameter Store or Secrets Manager at runtime (not environment variables)

### Future Considerations
- **Multi-Account Strategy**: Separate AWS accounts for dev/staging/prod environments
- **Immutable Infrastructure**: Deploy new Lambda versions rather than updating in-place
- **Audit Logging**: CloudTrail monitoring for all function invocations and permission changes

## Related Repositories

- Infrastructure/Terraform: (Link to be added)

## License

(To be determined)
