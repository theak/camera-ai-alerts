"""Home Assistant integration."""

import logging

import requests

logger = logging.getLogger(__name__)


class HomeAssistant:
    """Home Assistant API client."""

    def __init__(self, url: str, token: str):
        """
        Initialize Home Assistant client.

        Args:
            url: Home Assistant URL (e.g., http://homeassistant.local:8123)
            token: Home Assistant long-lived access token
        """
        self.url = url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def speak(self, message: str, entity_ids: list) -> None:
        """
        Send text message to be spoken by Assist Satellite(s).

        Args:
            message: The text message to speak
            entity_ids: List of Assist Satellite entity IDs
        """
        try:
            url = f"{self.url}/api/services/assist_satellite/announce"
            data = {"entity_id": entity_ids, "message": message}
            response = requests.post(url, headers=self.headers, json=data, timeout=30)
            response.raise_for_status()
            logger.info(f"HA Speaking: '{message}'")
        except Exception as e:
            logger.error(f"Error calling HA speak: {e}")

    def check_entity_state(self, entity_id: str) -> bool:
        """
        Check if Home Assistant entity is 'on'.

        Args:
            entity_id: Entity ID to check

        Returns:
            True if entity state is 'on', False otherwise
        """
        try:
            url = f"{self.url}/api/states/{entity_id}"
            response = requests.get(url, headers=self.headers, timeout=5)
            response.raise_for_status()
            state = response.json().get("state", "").lower()
            return state == "on"
        except Exception as e:
            logger.error(f"Error checking HA entity {entity_id}: {e}")
            return False  # Default to off on error

    def increment_counter(self, entity_id: str) -> None:
        """
        Increment Home Assistant counter.

        Args:
            entity_id: Counter entity ID to increment
        """
        try:
            url = f"{self.url}/api/services/counter/increment"
            data = {"entity_id": entity_id}
            response = requests.post(url, headers=self.headers, json=data, timeout=5)
            response.raise_for_status()
            logger.info(f"Incremented counter {entity_id}")
        except Exception as e:
            logger.error(f"Error incrementing counter {entity_id}: {e}")

    def set_input_text(self, entity_id: str, value: str) -> None:
        """
        Set Home Assistant input_text value.

        Args:
            entity_id: Input text entity ID to update
            value: New value to set
        """
        try:
            url = f"{self.url}/api/services/input_text/set_value"
            data = {"entity_id": entity_id, "value": value}
            response = requests.post(url, headers=self.headers, json=data, timeout=5)
            response.raise_for_status()
            logger.info(f"Set {entity_id} to: {value}")
        except Exception as e:
            logger.error(f"Error setting {entity_id}: {e}")


if __name__ == "__main__":
    import os

    # Test with environment variables
    ha = HomeAssistant(
        os.getenv("HA_URL", "http://homeassistant.local:8123"),
        os.getenv("HA_TOKEN", ""),
    )
    entities = [os.getenv("HA_ANNOUNCE_ENTITY", "assist_satellite.living_room")]
    ha.speak("hello world", entities)
