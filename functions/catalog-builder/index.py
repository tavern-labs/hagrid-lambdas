import json
import os
import boto3
import requests
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ApprovalType(Enum):
    """Approval type enumeration matching Okta schema"""
    NONE = "NONE"
    MANAGER = "MANAGER"
    ACCOUNT_ID = "ACCOUNT_ID"
    ACCOUNT_EMAIL = "ACCOUNT_EMAIL"
    BOTH = "BOTH"
    MANUAL = "MANUAL"


class ApprovalLogic(Enum):
    """Approval logic enumeration"""
    ALL = "ALL"
    ANY = "ANY"


@dataclass
class ApprovalConfig:
    """Approval configuration from Okta group attributes"""
    approval_type: str
    approval_emails: List[str]
    approval_logic: str
    approval_threshold: int

    def describe_requirement(self) -> str:
        """Generate concise approval requirement for text catalog"""
        if self.approval_type == ApprovalType.NONE.value:
            return "Auto-approved."
        
        if self.approval_type == ApprovalType.MANAGER.value:
            return "Manager approval."
        
        if self.approval_type == ApprovalType.MANUAL.value:
            return "Manual review - contact IT directly."
        
        if self.approval_type in [ApprovalType.ACCOUNT_ID.value, ApprovalType.ACCOUNT_EMAIL.value]:
            if self.approval_logic == ApprovalLogic.ALL.value:
                count = self.approval_threshold if self.approval_threshold > 0 else len(self.approval_emails)
                return f"All designated approvers ({count} required)."
            else:
                threshold = self.approval_threshold if self.approval_threshold > 0 else 1
                if threshold == 1:
                    return "Any 1 designated approver."
                return f"Any {threshold} designated approvers."
        
        if self.approval_type == ApprovalType.BOTH.value:
            if self.approval_logic == ApprovalLogic.ALL.value:
                count = self.approval_threshold if self.approval_threshold > 0 else len(self.approval_emails)
                return f"Manager AND all designated approvers ({count} required)."
            else:
                threshold = self.approval_threshold if self.approval_threshold > 0 else 1
                if threshold == 1:
                    return "Manager OR any 1 designated approver."
                return f"Manager OR any {threshold} designated approvers."
        
        return "Contact IT for approval requirements."


class OktaGroupCatalogBuilder:
    """Fetches and processes Okta groups using REST API"""

    def __init__(self, okta_domain: str, okta_token: str):
        self.base_url = f'https://{okta_domain}/api/v1'
        self.headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'SSWS {okta_token}'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def fetch_app_groups(self, prefix: str = "app-") -> List[Dict[str, Any]]:
        """Fetch all groups with specified prefix"""
        app_groups = []
        url = f'{self.base_url}/groups'
        params = {'search': f'profile.name sw "{prefix}"', 'limit': 200}

        try:
            while url:
                print(f"Fetching: {url}")
                response = self.session.get(url, params=params)
                response.raise_for_status()
                groups = response.json()
                print(f"Retrieved {len(groups)} groups")

                for group in groups:
                    group_data = self._process_group(group)
                    if group_data:
                        app_groups.append(group_data)

                url = None
                params = None
                if 'link' in response.headers:
                    for link in response.headers['link'].split(','):
                        if 'rel="next"' in link:
                            url = link[link.find('<')+1:link.find('>')]
                            break

        except requests.exceptions.RequestException as e:
            print(f"Error fetching groups: {e}")

        return app_groups

    def _process_group(self, group: Dict) -> Optional[Dict[str, Any]]:
        """Extract relevant information from a group"""
        try:
            profile = group.get('profile', {})
            group_name = profile.get('name', '')
            group_id = group.get('id', '')
            description = profile.get('description')

            name_parts = group_name.split('-')
            if len(name_parts) < 2:
                return None

            app_name = name_parts[1]
            role_name = '-'.join(name_parts[2:]) if len(name_parts) > 2 else "user"

            approval_type = profile.get('approval_type', 'MANUAL')
            approval_emails = profile.get('approval_emails', [])
            approval_logic = profile.get('approval_logic', 'ANY')
            approval_threshold = profile.get('approval_threshold', 1)

            if not isinstance(approval_emails, list):
                approval_emails = [approval_emails] if approval_emails else []

            if approval_type not in [e.value for e in ApprovalType]:
                approval_type = 'MANUAL'

            if approval_logic not in [e.value for e in ApprovalLogic]:
                approval_logic = 'ANY'

            approval_config = ApprovalConfig(
                approval_type=approval_type,
                approval_emails=approval_emails,
                approval_logic=approval_logic,
                approval_threshold=int(approval_threshold)
            )

            return {
                'group_id': group_id,
                'group_name': group_name,
                'app_name': app_name,
                'role_name': role_name,
                'description': description or f"Access to {app_name} as {role_name}",
                'approval': {
                    'type': approval_config.approval_type,
                    'logic': approval_config.approval_logic,
                    'threshold': approval_config.approval_threshold,
                    'approver_emails': approval_config.approval_emails,
                    'requirement_text': approval_config.describe_requirement()
                }
            }
        except Exception as e:
            print(f"Error processing group: {e}")
            return None


def build_catalog_json(groups: List[Dict[str, Any]]) -> str:
    """Build JSON catalog for Approval Manager"""
    apps_catalog = {}
    for group in groups:
        app_name = group['app_name']
        if app_name not in apps_catalog:
            apps_catalog[app_name] = {'app_name': app_name, 'roles': []}
        apps_catalog[app_name]['roles'].append({
            'role_name': group['role_name'],
            'group_id': group['group_id'],
            'group_name': group['group_name'],
            'description': group['description'],
            'approval': group['approval']
        })

    catalog = {
        'metadata': {
            'last_updated': datetime.utcnow().isoformat(),
            'total_apps': len(apps_catalog),
            'total_roles': len(groups)
        },
        'applications': list(apps_catalog.values())
    }
    return json.dumps(catalog)


def build_catalog_text(groups: List[Dict[str, Any]]) -> str:
    """Build text catalog for AI/Conversation Manager"""
    apps_catalog = {}
    for group in groups:
        app_name = group['app_name']
        if app_name not in apps_catalog:
            apps_catalog[app_name] = []
        apps_catalog[app_name].append(group)

    lines = ["# SERVICE_CATALOG_START", ""]
    for app_name in sorted(apps_catalog.keys()):
        roles = apps_catalog[app_name]
        lines.append(f'<APP name="{app_name.capitalize()}">')
        for role in sorted(roles, key=lambda r: r['role_name']):
            lines.append(f"  - ROLE: {role['role_name']}")
            lines.append(f'    DESCRIPTION: "{role["description"]}"')
            lines.append(f'    APPROVAL_REQUIREMENT: "{role["approval"]["requirement_text"]}"')
        lines.append("</APP>")
        lines.append("")
    lines.append("# SERVICE_CATALOG_END")
    return "\n".join(lines)


def get_okta_credentials() -> Dict[str, str]:
    """Fetch Okta credentials from SSM"""
    parameter_name = os.environ.get('OKTA_CREDENTIALS_SSM_NAME', '/hagrid/okta-credentials')
    ssm_client = boto3.client('ssm')
    response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
    return json.loads(response['Parameter']['Value'])


def save_to_s3(content: str, bucket: str, key: str) -> bool:
    """Save content to S3"""
    try:
        s3_client = boto3.client('s3')
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode('utf-8'),
            ContentType='application/json' if key.endswith('.json') else 'text/plain'
        )
        print(f"Saved to s3://{bucket}/{key}")
        return True
    except Exception as e:
        print(f"Error saving to S3: {e}")
        return False


def lambda_handler(event, context):
    """AWS Lambda handler"""
    credentials = get_okta_credentials()
    group_prefix = os.environ.get('OKTA_GROUP_PREFIX', 'app-')
    s3_bucket = os.environ.get('CATALOG_S3_BUCKET')

    try:
        catalog_builder = OktaGroupCatalogBuilder(credentials['domain'], credentials['api_token'])
        app_groups = catalog_builder.fetch_app_groups(prefix=group_prefix)
        print(f"Found {len(app_groups)} groups")

        catalog_json = build_catalog_json(app_groups)
        catalog_text = build_catalog_text(app_groups)

        json_saved = save_to_s3(catalog_json, s3_bucket, 'catalog.json')
        text_saved = save_to_s3(catalog_text, s3_bucket, 'catalog.txt')

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Successfully built catalogs',
                'groups_count': len(app_groups),
                'json_saved': json_saved,
                'text_saved': text_saved
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}