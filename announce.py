import json
import urllib.request
import urllib.error

def ha_speak(message: str, base_url: str, token: str, entity_id: str) -> None:
    """
    Sends a text message to Home Assistant to be spoken by the Assist Satellite.

    Args:
        message: The text message to speak
        base_url: Home Assistant URL (e.g., http://homeassistant.local:8123)
        token: Home Assistant long-lived access token
        entity_id: Assist Satellite entity ID (e.g., assist_satellite.living_room)
    """
    if not all([token, base_url, entity_id]):
        print("❌ Error: Missing required parameters (token, base_url, or entity_id).")
        return

    # 2. Prepare the Request
    # Handle trailing slashes in URL just in case
    endpoint = f"{base_url.rstrip('/')}/api/services/assist_satellite/announce"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "entity_id": entity_id,
        "message": message
    }

    # 3. Send via Standard Library (No pip install needed)
    try:
        req = urllib.request.Request(
            endpoint, 
            data=json.dumps(payload).encode('utf-8'), 
            headers=headers, 
            method='POST'
        )
        
        with urllib.request.urlopen(req) as response:
            # 200/201 are typical success codes for HA API
            if 200 <= response.status < 300:
                print(f"✅ HA Speaking: '{message}'")
            else:
                print(f"⚠️ API Error: Received status code {response.status}")
                
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP Error {e.code}: {e.read().decode('utf-8')}")
    except urllib.error.URLError as e:
        print(f"❌ Connection Error: {e.reason}")

# --- Example Usage ---
if __name__ == "__main__":
    import os
    # Test call - requires env vars for testing
    ha_speak(
        "Hello world",
        os.getenv("HA_URL", "http://homeassistant.local:8123"),
        os.getenv("HA_TOKEN", ""),
        os.getenv("HA_ANNOUNCE_ENTITY", "")
    )