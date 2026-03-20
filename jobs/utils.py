import json
import logging
from datetime import datetime

import redis
from vchat.settings import config

logger = logging.getLogger(__name__)


def send_flash_background(user_id: int, message: str, category: str = "success"):
    """
    Send a flash notification to a user from a background task (synchronous).
    """
    if not user_id:
        return

    try:
        redis_url = config.get("redis_uri")
        r = redis.from_url(redis_url, decode_responses=True)

        # Remove pipe symbol for safety as per utils.flash
        message = message.replace("|", "")

        key = f"flash_toast_{user_id}"
        channel_name = f"user_{user_id}"

        msg_data = {
            "type": "flash",
            "body": message,
            "category": category,
            "created_at": datetime.now().isoformat(),
        }

        payload = json.dumps(msg_data)

        # Push to list for persistence
        r.rpush(key, payload)
        r.expire(key, 60)

        # Publish for realtime
        r.publish(channel_name, payload)

        r.close()
    except Exception as e:
        logger.error(f"Failed to send background flash to user {user_id}: {e}")
