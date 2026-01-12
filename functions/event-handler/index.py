"""
Event Handler Lambda Function
Hagrid Event Handler Lambda

Entry point for all Slack webhook events. Acts as a router - no business logic.

Flow:
1. Verify request authenticity (Slack signing secret)
2. Identify event type (message, button click, URL verification)
3. Route to appropriate Lambda (Conversation Manager, Approval Manager)
4. Respond 200 immediately to Slack (required within 3 seconds)

Event Types Handled:
- url_verification: One-time handshake when configuring Slack app
- message.im: Direct messages from users → Conversation Manager
- block_actions: Button clicks from approval DMs → Approval Manager

Environment Variables:
- SLACK_SIGNING_SECRET_SSM: SSM parameter name for signing secret
"""

import json
import logging
import hmac
import hashlib
import time
import os
import boto3
from urllib.parse import parse_qs

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize outside handler for connection reuse
ssm = boto3.client('ssm')
_signing_secret = None


def get_signing_secret():
    """Fetch and cache Slack signing secret from SSM."""
    global _signing_secret
    if _signing_secret:
        return _signing_secret
    
    param_name = os.environ.get('SLACK_SIGNING_SECRET_SSM', '/hagrid/slack-signing-secret')
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _signing_secret = response['Parameter']['Value']
    return _signing_secret


def verify_slack_signature(event):
    """Verify request is from Slack using signing secret."""
    headers = event.get('headers', {})
    body = event.get('body', '')
    
    # Normalize header keys (API Gateway may lowercase them)
    headers_lower = {k.lower(): v for k, v in headers.items()}
    timestamp = headers_lower.get('x-slack-request-timestamp', '')
    signature = headers_lower.get('x-slack-signature', '')
    
    if not timestamp or not signature:
        logger.warning('Missing Slack signature headers')
        return False
    
    # Reject requests older than 5 minutes (replay attack prevention)
    if abs(time.time() - int(timestamp)) > 300:
        logger.warning(f'Request too old: {timestamp}')
        return False
    
    # Build and compare signature
    sig_basestring = f'v0:{timestamp}:{body}'
    my_signature = 'v0=' + hmac.new(
        get_signing_secret().encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(my_signature, signature)


def lambda_handler(event, context):
    """Main Lambda handler for Slack events."""
    logger.info(f"Received event: {json.dumps(event)}")
    
    # Verify signature first
    if not verify_slack_signature(event):
        return {'statusCode': 401, 'body': 'Invalid signature'}
    
    body_raw = event.get('body', '{}')
    content_type = event.get('headers', {}).get('content-type', '')
    
    # Handle interactive payloads (button clicks) - URL-encoded
    if 'application/x-www-form-urlencoded' in content_type:
        parsed = parse_qs(body_raw)
        payload = parsed.get('payload', ['{}'])[0]
        logger.info(f"Interactive payload: {payload}")
        # TODO: Route to approval-manager
        return {'statusCode': 200, 'body': ''}
    
    # Parse JSON body for standard events
    try:
        body = json.loads(body_raw)
    except json.JSONDecodeError as e:
        logger.error(f'Invalid JSON: {e}')
        return {'statusCode': 200, 'body': 'OK'}
    
    # URL verification - return plain text challenge
    if body.get('type') == 'url_verification':
        logger.info("URL verification challenge")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/plain'},
            'body': body.get('challenge', '')
        }
    
    # Event callbacks
    if body.get('type') == 'event_callback':
        event_data = body.get('event', {})
        event_type = event_data.get('type')
        logger.info(f"Event type: {event_type}")
            
        if event_type == 'message':
            # Ignore bot messages
            if event_data.get('bot_id') or event_data.get('subtype'):
                logger.info('Ignoring bot/subtype message')
                return {'statusCode': 200, 'body': 'OK'}
                
            logger.info(f"DM - User: {event_data.get('user')}, Text: {event_data.get('text')}")
                
            # Route to Conversation Manager
            lambda_client = boto3.client('lambda')
            lambda_client.invoke(
                FunctionName='hagrid-conversation-manager',
                InvocationType='Event',  # Async - don't wait
                Payload=json.dumps({
                    'user_id': event_data.get('user'),
                    'text': event_data.get('text', ''),
                    'channel': event_data.get('channel'),
                    'message_ts': event_data.get('ts')
                })
            )
        
      return {'statusCode': 200, 'body': 'OK'}
