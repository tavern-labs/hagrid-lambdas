"""
Conversation Manager Lambda Function

This function handles AI/NLP processing for Slack messages to detect user intent
and manage the conversational flow of access requests.

Responsibilities:
- Process natural language messages from users
- Detect intent (e.g., "I need access to X", "What can I access?")
- Extract entities (app names, roles, etc.)
- Maintain conversation context in DynamoDB
- Generate appropriate responses via AI
- Send responses back to user via Slack API
- Invoke approval-manager when a complete access request is ready
"""

import json
import logging
import os
import boto3
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize clients outside handler
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')

# Cache for SSM values
_slack_bot_token = None
_okta_catalog = None
_system_prompt = None
_gemini_api_key = None
_processed_messages = set()

# Table references
conversations_table = dynamodb.Table(os.environ.get('CONVERSATIONS_TABLE', 'hagrid-conversations'))
access_requests_table = dynamodb.Table(os.environ.get('ACCESS_REQUESTS_TABLE', 'hagrid-access-requests'))


# =============================================================================
# SSM GETTERS
# =============================================================================

def get_slack_bot_token():
    """Fetch and cache Slack bot token for sending replies."""
    global _slack_bot_token
    if _slack_bot_token:
        return _slack_bot_token
    
    param_name = os.environ.get('SLACK_BOT_TOKEN_SSM', '/hagrid/slack-bot-token')
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _slack_bot_token = response['Parameter']['Value']
    return _slack_bot_token


def get_okta_catalog():
    """Fetch and cache Okta app catalog (available apps/roles)."""
    global _okta_catalog
    if _okta_catalog:
        return _okta_catalog
    
    param_name = os.environ.get('OKTA_CATALOG_SSM', '/hagrid/okta-catalog')
    
    try:
        # Added WithDecryption=True to handle SecureString parameters
        response = ssm.get_parameter(Name=param_name, WithDecryption=True)
        _okta_catalog = response['Parameter']['Value']
        
        # LOG A SNIPPET TO VERIFY (But don't log the whole secret!)
        # This will help you see if # SERVICE_CATALOG_START is actually there
        logger.info(f"Loaded catalog: {len(_okta_catalog)} chars. Starts with: {_okta_catalog[:50]}...")
        
        return _okta_catalog
        
    except Exception as e:
        logger.error(f"Error fetching SSM parameter {param_name}: {e}")
        return "" # Return empty so the bot uses the 'Unknown Request' logic


def get_system_prompt():
    """Fetch and cache AI system prompt."""
    global _system_prompt
    if _system_prompt:
        return _system_prompt
    
    param_name = os.environ.get('SYSTEM_PROMPT_SSM', '/hagrid/system-prompt')
    response = ssm.get_parameter(Name=param_name)
    _system_prompt = response['Parameter']['Value']
    return _system_prompt


def get_gemini_api_key():
    """Fetch and cache Gemini API key."""
    global _gemini_api_key
    if _gemini_api_key:
        return _gemini_api_key
    
    param_name = os.environ.get('GEMINI_API_KEY_SSM', '/hagrid/gemini-api-key')
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _gemini_api_key = response['Parameter']['Value']
    return _gemini_api_key


# =============================================================================
# CONVERSATION HISTORY
# =============================================================================

def get_conversation_history(user_id):
    """
    Retrieve today's conversation history for a user.
    Returns list of messages in chronological order.
    """
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conversation_id = f"{user_id}-{today}"
    
    try:
        response = conversations_table.query(
            KeyConditionExpression='conversation_id = :cid',
            ExpressionAttributeValues={':cid': conversation_id},
            ScanIndexForward=True
        )
        return response.get('Items', [])
    except Exception as e:
        logger.error(f"Error fetching conversation history: {e}")
        return []


def save_message(user_id, role, content):
    """
    Save a message to conversation history.
    role: 'user' or 'assistant'
    """
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conversation_id = f"{user_id}-{today}"
    
    # Get next message index
    history = get_conversation_history(user_id)
    message_index = len(history)
    
    # TTL: 30 days from now
    ttl = int(datetime.now(timezone.utc).timestamp()) + (30 * 24 * 60 * 60)
    
    try:
        conversations_table.put_item(Item={
            'conversation_id': conversation_id,
            'message_index': message_index,
            'role': role,
            'content': content,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'expires_at': ttl
        })
    except Exception as e:
        logger.error(f"Error saving message: {e}")


# =============================================================================
# SLACK INTEGRATION
# =============================================================================

def send_slack_message(channel, text):
    """Send a message to Slack channel/DM."""
    token = get_slack_bot_token()
    
    url = 'https://slack.com/api/chat.postMessage'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    data = json.dumps({
        'channel': channel,
        'text': text
    }).encode('utf-8')
    
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            
            if not result.get('ok'):
                logger.error(f"Slack API error: {result.get('error')}")
                return False
            return True
    except urllib.error.URLError as e:
        logger.error(f"Error sending Slack message: {e}")
        return False


# =============================================================================
# AI INTEGRATION
# =============================================================================

def call_ai(messages, catalog):
    """
    Call AI API with conversation history and catalog context.
    Returns AI response text.
    
    Using Google Gemini (free tier: 1M tokens/day).
    """
    api_key = get_gemini_api_key()
    url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent'
    
    # Build system context
    system_prompt = get_system_prompt()
    logger.info(f"System prompt length: {len(system_prompt)}")
    logger.info(f"Catalog length: {len(catalog)}")
    
    # 1. Get the actual query the user just sent
    user_query = messages[-1]['content']

    # 2. Combine them into one "Grounded" message
    grounded_text = f"{catalog}\n\nUser Request: {user_query}"

    # 3. Add the historical messages
    contents = []
    for msg in messages[:-1]:  # All messages EXCEPT the last one
        role = 'user' if msg['role'] == 'user' else 'model'
        contents.append({
            'role': role,
            'parts': [{'text': msg['content']}]
        })

    # 4. Push the NEW grounded message as the final 'user' turn
    contents.append({
        'role': 'user',
        'parts': [{'text': grounded_text}]
    })

    logger.info(f"DEBUG CATALOG CONTENT: {catalog}")
    
    payload = {
        "system_instruction": {
        "parts": [
            {"text": system_prompt}
        ]
        },
        'contents': contents,
        'generationConfig': {
            'temperature': 0.0,
            'maxOutputTokens': 500, 
            'topP' : 0.1
        }
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'X-goog-api-key': api_key
            }
        )
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"AI API error: {e}")
        return "Sorry, I'm having trouble processing your request. Please try again."


# =============================================================================
# MAIN HANDLER
# =============================================================================

def lambda_handler(event, context):
    """
    Process incoming messages and manage conversation flow.
    
    Expected event format (from Event Handler):
    {
        'user_id': 'U12345',
        'text': 'I need access to AWS',
        'channel': 'D12345',
        'message_ts': '1234567890.123456'
    }
    """
    logger.info(f"Received event: {json.dumps(event)}")

    # Deduplicate - prevent processing same message twice
    message_ts = event.get('message_ts')
    if message_ts:
        if message_ts in _processed_messages:
            logger.info(f"Skipping duplicate message: {message_ts}")
            return {'statusCode': 200, 'body': 'Duplicate'}
        _processed_messages.add(message_ts)   

    try:
        # Extract message details
        user_id = event.get('user_id')
        text = event.get('text', '')
        channel = event.get('channel')
        
        if not user_id or not channel:
            logger.error("Missing user_id or channel")
            return {'statusCode': 400, 'body': 'Missing required fields'}
        
        # Save user message to history
        save_message(user_id, 'user', text)
        
        # Get conversation history and catalog
        history = get_conversation_history(user_id)
        catalog = get_okta_catalog()
        
        # Build messages for AI
        messages = [{'role': msg['role'], 'content': msg['content']} for msg in history]
        
        # Call AI for response
        ai_response = call_ai(messages, catalog)
        logger.info(f"AI response: {ai_response}")
        
        # Save assistant response to history
        save_message(user_id, 'assistant', ai_response)
        
        # Send response to user
        send_slack_message(channel, ai_response)
        
        # TODO: Parse AI response for structured actions
        # TODO: If AI detected complete access request, invoke Approval Manager
        
        return {
            'statusCode': 200,
            'body': json.dumps({'status': 'processed'})
        }
        
    except Exception as e:
        logger.error(f"Error in conversation manager: {e}", exc_info=True)
        
        # Attempt to notify user of error
        if 'channel' in event:
            send_slack_message(event['channel'], "Sorry, something went wrong. Please try again.")
        
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
