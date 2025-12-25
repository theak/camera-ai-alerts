#!/usr/bin/env python3
"""
Simple web server to handle Blue Iris motion detection webhooks
and process camera images with Google Gemini API.
"""

import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from threading import Lock
from string import Template
from flask import Flask, request, jsonify
import requests
import yaml
from google import genai
from google.genai import types
from announce import ha_speak

# Configure logging
os.makedirs('config', exist_ok=True)
handlers = [
    logging.StreamHandler(sys.stdout),
    RotatingFileHandler(
        'config/motion_server.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
]
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Load configuration with environment variable interpolation
with open('config/config.yaml', 'r') as f:
    template = Template(f.read())
    config = yaml.safe_load(template.substitute(os.environ))

# Extract config values
GEMINI_MODEL = config['gemini']['model']
GEMINI_API_KEY = config['gemini']['api_key']
HA_URL = config['home_assistant']['url']
HA_TOKEN = config['home_assistant']['token']
HA_ANNOUNCE_ENTITY = config['home_assistant']['entities']['announce']
HA_VOICE_ENTITY = config['home_assistant']['entities']['voice_announcements']
HA_HOME_OCCUPIED_ENTITY = config['home_assistant']['entities']['home_occupied']
CALLMEBOT_ENABLED = config['callmebot']['enabled']
CALLMEBOT_API_URL = config['callmebot']['api_url']
CALLMEBOT_PHONE = config['callmebot']['phone']
CALLMEBOT_API_KEY = config['callmebot']['api_key']
COOLDOWN_SECONDS = config['rate_limiting']['cooldown_seconds']
SYSTEM_PROMPT_FILE = config['system_prompt_file']

# Validate required secrets
if not GEMINI_API_KEY:
    logger.error("ERROR: GOOGLE_API_KEY environment variable not set")
    sys.exit(1)
if not HA_TOKEN:
    logger.error("ERROR: HA_TOKEN environment variable not set")
    sys.exit(1)

# Configure Gemini client
client = genai.Client(api_key=GEMINI_API_KEY)

# Rate limiting configuration
last_processed = {}  # location -> timestamp
cooldown_lock = Lock()

# In-flight request tracking (prevents thundering herd)
processing_locations = set()
processing_lock = Lock()

# Load system prompt template
with open(SYSTEM_PROMPT_FILE, 'r') as f:
    SYSTEM_PROMPT_TEMPLATE = f.read()

def is_in_cooldown(location):
    """Check if location is within cooldown period"""
    with cooldown_lock:
        if location in last_processed:
            elapsed = (datetime.now() - last_processed[location]).total_seconds()
            return elapsed < COOLDOWN_SECONDS
        return False

def update_last_processed(location):
    """Update the last processed timestamp for a location"""
    with cooldown_lock:
        last_processed[location] = datetime.now()

def fetch_image(url, username=None, password=None):
    """Fetch image from URL with optional basic auth"""
    try:
        auth = None
        if username and password:
            auth = (username, password)
        response = requests.get(url, timeout=10, auth=auth)
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.error(f"Error fetching image from {url}: {e}")
        raise

def analyze_image(image_data, location):
    """Send image to Gemini for analysis"""
    try:
        prompt = SYSTEM_PROMPT_TEMPLATE.format(location=location)
        image_part = types.Part.from_bytes(data=image_data, mime_type='image/jpeg')

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, image_part]
        )

        return response.text.strip()
    except Exception as e:
        logger.error(f"Error analyzing image with Gemini: {e}")
        raise

def check_ha_entity_state(entity_id):
    """Check if Home Assistant entity is 'on'"""
    try:
        url = f"{HA_URL}/api/states/{entity_id}"
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json"
        }
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        state = response.json().get('state', '').lower()
        return state == 'on'
    except Exception as e:
        logger.error(f"Error checking HA entity {entity_id}: {e}")
        return False  # Default to off on error

def send_sms(message):
    """Send SMS via CallMeBot"""
    try:
        url = f"{CALLMEBOT_API_URL}?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        logger.info(f"SMS sent successfully")
    except Exception as e:
        logger.error(f"Error sending SMS: {e}")

@app.route('/motion', methods=['GET', 'POST'])
def handle_motion():
    """Handle motion detection webhook from Blue Iris"""
    try:
        # Parse request data
        if request.method == 'POST':
            if request.is_json:
                data = request.get_json()
            else:
                # Try to parse form data or raw body
                try:
                    data = json.loads(request.data.decode('utf-8'))
                except:
                    data = request.form.to_dict()
        else:  # GET
            data = request.args.to_dict()

        logger.info(f"Received motion request: {data}")

        # Extract jpegUrl and location
        jpeg_url = data.get('jpegUrl') or data.get('jpegurl')
        location = data.get('location', 'unknown')
        username = data.get('username')
        password = data.get('password')
        ignore_cooldown = data.get('ignoreCooldown', False)

        # Check cooldown (unless explicitly ignored)
        if not ignore_cooldown and is_in_cooldown(location):
            logger.info(f"Skipping {location} - in cooldown period")
            return jsonify({
                "location": location,
                "result": "skipped_cooldown",
                "timestamp": datetime.now().isoformat()
            })

        # Check if already processing this location (prevents thundering herd)
        with processing_lock:
            if location in processing_locations:
                logger.info(f"Skipping {location} - already processing")
                return jsonify({
                    "location": location,
                    "result": "skipped_in_progress",
                    "timestamp": datetime.now().isoformat()
                })
            processing_locations.add(location)

        # Update cooldown tracker immediately (before slow Gemini call)
        update_last_processed(location)

        try:
            if not jpeg_url:
                error_msg = "Missing jpegUrl parameter"
                logger.error(error_msg)
                return jsonify({"error": error_msg}), 400

            # Fetch the image
            logger.info(f"Fetching image from {jpeg_url}")
            image_data = fetch_image(jpeg_url, username, password)

            # Save the image to tmp.jpg for debugging
            with open('tmp.jpg', 'wb') as f:
                f.write(image_data)

            # Analyze with Gemini
            logger.info(f"Analyzing image from {location} with Gemini...")
            result = analyze_image(image_data, location)

            # Log the result
            logger.info(f"=== GEMINI RESPONSE for {location} ===")
            logger.info(f"{result}")
            logger.info(f"=" * 50)

            # Announce via Home Assistant if something detected
            if result.lower() != "none":
                # Prepend location to announcement for clarity
                announcement = f"{location}: {result}"

                # Check voice announcements control
                should_announce_voice = check_ha_entity_state(HA_VOICE_ENTITY)
                if should_announce_voice:
                    logger.info(f"Voice announcement: {announcement}")
                    ha_speak(announcement, HA_URL, HA_TOKEN, HA_ANNOUNCE_ENTITY)
                else:
                    logger.info("Voice announcements disabled")

                # Check if we should send SMS (when not home)
                is_home = check_ha_entity_state(HA_HOME_OCCUPIED_ENTITY)
                if CALLMEBOT_ENABLED and not is_home:
                    logger.info(f"Sending SMS: {announcement}")
                    send_sms(announcement)
                else:
                    logger.info(f"SMS skipped (home={is_home}, enabled={CALLMEBOT_ENABLED})")

            return jsonify({
                "location": location,
                "result": result,
                "timestamp": datetime.now().isoformat()
            })
        finally:
            # Clear in-flight tracking
            with processing_lock:
                processing_locations.discard(location)

    except Exception as e:
        logger.exception(f"Error processing motion request: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    logger.info("Starting motion detection server...")
    logger.info(f"Gemini API configured: {'✓' if GEMINI_API_KEY else '✗'}")
    logger.info(f"Model: {GEMINI_MODEL}")
    app.run(
        host=config['server']['host'],
        port=config['server']['port'],
        debug=False
    )
