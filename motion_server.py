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
from threading import Lock
from string import Template
from collections import defaultdict
from flask import Flask, request, jsonify
import requests
from requests.auth import HTTPDigestAuth
import yaml
from google import genai
from google.genai import types
from ha import HomeAssistant
from notifications import CallMeBotSMS
from rate_limiter import RateLimiter
from gcs_backup import GCSBackup

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
gemini = config['gemini']
ha_config = config['home_assistant']
ha_entities = ha_config['entities']
callmebot = config['callmebot']
rate_limiting = config['rate_limiting']

GEMINI_MODEL = gemini['model']
GEMINI_API_KEY = gemini['api_key']
HA_URL = ha_config['url']
HA_TOKEN = ha_config['token']
HA_ANNOUNCE_ENTITY = ha_entities['announce']
HA_VOICE_ENTITY = ha_entities['voice_announcements']
HA_HOME_OCCUPIED_ENTITY = ha_entities['home_occupied']
HA_ANALYSIS_COUNTER = ha_entities.get('analysis_counter')
HA_EVENT_COUNTER = ha_entities.get('event_counter')
HA_LAST_IMAGE_URL = ha_entities.get('last_image_url')
HA_LAST_EVENT_DESC = ha_entities.get('last_event_description')
CALLMEBOT_ENABLED = callmebot['enabled']
CALLMEBOT_API_URL = callmebot['api_url']
CALLMEBOT_PHONE = callmebot['phone']
CALLMEBOT_API_KEY = callmebot['api_key']
COOLDOWN_SECONDS = rate_limiting['cooldown_seconds']
VOICE_ANNOUNCEMENT_COOLDOWN = rate_limiting.get('voice_announcement_cooldown_seconds')
SMS_COOLDOWN = rate_limiting.get('sms_cooldown_seconds')
SYSTEM_PROMPT_FILE = config['system_prompt_file']
DEBUG_SAVE_IMAGES = config.get('debug', {}).get('save_images', False)
GCS_ENABLED = config.get('google_cloud_storage', {}).get('enabled', False)
GCS_BUCKET_NAME = config.get('google_cloud_storage', {}).get('bucket_name')
GCS_SERVICE_ACCOUNT_JSON = config.get('google_cloud_storage', {}).get('service_account_json')
GCS_BACKUP_CONTROL_ENTITY = config.get('google_cloud_storage', {}).get('backup_control_entity')

# Validate required secrets
if not GEMINI_API_KEY:
    logger.error("ERROR: GOOGLE_API_KEY environment variable not set")
    sys.exit(1)
if not HA_TOKEN:
    logger.error("ERROR: HA_TOKEN environment variable not set")
    sys.exit(1)

# Configure Gemini client
client = genai.Client(api_key=GEMINI_API_KEY)

# Initialize Home Assistant and notification clients
ha = HomeAssistant(HA_URL, HA_TOKEN)
sms = CallMeBotSMS(CALLMEBOT_API_URL, CALLMEBOT_PHONE, CALLMEBOT_API_KEY) if CALLMEBOT_ENABLED else None

# Initialize GCS backup client if enabled
gcs = None
if GCS_ENABLED:
    try:
        gcs = GCSBackup(GCS_BUCKET_NAME, GCS_SERVICE_ACCOUNT_JSON)
    except Exception as e:
        logger.error(f"Failed to initialize GCS backup: {e}")
        gcs = None

# Rate limiting
location_limiters = defaultdict(lambda: RateLimiter(COOLDOWN_SECONDS))
voice_limiter = RateLimiter(VOICE_ANNOUNCEMENT_COOLDOWN) if VOICE_ANNOUNCEMENT_COOLDOWN else None
sms_limiter = RateLimiter(SMS_COOLDOWN) if SMS_COOLDOWN else None

# In-flight request tracking (prevents thundering herd)
processing_locations = set()
processing_lock = Lock()

# Load system prompt template
with open(SYSTEM_PROMPT_FILE, 'r') as f:
    SYSTEM_PROMPT_TEMPLATE = f.read()

def fetch_image(url, username=None, password=None):
    """Fetch image from URL with optional auth (tries Basic, then Digest)"""
    try:
        if username and password:
            # Try Basic Auth first
            response = requests.get(url, timeout=10, auth=(username, password))

            # If 401, try Digest Auth
            if response.status_code == 401:
                logger.info(f"Basic auth failed, trying Digest auth for {url}")
                response = requests.get(url, timeout=10, auth=HTTPDigestAuth(username, password))

            response.raise_for_status()
        else:
            response = requests.get(url, timeout=10)
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
                except Exception:
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
        if not ignore_cooldown and not location_limiters[location].check_and_update():
            logger.info(f"Skipping {location} - in cooldown period")
            return jsonify({
                "location": location,
                "result": "skipped_cooldown"
            })

        # Check if already processing this location (prevents thundering herd)
        with processing_lock:
            if location in processing_locations:
                logger.info(f"Skipping {location} - already processing")
                return jsonify({
                    "location": location,
                    "result": "skipped_in_progress"
                })
            processing_locations.add(location)

        try:
            if not jpeg_url:
                error_msg = "Missing jpegUrl parameter"
                logger.error(error_msg)
                return jsonify({"error": error_msg}), 400

            # Fetch the image
            logger.info(f"Fetching image from {jpeg_url}")
            image_data = fetch_image(jpeg_url, username, password)

            # Save last scan image for debugging
            if DEBUG_SAVE_IMAGES:
                with open('config/last_scan.jpg', 'wb') as f:
                    f.write(image_data)

            # Analyze with Gemini
            logger.info(f"Analyzing image from {location} with Gemini...")
            result = analyze_image(image_data, location)

            # Increment analysis counter if configured
            if HA_ANALYSIS_COUNTER:
                ha.increment_counter(HA_ANALYSIS_COUNTER)

            # Log the result
            logger.info(f"=== GEMINI RESPONSE for {location} ===")
            logger.info(f"{result}")
            logger.info(f"=" * 50)

            # Announce via Home Assistant if something detected
            if result.lower() != "none":
                # Save last detection image for debugging
                if DEBUG_SAVE_IMAGES:
                    with open('config/last_detection.jpg', 'wb') as f:
                        f.write(image_data)

                # Prepend location to announcement for clarity
                announcement = f"{location}: {result}"

                # Backup to Google Cloud Storage if enabled (do this first to get GCS URL)
                gcs_image_url = None
                if gcs and GCS_BACKUP_CONTROL_ENTITY:
                    should_backup = ha.check_entity_state(GCS_BACKUP_CONTROL_ENTITY)
                    if should_backup:
                        logger.info("Backing up detection image to GCS...")
                        gcs_image_url = gcs.upload_image(image_data, location, result)
                    else:
                        logger.info("GCS backup disabled by HA entity")
                elif gcs:
                    # No control entity configured, always backup
                    logger.info("Backing up detection image to GCS...")
                    gcs_image_url = gcs.upload_image(image_data, location, result)

                # Update HA entities if configured
                if HA_EVENT_COUNTER:
                    ha.increment_counter(HA_EVENT_COUNTER)
                if HA_LAST_IMAGE_URL:
                    # Use GCS URL if available, otherwise use original jpeg_url
                    image_url = gcs_image_url if gcs_image_url else jpeg_url
                    ha.set_input_text(HA_LAST_IMAGE_URL, image_url)
                if HA_LAST_EVENT_DESC:
                    ha.set_input_text(HA_LAST_EVENT_DESC, announcement)

                # Check voice announcements control
                should_announce_voice = ha.check_entity_state(HA_VOICE_ENTITY)
                if should_announce_voice:
                    if not voice_limiter or voice_limiter.check_and_update():
                        logger.info(f"Voice announcement: {announcement}")
                        ha.speak(announcement, HA_ANNOUNCE_ENTITY)
                    else:
                        logger.info("Skipping voice announcement - in global cooldown")
                else:
                    logger.info("Voice announcements disabled")

                # Check if we should send SMS (when not home)
                is_home = ha.check_entity_state(HA_HOME_OCCUPIED_ENTITY)
                if sms and not is_home:
                    if not sms_limiter or sms_limiter.check_and_update():
                        logger.info(f"Sending SMS: {announcement}")
                        sms.send(announcement)
                    else:
                        logger.info("Skipping SMS - in global cooldown")
                else:
                    logger.info(f"SMS skipped (home={is_home}, enabled={sms is not None})")

            return jsonify({
                "location": location,
                "result": result
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
