#!/usr/bin/env python3
"""
Simple web server to handle Blue Iris motion detection webhooks
and process camera images with Google Gemini API.
"""

import os
import sys
import json
import logging
from datetime import datetime
from threading import Lock
from flask import Flask, request, jsonify
import requests
import google.generativeai as genai
from announce import ha_speak

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Rate limiting configuration
COOLDOWN_SECONDS = 30
last_processed = {}  # location -> timestamp
cooldown_lock = Lock()

# Get API key from environment
GEMINI_API_KEY = os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    logger.error("ERROR: GOOGLE_API_KEY or GEMINI_API_KEY environment variable not set")
    sys.exit(1)

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-exp')

# Load system prompt template
with open('system_prompt.txt', 'r') as f:
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
        # Create prompt from template
        prompt = SYSTEM_PROMPT_TEMPLATE.format(location=location)

        # Send to Gemini
        response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": image_data}])

        result = response.text.strip()
        return result
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

        # Update cooldown tracker
        update_last_processed(location)

        # Log the result
        logger.info(f"=== GEMINI RESPONSE for {location} ===")
        logger.info(f"{result}")
        logger.info(f"=" * 50)

        # Announce via Home Assistant if something detected
        if result.lower() != "none":
            # Prepend location to announcement for clarity
            announcement = f"{location}: {result}"
            logger.info("Announcing:")
            logger.info(announcement)
            ha_speak(announcement)

        return jsonify({
            "location": location,
            "result": result,
            "timestamp": datetime.now().isoformat()
        })

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
    app.run(host='0.0.0.0', port=5427, debug=False)
