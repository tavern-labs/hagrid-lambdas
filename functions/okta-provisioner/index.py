"""
Okta Provisioner Lambda Function

This function handles the actual provisioning of access by adding users
to Okta groups when their access requests are approved.

Responsibilities:
- Receive approved access requests from approval-manager
- Authenticate with Okta API using credentials from Secrets Manager
- Add user to specified Okta group(s)
- Handle Okta API errors and retries
- Log all provisioning actions for audit trail
- Return success/failure status to approval-manager
"""

import json
import logging
import os
import urllib.request
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client('ssm')

# Cache for credentials
_okta_creds = None

def get_okta_creds():
    """Fetch and cache Okta JSON credentials from SSM."""
    global _okta_creds
    if _okta_creds:
        return _okta_creds
    
    param_name = os.environ.get('OKTA_CREDS_SSM', '/hagrid/okta-credentials')
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _okta_creds = json.loads(response['Parameter']['Value'])
    return _okta_creds

def add_user_to_okta_group(user_email, group_id):
    """Assigns an Okta user to a group using credentials from JSON."""
    creds = get_okta_creds()
    okta_domain = creds.get('domain')
    api_token = creds.get('api_token')
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"SSWS {api_token}"
    }

    try:
        # 1. Find User ID by Email
        search_url = f"https://{okta_domain}/api/v1/users?q={user_email}&limit=1"
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req) as response:
            users = json.loads(response.read().decode('utf-8'))
            
        if not users:
            logger.error(f"User {user_email} not found in Okta.")
            return False
            
        user_id = users[0]['id']

        # 2. Add User to Group
        provision_url = f"https://{okta_domain}/api/v1/groups/{group_id}/users/{user_id}"
        prov_req = urllib.request.Request(provision_url, headers=headers, method='PUT')
        with urllib.request.urlopen(prov_req) as response:
            if response.status == 204:
                logger.info(f"Successfully provisioned {user_email} to {group_id}")
                return True
            
    except Exception as e:
        logger.error(f"Okta Provisioning Error: {e}")
        return False



def lambda_handler(event, context):
    """
    Handles provisioning event from Approval Manager.
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    user_email = event.get('user_email')
    group_id = event.get('group_id')

    if not all([user_email, group_id]):
        logger.error("Missing email or group_id in event payload")
        return {'statusCode': 400, 'body': 'Missing required fields'}

    success = add_user_to_okta_group(user_email, group_id)

    return {
        'statusCode': 200 if success else 500,
        'body': json.dumps({'success': success, 'request_id': event.get('request_id')})
    }