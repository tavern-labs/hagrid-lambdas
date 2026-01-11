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
    BOTH = "BOTH"
    MANUAL = "MANUAL"


class ApprovalLogic(Enum):
    """Approval logic enumeration"""
    ALL = "ALL"  # AND conditions
    ANY = "ANY"  # OR conditions


@dataclass
class ApprovalConfig:
    """Approval configuration from Okta group attributes"""
    approval_type: str
    approval_emails: List[str]
    approval_logic: str
    approval_threshold: int

    def describe_flow(self) -> str:
        """Generate human-readable approval flow description"""
        if self.approval_type == ApprovalType.NONE.value:
            return "Auto-approved (no approval required)"

        parts = []

        if self.approval_type == ApprovalType.MANAGER.value:
            parts.append("Manager approval")
        elif self.approval_type == ApprovalType.ACCOUNT_ID.value:
            parts.append(f"Approval from designated approvers ({len(self.approval_emails)} approvers)")
        elif self.approval_type == ApprovalType.BOTH.value:
            parts.append(f"Manager AND designated approvers ({len(self.approval_emails)} approvers)")
        elif self.approval_type == ApprovalType.MANUAL.value:
            parts.append("Manual approval process")

        if self.approval_type in [ApprovalType.ACCOUNT_ID.value, ApprovalType.BOTH.value]:
            if self.approval_logic == ApprovalLogic.ALL.value:
                if self.approval_threshold == 0:
                    parts.append("- requires ALL approvers")
                else:
                    parts.append(f"- requires ALL of {self.approval_threshold} approvers")
            else:  # ANY
                if self.approval_threshold > 0:
                    parts.append(f"- requires ANY {self.approval_threshold} approver(s)")
                else:
                    parts.append("- requires at least 1 approver")

        return " ".join(parts)


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
        """Fetch all groups that assign apps (with specified prefix)"""
        app_groups = []

        # Okta Groups API with search
        url = f'{self.base_url}/groups'
        params = {
            'search': f'profile.name sw "{prefix}"',
            'limit': 200  # Max per page
        }

        try:
            while url:
                print(f"Fetching: {url}")
                response = self.session.get(url, params=params)
                response.raise_for_status()

                groups = response.json()
                print(f"Retrieved {len(groups)} groups in this page")

                for group in groups:
                    group_data = self._process_group(group)
                    if group_data:
                        app_groups.append(group_data)

                # Handle pagination - check for 'next' link in headers
                url = None
                params = None  # Only use params on first request

                if 'link' in response.headers:
                    links = response.headers['link'].split(',')
                    for link in links:
                        if 'rel="next"' in link:
                            # Extract URL from: <https://...>; rel="next"
                            url = link[link.find('<')+1:link.find('>')]
                            break

        except requests.exceptions.RequestException as e:
            print(f"Error fetching groups: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response: {e.response.text}")

        return app_groups

    def _process_group(self, group: Dict) -> Optional[Dict[str, Any]]:
        """Extract relevant information from a group"""
        try:
            profile = group.get('profile', {})
            group_name = profile.get('name', '')
            group_id = group.get('id', '')
            group_type = group.get('type', 'OKTA_GROUP')
            description = profile.get('description')

            # Parse the group name to extract app and role
            name_parts = group_name.split('-')
            if len(name_parts) < 2:
                print(f"Warning: Group {group_name} doesn't follow expected naming convention")
                return None

            app_name = name_parts[1]
            role_name = '-'.join(name_parts[2:]) if len(name_parts) > 2 else "user"

            # Extract approval configuration from custom attributes
            approval_type = profile.get('approval_type', 'MANUAL')
            approval_emails = profile.get('approval_emails', [])
            approval_logic = profile.get('approval_logic', 'ANY')
            approval_threshold = profile.get('approval_threshold', 1)

            # Ensure approval_emails is a list
            if not isinstance(approval_emails, list):
                if approval_emails:
                    approval_emails = [approval_emails]
                else:
                    approval_emails = []

            # Validate approval_type
            if approval_type not in [e.value for e in ApprovalType]:
                print(f"Warning: Invalid approval_type '{approval_type}' for group {group_name}, defaulting to MANUAL")
                approval_type = 'MANUAL'

            # Validate approval_logic
            if approval_logic not in [e.value for e in ApprovalLogic]:
                print(f"Warning: Invalid approval_logic '{approval_logic}' for group {group_name}, defaulting to ANY")
                approval_logic = 'ANY'

            # Create approval config
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
                    'flow_description': approval_config.describe_flow()
                },
                'metadata': {
                    'is_currently_assigned': group_type == 'APP_GROUP',
                    'okta_group_type': group_type
                }
            }
        except Exception as e:
            print(f"Error processing group {group.get('id', 'unknown')}: {e}")
            import traceback
            traceback.print_exc()
            return None


def build_catalog_json(groups: List[Dict[str, Any]]) -> str:
    """Build a structured JSON catalog for the LLM"""

    # Organize by app
    apps_catalog = {}
    for group in groups:
        app_name = group['app_name']
        if app_name not in apps_catalog:
            apps_catalog[app_name] = {
                'app_name': app_name,
                'roles': []
            }

        apps_catalog[app_name]['roles'].append({
            'role_name': group['role_name'],
            'group_id': group['group_id'],
            'group_name': group['group_name'],
            'description': group['description'],
            'approval': group['approval'],
            'metadata': group['metadata']
        })

    catalog = {
        'metadata': {
            'last_updated': datetime.utcnow().isoformat(),
            'total_apps': len(apps_catalog),
            'total_roles': len(groups),
            'schema_version': '1.0'
        },
        'applications': list(apps_catalog.values())
    }

    return json.dumps(catalog, indent=2)


def get_okta_credentials() -> Dict[str, str]:
    """
    Fetch Okta credentials from AWS Systems Manager Parameter Store.

    Returns:
        Dict with 'domain' and 'api_token' keys

    Raises:
        Exception if parameter not found or invalid format
    """
    # Get parameter name from environment variable, default to /hagrid/okta/credentials
    parameter_name = os.environ.get('OKTA_CREDENTIALS_PARAMETER', '/hagrid/okta/credentials')

    try:
        ssm_client = boto3.client('ssm')
        response = ssm_client.get_parameter(
            Name=parameter_name,
            WithDecryption=True  # Decrypt if it's a SecureString
        )

        # Parse JSON credentials
        credentials = json.loads(response['Parameter']['Value'])

        # Validate required fields
        if 'domain' not in credentials or 'api_token' not in credentials:
            raise ValueError("SSM parameter must contain 'domain' and 'api_token' fields")

        return credentials

    except Exception as e:
        print(f"Error fetching Okta credentials from SSM parameter '{parameter_name}': {e}")
        raise


def lambda_handler(event, context):
    """AWS Lambda handler function - Using REST API"""

    # Fetch Okta credentials from SSM Parameter Store
    credentials = get_okta_credentials()
    okta_domain = credentials['domain']
    okta_token = credentials['api_token']

    # Group prefix can be configured via environment variable
    group_prefix = os.environ.get('OKTA_GROUP_PREFIX', 'app-')

    try:
        # Fetch Okta groups
        print(f"Fetching Okta groups with prefix '{group_prefix}'...")
        print(f"Using Okta REST API directly")

        catalog_builder = OktaGroupCatalogBuilder(okta_domain, okta_token)
        app_groups = catalog_builder.fetch_app_groups(prefix=group_prefix)

        print(f"Found {len(app_groups)} app-assigning groups")

        if len(app_groups) == 0:
            print("Warning: No groups found matching criteria")

        # Build catalog
        print("Building catalog JSON...")
        catalog_json = build_catalog_json(app_groups)

        # For testing - just print the catalog
        print("="*80)
        print("CATALOG OUTPUT:")
        print("="*80)
        print(catalog_json)
        print("="*80)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Successfully built Okta groups catalog',
                'groups_count': len(app_groups),
                'catalog_size_bytes': len(catalog_json),
                'catalog': json.loads(catalog_json)  # Include in response for testing
            }, indent=2)
        }

    except Exception as e:
        print(f"Error in lambda_handler: {e}")
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
