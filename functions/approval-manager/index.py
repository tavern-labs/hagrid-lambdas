"""
Approval Manager Lambda Function

Handles the approval workflow for access requests.

Responsibilities:
- Receive confirmed access requests from Conversation Manager
- Create request record in DynamoDB
- Determine approval requirements from catalog
- Send approval DMs to approvers with approve/deny buttons
- Handle button click responses
- Track approval status and threshold
- Invoke Okta Provisioner when approved
- Notify user of request status

Approval Types:
- NONE: Auto-approve, invoke Okta Provisioner immediately
- MANAGER: Send approval request to user's manager (TODO)
- ACCOUNT_ID: Send to designated approvers in catalog (same as MANUAL)
- MANUAL: Send to designated approvers in catalog
- BOTH: Requires manager AND designated approvers (TODO - currently only uses designated approvers)

Approval Logic:
- ALL: Requires all approvers to approve
- ANY: Requires threshold number of approvers
"""

import json
import logging
import os
import uuid
import boto3
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize clients outside handler
ssm = boto3.client('ssm')
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')

# Cache for SSM values
_slack_bot_token = None
_okta_catalog_data = None

# Table references
access_requests_table = dynamodb.Table(os.environ.get('ACCESS_REQUESTS_TABLE', 'hagrid-access-requests'))
approval_messages_table = dynamodb.Table(os.environ.get('APPROVAL_MESSAGES_TABLE', 'hagrid-approval-messages'))


# =============================================================================
# SSM GETTERS
# =============================================================================

def get_slack_bot_token():
    """Fetch and cache Slack bot token for sending DMs."""
    global _slack_bot_token
    if _slack_bot_token:
        return _slack_bot_token
    
    param_name = os.environ.get('SLACK_BOT_TOKEN_SSM', '/hagrid/slack-bot-token')
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _slack_bot_token = response['Parameter']['Value']
    return _slack_bot_token


def get_okta_catalog_data():
    """Fetch and cache Okta catalog from S3 (JSON format with group IDs and approvers)."""
    global _okta_catalog_data
    if _okta_catalog_data:
        return _okta_catalog_data

    bucket = os.environ.get('CATALOG_S3_BUCKET')
    key = 'catalog.json'

    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        _okta_catalog_data = json.loads(content)
        return _okta_catalog_data

    except Exception as e:
        logger.error(f"Error fetching catalog from S3 bucket {bucket}/{key}: {e}")
        return {}


def get_role_config(app_name, role_name):
    """Look up role configuration from catalog."""
    catalog = get_okta_catalog_data()
    
    for app in catalog.get('applications', []):
        if app['app_name'].lower() == app_name.lower():
            for role in app.get('roles', []):
                if role['role_name'].lower() == role_name.lower():
                    return role
    
    return None


# =============================================================================
# SLACK FUNCTIONS
# =============================================================================

def get_requester_email(user_id):
    """Look up requester's email from their Slack user ID."""
    token = get_slack_bot_token()
    url = f'https://slack.com/api/users.info?user={user_id}'
    
    try:
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('ok'):
                return result['user']['profile'].get('email')
    except Exception as e:
        logger.error(f"Error getting user email: {e}")
    
    return None


def get_approver_slack_id(email):
    """Look up approver's Slack user ID from their email."""
    token = get_slack_bot_token()
    url = f'https://slack.com/api/users.lookupByEmail?email={email}'
    
    try:
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('ok'):
                return result['user']['id']
    except Exception as e:
        logger.error(f"Error looking up user by email: {e}")
    
    return None

def send_slack_api(method, payload, url=None):
    """Universal helper. Uses method or specific URL and cached token."""
    # Use provided URL (for updates) or build the standard one (for new messages)
    api_url = url if url else f'https://slack.com/api/{method}'
    
    headers = {'Content-Type': 'application/json'}
    
    # Use cached token only for standard API calls
    if not url:
        headers['Authorization'] = f'Bearer {get_slack_bot_token()}'

    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(api_url, data=data, headers=headers)
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode('utf-8')
            try:
                return json.loads(res_body)
            except:
                return {'ok': res_body == 'ok'}
    except Exception as e:
        logger.error(f"Slack API Error: {e}")
        return {'ok': False}


def send_approval_dm(approver_slack_id, request_id, requester_email, app_name, role_name, role_description):
    """Send approval request DM with approve/deny buttons."""
    token = get_slack_bot_token()
    
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Access Request*\n\n*Requester:* {requester_email}\n*Application:* {app_name}\n*Role:* {role_name}\n*Description:* {role_description}"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✓ Approve"},
                    "style": "primary",
                    "action_id": "approve_request",
                    "value": request_id
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✗ Deny"},
                    "style": "danger",
                    "action_id": "deny_request",
                    "value": request_id
                }
            ]
        }
    ]
    
    payload = {
        'channel': approver_slack_id,
        'text': f'Access request from {requester_email} for {app_name} {role_name}',
        'blocks': blocks
    }
    
    result = send_slack_api('chat.postMessage', payload)
    return result.get('ts') if result.get('ok') else None
    
    return None


def send_slack_message(channel, text):
    """Send a simple text message to Slack."""
    result = send_slack_api('chat.postMessage', {'channel': channel, 'text': text})
    return result.get('ok', False)


def update_approval_message(response_url, text):
    """Update the original approval DM to show result."""
    send_slack_api(None, {'replace_original': 'true', 'text': text}, url=response_url)


# =============================================================================
# DYNAMODB FUNCTIONS
# =============================================================================

def create_access_request(request_id, user_id, user_email, app_name, role_name, group_name, group_id, approval_type, required_approvals, approver_emails):
    """Create a new access request record."""
    now = datetime.now(timezone.utc).isoformat()
    
    item = {
        'request_id': request_id,
        'user_id': user_id,
        'user_email': user_email,
        'app': app_name,
        'role': role_name,
        'group_name': group_name,
        'group_id': group_id,
        'status': 'pending',
        'approval_type': approval_type,
        'required_approvals': required_approvals,
        'approver_emails': approver_emails,
        'approvals_received': [],
        'denials_received': [],
        'created_at': now,
        'updated_at': now
    }
    
    try:
        access_requests_table.put_item(Item=item)
        logger.info(f"Created access request: {request_id}")
        return True
    except Exception as e:
        logger.error(f"Error creating access request: {e}")
        return False


def create_approval_message(approval_message_id, request_id, approver_email, approver_slack_id, message_ts):
    """Track an approval DM sent to an approver."""
    now = datetime.now(timezone.utc).isoformat()
    
    item = {
        'approval_message_id': approval_message_id,
        'request_id': request_id,
        'approver_email': approver_email,
        'approver_slack_id': approver_slack_id,
        'message_ts': message_ts,
        'status': 'pending',
        'created_at': now
    }
    
    try:
        approval_messages_table.put_item(Item=item)
        logger.info(f"Created approval message record: {approval_message_id}")
        return True
    except Exception as e:
        logger.error(f"Error creating approval message: {e}")
        return False


def get_access_request(request_id):
    """Retrieve an access request by ID."""
    try:
        response = access_requests_table.get_item(Key={'request_id': request_id})
        return response.get('Item')
    except Exception as e:
        logger.error(f"Error getting access request: {e}")
        return None


def update_request_status(request_id, status, approver_email=None, action=None):
    """Update request status and record approval/denial."""
    now = datetime.now(timezone.utc).isoformat()
    
    try:
        request = get_access_request(request_id)
        if not request:
            return False
        
        update_expr = 'SET #status = :status, updated_at = :now'
        expr_values = {':status': status, ':now': now}
        expr_names = {'#status': 'status'}
        
        if action == 'approve' and approver_email:
            approvals = request.get('approvals_received', [])
            if approver_email not in approvals:
                approvals.append(approver_email)
            update_expr += ', approvals_received = :approvals'
            expr_values[':approvals'] = approvals
        elif action == 'deny' and approver_email:
            denials = request.get('denials_received', [])
            if approver_email not in denials:
                denials.append(approver_email)
            update_expr += ', denials_received = :denials'
            expr_values[':denials'] = denials
        
        access_requests_table.update_item(
            Key={'request_id': request_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames=expr_names
        )
        logger.info(f"Updated request {request_id} status to {status}")
        return True
    except Exception as e:
        logger.error(f"Error updating request status: {e}")
        return False

def mark_message_as_handled(request_id, user_id):
    """Updates the message audit table to show who interacted with it."""
    approval_messages_table.update_item(
        Key={'approval_message_id': request_id},
        UpdateExpression="SET #s = :s, handled_by = :u",
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'clicked', ':u': user_id}
    )

# =============================================================================
# APPROVAL FLOW LOGIC
# =============================================================================

def check_approval_threshold(request_id):
    """
    Check if enough approvals have been received.
    Returns: 'approved', 'denied', or 'pending'
    """
    request = get_access_request(request_id)
    if not request:
        return 'pending'
    
    approval_type = request.get('approval_type')
    required = request.get('required_approvals', 1)
    approvals = request.get('approvals_received', [])
    denials = request.get('denials_received', [])
    
    if denials:
        return 'denied'
    
    if approval_type == 'NONE':
        return 'approved'
    
    if len(approvals) >= required:
        return 'approved'
    
    return 'pending'


def process_new_request(user_id, channel, app_name, role_name):
    """
    Process a newly confirmed access request.
    Creates request record and sends approval DMs as needed.
    """
    role_config = get_role_config(app_name, role_name)
    if not role_config:
        logger.error(f"Role not found: {app_name}/{role_name}")
        send_slack_message(channel, f"Sorry, I couldn't find the role {role_name} for {app_name}.")
        return None
    
    approval = role_config.get('approval', {})
    approval_type = approval.get('type', 'MANUAL')
    approver_emails = approval.get('approver_emails', [])
    threshold = approval.get('threshold', 1)
    logic = approval.get('logic', 'ANY')
    group_name = role_config.get('group_name')
    group_id = role_config.get('group_id')
    
    # Determine required approvals based on type and logic
    if approval_type == 'NONE':
        required_approvals = 0
    elif approval_type == 'MANAGER':
        # TODO: Look up user's manager from Okta/Slack
        required_approvals = 1
        logger.warning("MANAGER approval type not fully implemented - requires manager lookup")
    elif approval_type == 'BOTH':
        # TODO: Requires manager AND designated approvers
        # For now, just use designated approvers
        if logic == 'ALL':
            required_approvals = len(approver_emails) if approver_emails else 1
        else:
            required_approvals = threshold if threshold > 0 else 1
        logger.warning("BOTH approval type not fully implemented - manager approval skipped")
    elif approval_type in ('MANUAL', 'ACCOUNT_ID'):
        # MANUAL and ACCOUNT_ID both use approver_emails list
        if logic == 'ALL':
            required_approvals = len(approver_emails) if approver_emails else 1
        else:
            required_approvals = threshold if threshold > 0 else 1
    else:
        logger.warning(f"Unknown approval type: {approval_type}, defaulting to MANUAL behavior")
        required_approvals = threshold if threshold > 0 else 1
    
    requester_email = get_requester_email(user_id)
    if not requester_email:
        logger.error(f"Could not get email for user: {user_id}")
        send_slack_message(channel, "Sorry, I couldn't look up your email. Please try again.")
        return None
    
    request_id = str(uuid.uuid4())
    
    create_access_request(
        request_id=request_id,
        user_id=user_id,
        user_email=requester_email,
        app_name=app_name,
        role_name=role_name,
        group_name=group_name,
        group_id=group_id,
        approval_type=approval_type,
        required_approvals=required_approvals,
        approver_emails=approver_emails
    )
    
    if approval_type == 'NONE':
        logger.info(f"Auto-approving request {request_id}")
        update_request_status(request_id, 'approved')
        
        lambda_client.invoke(
            FunctionName='hagrid-okta-provisioner',
            InvocationType='Event',
            Payload=json.dumps({
                'request_id': request_id,
                'user_email': requester_email,
                'group_id': group_id,
                'channel': channel
            })
        )
        return request_id
    
    send_approval_requests(request_id, requester_email, app_name, role_name, role_config, approver_emails, channel)
    
    return request_id


def send_approval_requests(request_id, requester_email, app_name, role_name, role_config, approver_emails, channel):
    """Send approval DMs to all required approvers with combined IDs for efficient tracking."""
    description = role_config.get('description', '')
    sent_count = 0
    
    for approver_email in approver_emails:
        approver_slack_id = get_approver_slack_id(approver_email)
        
        if not approver_slack_id:
            logger.warning(f"Could not find Slack user for approver: {approver_email}")
            continue
        
        # 1. Pre-generate the message ID to embed in the button
        approval_message_id = str(uuid.uuid4())
        combined_value = f"{request_id}:{approval_message_id}"
        
        # 2. Send DM with the combined value
        message_ts = send_approval_dm(
            approver_slack_id=approver_slack_id,
            request_id=combined_value,
            requester_email=requester_email,
            app_name=app_name,
            role_name=role_name,
            role_description=description
        )
        
        # 3. Create the tracking record
        if message_ts:
            create_approval_message(
                approval_message_id=approval_message_id,
                request_id=request_id,
                approver_email=approver_email,
                approver_slack_id=approver_slack_id,
                message_ts=message_ts
            )
            sent_count += 1
    
    if sent_count > 0:
        send_slack_message(channel, f"I've sent your request to {sent_count} approver(s). I'll let you know when they respond.")
    else:
        logger.error(f"Failed to send any approval DMs for request {request_id}")
        send_slack_message(channel, "Sorry, I couldn't reach any approvers. Please contact IT directly.")


# =============================================================================
# BUTTON CLICK HANDLER
# =============================================================================

def handle_approval_response(user_id, action_id, combined_value, response_url):
    """
    Handle approver's button click (approve/deny).
    Splits combined_value to update specific message and request records.
    """
    # 1. Split the combined string to get both specific IDs
    request_id, approval_msg_id = combined_value.split(':')
    
    approver_email = get_requester_email(user_id)
    if not approver_email:
        logger.error(f"Could not get email for approver: {user_id}")
        return

    # 2. Update the specific message record directly by its Primary Key
    approval_messages_table.update_item(
        Key={'approval_message_id': approval_msg_id},
        UpdateExpression="SET #s = :s, handled_by = :u",
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'clicked', ':u': user_id}
    )
    
    request = get_access_request(request_id)
    if not request:
        logger.error(f"Request not found: {request_id}")
        update_approval_message(response_url, "This request no longer exists.")
        return
    
    if request.get('status') != 'pending':
        update_approval_message(response_url, f"This request has already been {request.get('status')}.")
        return
    
    approvals = request.get('approvals_received', [])
    denials = request.get('denials_received', [])
    if approver_email in approvals or approver_email in denials:
        update_approval_message(response_url, "You've already responded to this request.")
        return
    
    if action_id == 'approve_request':
        update_request_status(request_id, 'pending', approver_email, 'approve')
        update_approval_message(response_url, f"✓ You approved access for {request.get('user_email')} to {request.get('app')} {request.get('role')}.")
        logger.info(f"Approver {approver_email} approved request {request_id}")
        
    elif action_id == 'deny_request':
        update_request_status(request_id, 'denied', approver_email, 'deny')
        update_approval_message(response_url, f"✗ You denied access for {request.get('user_email')} to {request.get('app')} {request.get('role')}.")
        logger.info(f"Approver {approver_email} denied request {request_id}")
        
        send_slack_message(request.get('user_id'), f"Your request for {request.get('app')} {request.get('role')} was denied by an approver.")
        return
    
    result = check_approval_threshold(request_id)
    
    if result == 'approved':
        update_request_status(request_id, 'approved')

        send_slack_api('chat.postMessage', {
            'channel': request.get('user_id'),
            'text': f"✅ Your request for *{request.get('app')} {request.get('role')}* was *approved* by <@{user_id}>."
        })
        
        lambda_client.invoke(
            FunctionName='hagrid-okta-provisioner',
            InvocationType='Event',
            Payload=json.dumps({
                'request_id': request_id,
                'user_email': request.get('user_email'),
                'group_id': request.get('group_id'),
                'channel': request.get('user_id')
            })
        )


# =============================================================================
# MAIN HANDLER
# =============================================================================

def lambda_handler(event, context):
    """
    Main entry point for Approval Manager.
    
    Handles two event types:
    1. New request from Conversation Manager:
       {
           'type': 'new_request',
           'user_id': 'U12345',
           'channel': 'D12345',
           'app': 'aws',
           'role': 'developer'
       }
    
    2. Button click from Event Handler:
       {
           'type': 'approval_response',
           'user_id': 'U67890',
           'action_id': 'approve_request',
           'action_value': 'request-uuid',
           'response_url': 'https://hooks.slack.com/...'
       }
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    event_type = event.get('type')
    
    if event_type == 'new_request':
        user_id = event.get('user_id')
        channel = event.get('channel')
        app_name = event.get('app')
        role_name = event.get('role')
        
        if not all([user_id, channel, app_name, role_name]):
            logger.error("Missing required fields for new request")
            return {'statusCode': 400, 'body': 'Missing required fields'}
        
        request_id = process_new_request(user_id, channel, app_name, role_name)
        
        return {
            'statusCode': 200,
            'body': json.dumps({'request_id': request_id})
        }
    
    elif event_type == 'approval_response':
        user_id = event.get('user_id')
        action_id = event.get('action_id')
        request_id = event.get('action_value')
        response_url = event.get('response_url')
        
        if not all([user_id, action_id, request_id, response_url]):
            logger.error("Missing required fields for approval response")
            return {'statusCode': 400, 'body': 'Missing required fields'}
        
        handle_approval_response(user_id, action_id, request_id, response_url)
        
        return {'statusCode': 200, 'body': 'OK'}
    
    else:
        logger.warning(f"Unknown event type: {event_type}")
        return {'statusCode': 400, 'body': 'Unknown event type'}