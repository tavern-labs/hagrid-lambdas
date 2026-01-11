"""
Event Handler Lambda Function

This function receives all incoming Slack webhook events and routes them
to the appropriate downstream functions based on event type.

Responsibilities:
- Validate Slack webhook signatures
- Handle Slack URL verification challenge
- Route events to conversation-manager for message events
- Route button interactions to approval-manager
- Log all events for debugging and audit purposes
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Main Lambda handler for Slack events.

    Args:
        event: API Gateway proxy event containing Slack webhook payload
        context: Lambda context object

    Returns:
        Dict with statusCode and body for API Gateway response
    """
    try:
        # Parse the incoming request body
        body = json.loads(event.get('body', '{}'))
        logger.info(f"Received event: {json.dumps(body)}")

        # Handle Slack URL verification challenge
        # This is sent when you configure the webhook URL in Slack
        if body.get('type') == 'url_verification':
            logger.info("Handling URL verification challenge")
            return {
                'statusCode': 200,
                'body': json.dumps({'challenge': body.get('challenge')})
            }

        # Handle event callbacks (actual Slack events)
        if body.get('type') == 'event_callback':
            event_type = body.get('event', {}).get('type')
            logger.info(f"Processing event callback: {event_type}")

            # TODO: Route to conversation-manager for message events
            # TODO: Implement event routing logic

            return {
                'statusCode': 200,
                'body': json.dumps({'status': 'ok'})
            }

        # Handle interactive components (button clicks, etc.)
        # These come as form-encoded payload in a different format
        if 'payload' in event.get('body', ''):
            logger.info("Processing interactive component")

            # TODO: Route to approval-manager for button interactions

            return {
                'statusCode': 200,
                'body': json.dumps({'status': 'ok'})
            }

        # Unknown event type
        logger.warning(f"Unknown event type: {body.get('type')}")
        return {
            'statusCode': 200,
            'body': json.dumps({'status': 'ignored'})
        }

    except Exception as e:
        logger.error(f"Error processing event: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Internal server error'})
        }
