# Hagrid - Slack Access Request Bot

Hagrid is an AI-powered Slack bot for Okta access management. It provides a conversational interface for requesting access to Okta groups with configurable approval workflows.

## Architecture

Five Lambda functions handle the end-to-end access request flow:

- **event-handler** (145 lines): Validates Slack webhooks and routes events
- **conversation-manager** (356 lines): AI-powered intent detection using Google Gemini, manages conversation state in DynamoDB
- **approval-manager** (644 lines): Handles approval workflows with 6 approval types (NONE, MANUAL, MANAGER, ACCOUNT_ID, ACCOUNT_EMAIL, BOTH), sends interactive Slack DMs
- **okta-provisioner** (97 lines): Provisions users to Okta groups upon approval
- **catalog-builder** (271 lines): Daily sync of Okta groups to S3 (JSON + text formats)

**Data Flow**: Slack → event-handler → conversation-manager → approval-manager → okta-provisioner
**Data Stores**: DynamoDB (conversations, requests, approvals), S3 (catalog), SSM (secrets)

## Deployment

GitHub Actions deploys to AWS on pushes to `main`:
- Path-based deployment (only changed functions)
- OIDC authentication (no long-lived credentials)
- Automated packaging and `aws lambda update-function-code`

## TODOs: Code Refactoring & Improvements

### High Priority - Code Reuse
- [ ] **Create shared library structure** (`common/` directory)
  - Extract SSM parameter caching (duplicated 8+ times across 4 functions)
  - Create `SlackClient` class (consolidate ~150 lines of urllib boilerplate)
  - Create `CatalogLoader` for S3 catalog fetching (duplicated in 2 functions)
  - Add `LambdaClient` wrapper for invocations with proper error handling
- [ ] **Implement Lambda Layers** for shared code and dependencies
  - Share common utilities across all functions
  - Reduce deployment package sizes
  - Ensure consistent dependency versions
- [ ] **Centralize configuration management**
  - Create `Config` class with validation
  - Replace scattered `os.environ.get()` calls (15+ env vars)
  - Use environment variables for Lambda function names (currently hardcoded)
  - Use environment variables for DynamoDB table names (currently hardcoded)

### High Priority - Error Handling & Reliability
- [ ] **Add retry logic with exponential backoff**
  - Slack API calls (no retry logic currently)
  - S3 operations
  - DynamoDB operations
- [ ] **Implement consistent error handling**
  - Create custom exception types
  - Add error handling decorator pattern
  - Standardize error responses across functions
- [ ] **Add Slack API rate limit handling**
  - Currently no rate limit detection or backoff

### Medium Priority - Code Quality
- [ ] **Refactor `process_new_request()` in approval-manager** (lines 369-458, 90 lines)
  - Extract strategy pattern for 6 approval types
  - Reduce nested conditionals (29-line if/elif chain)
  - Separate business logic from API calls
- [ ] **Refactor `handle_approval_response()` in approval-manager** (lines 509-578, 70 lines)
  - Consider state machine or command pattern
  - Simplify state validation logic
- [ ] **Fix naming inconsistency**: `get_requester_email()` is used for both requesters and approvers (approval-manager:108-122)
  - Rename to `get_user_email(user_id)`
- [ ] **Remove or implement TODO comments** in approval-manager
  - Line 18: MANAGER approval type implementation
  - Line 21: BOTH approval type implementation
  - Lines 397, 401: Implementation stubs
- [ ] **Remove commented code** in conversation-manager (lines 214-233)
  - 20 lines of old "grounded" message approach
- [ ] **Remove magic comment** in conversation-manager (line 17)
  - "Refresh Cache Line -- modify this and push to refresh lambda catalog"

### Medium Priority - Performance
- [ ] **Implement caching improvements**
  - Cache Slack user list (currently fetches entire list on each approver lookup)
  - Consider ElastiCache or DynamoDB for catalog caching (longer TTL than container lifetime)
- [ ] **Batch DynamoDB operations** where possible
  - Replace multiple individual operations with batch_write_item
- [ ] **Parallelize approval DM sending** in approval-manager (lines 466-496)
  - Use ThreadPoolExecutor for concurrent Slack API calls

### Low Priority - Standardization
- [ ] **Standardize on `requests` library**
  - Currently: 4 functions use urllib, 1 uses requests
  - `requests` is more Pythonic and less error-prone
- [ ] **Add comprehensive type hints** throughout codebase
- [ ] **Add input validation decorators** or pydantic models
  - Repeated validation patterns in all lambda_handler functions
- [ ] **Document empty requirements.txt files**
  - 4 out of 5 functions have empty requirements
  - Add `requests` for consistency

### Architecture Considerations
- [ ] **Consider AWS Step Functions** for complex approval workflows
  - Visual workflow representation
  - Built-in error handling and retries
- [ ] **Consider EventBridge** for loose coupling
  - Replace hardcoded Lambda function names
  - Event-driven architecture with pub/sub pattern
- [ ] **Consider SQS for resilience**
  - Currently async Lambda invocations are fire-and-forget
  - SQS provides automatic retries and dead letter queues

### Security Hardening
- [ ] **Branch Protection**: Enable required reviews and deployment gates for `main` branch
- [ ] **Code Signing**: Implement AWS Lambda code signing for integrity verification
- [ ] **Artifact Verification**: Add checksum verification for deployment packages
- [ ] **Audit Logging**: CloudTrail monitoring for function invocations and permission changes
- [ ] **Multi-Account Strategy**: Separate AWS accounts for dev/staging/prod

## Related Repositories

- Infrastructure/Terraform: https://github.com/tavern-labs/hagrid-aws-terraform
