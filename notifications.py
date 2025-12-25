"""External notification services (SMS, email, push, etc.)."""

import logging
import requests

logger = logging.getLogger(__name__)


class CallMeBotSMS:
    """CallMeBot WhatsApp SMS client."""

    def __init__(self, api_url: str, phone: str, api_key: str):
        """
        Initialize CallMeBot SMS client.

        Args:
            api_url: CallMeBot API URL
            phone: Phone number with country code (e.g., +1234567890)
            api_key: CallMeBot API key
        """
        self.api_url = api_url
        self.phone = phone
        self.api_key = api_key

    def send(self, message: str) -> None:
        """
        Send SMS via CallMeBot WhatsApp API.

        Args:
            message: Message to send
        """
        try:
            url = f"{self.api_url}?phone={self.phone}&text={requests.utils.quote(message)}&apikey={self.api_key}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            logger.info(f"SMS sent successfully")
        except Exception as e:
            logger.error(f"Error sending SMS: {e}")
