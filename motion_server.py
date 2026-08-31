#!/usr/bin/env python3
"""
Simple web server to handle Blue Iris motion detection webhooks
and process camera images with Google Gemini API.
"""

import os
import re
import sys
import json
from urllib.parse import quote
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from threading import Lock, Timer
from string import Template
from collections import defaultdict
from flask import Flask, request, jsonify, send_file, Response, render_template_string
import requests
from requests.auth import HTTPDigestAuth
import yaml
from google import genai
from google.genai import types
from ha import HomeAssistant
from notifications import CallMeBotSMS
from rate_limiter import RateLimiter, ConsecutiveNoneTracker
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
HA_ANNOUNCE_ENTITY = ha_entities.get('announce')
HA_VOICE_ENTITY = ha_entities.get('voice_announcements')
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
SMS_DELAY_SECONDS = rate_limiting.get('sms_delay_seconds')
CONSECUTIVE_NONE_THRESHOLD = rate_limiting.get('consecutive_none_threshold')
NONE_DETECTION_WINDOW = rate_limiting.get('none_detection_window_seconds')
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

# Per-location pause after consecutive "None" results (both fields required to enable)
none_pause_enabled = bool(CONSECUTIVE_NONE_THRESHOLD and NONE_DETECTION_WINDOW)
none_trackers = defaultdict(
    lambda: ConsecutiveNoneTracker(CONSECUTIVE_NONE_THRESHOLD, NONE_DETECTION_WINDOW)
) if none_pause_enabled else None

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

def analyze_image(image_data, location, system_prompt=None):
    """Send image to Gemini for analysis"""
    try:
        prompt = system_prompt if system_prompt else SYSTEM_PROMPT_TEMPLATE.format(location=location)
        image_part = types.Part.from_bytes(data=image_data, mime_type='image/jpeg')

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, image_part]
        )

        return response.text.strip()
    except Exception as e:
        logger.error(f"Error analyzing image with Gemini: {e}")
        raise

def send_sms_if_not_home(announcement):
    """Delayed SMS callback - checks is_home after delay"""
    is_home = ha.check_entity_state(HA_HOME_OCCUPIED_ENTITY)
    if not is_home:
        if not sms_limiter or sms_limiter.check_and_update():
            logger.info(f"Sending delayed SMS: {announcement}")
            sms.send(announcement)
        else:
            logger.info("Skipping SMS - in global cooldown")
    else:
        logger.info("SMS skipped - user is home")

# Detection filename format written by GCSBackup.upload_image:
#   {YYYYMMDD}_{HHMMSS}_{location}_{sanitized_description}.jpg
# The raw location may contain spaces (e.g. "Front Door") but not underscores,
# so the first underscore after the timestamp is the area/description boundary.
DETECTION_NAME_RE = re.compile(r'^(\d{8})_(\d{6})_(.+)\.jpg$')


def parse_detection_name(name):
    """Parse a GCS detection object name into structured fields.

    Returns a dict {name, timestamp, area, description} or None if it doesn't
    match the expected detection filename format.
    """
    match = DETECTION_NAME_RE.match(name)
    if not match:
        return None

    date_str, time_str, rest = match.groups()
    try:
        ts = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None

    area, sep, desc = rest.partition('_')
    description = desc.replace('_', ' ').strip() if sep else ''

    return {
        "name": name,
        # Filenames are stamped with the server's clock, which runs UTC in the
        # container; mark the timestamp accordingly so clients convert to local.
        "timestamp": ts.isoformat() + "Z",
        "area": area,
        "description": description,
    }


EVENTS_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Camera Events</title>
<style>
  :root {
    --bg: #0f1115; --panel: #181b22; --panel-2: #1f232c;
    --border: #2a2f3a; --text: #e6e9ef; --muted: #97a0b0;
    --accent: #6ea8fe; --chip: #263041;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 5; background: var(--panel);
    border-bottom: 1px solid var(--border); padding: 14px 20px;
  }
  h1 { margin: 0 0 12px; font-size: 18px; font-weight: 600; }
  .controls { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: flex-end; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field label { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  input, select {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 10px; font-size: 14px; min-width: 150px;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  .status { margin-left: auto; color: var(--muted); font-size: 13px; }
  main { padding: 20px; }
  .grid {
    display: grid; gap: 16px;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    overflow: hidden; display: flex; flex-direction: column;
  }
  .card a { display: block; background: #000; aspect-ratio: 16 / 9; }
  .card img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .card .body { padding: 10px 12px 12px; }
  .card .desc { font-size: 14px; margin: 0 0 8px; }
  .card .meta { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .chip { background: var(--chip); color: var(--accent); border-radius: 999px; padding: 2px 10px; font-size: 12px; white-space: nowrap; }
  .time { color: var(--muted); font-size: 12px; }
  .empty { color: var(--muted); text-align: center; padding: 60px 20px; }
</style>
</head>
<body>
<header>
  <h1>Camera Events</h1>
  <div class="controls">
    <div class="field"><label for="range">Range</label><select id="range">
      <option value="0">All time</option>
      <option value="30">Past 30 days</option>
      <option value="7">Past 7 days</option>
      <option value="1">Past 24 hours</option>
    </select></div>
    <div class="field"><label for="area">Area</label><select id="area"><option value="">All areas</option></select></div>
    <div class="field"><label for="q">Filter text</label><input type="search" id="q" placeholder="e.g. white car, person"></div>
    <div class="status" id="status"></div>
  </div>
</header>
<main>
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" style="display:none">No detections match your filters.</div>
</main>
<script>
  const $ = (id) => document.getElementById(id);
  const grid = $('grid'), empty = $('empty'), status = $('status');
  const areaSel = $('area');
  let knownAreas = new Set();

  function fmtDate(d) { return d.toISOString().slice(0, 10); }
  function fmtTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  let timer = null;
  function debouncedLoad() { clearTimeout(timer); timer = setTimeout(load, 250); }

  async function load() {
    const params = new URLSearchParams();
    const days = parseInt($('range').value, 10);
    if (days > 0) {
      const start = new Date(Date.now() - (days - 1) * 86400000);
      params.set('start', fmtDate(start));
    }
    if (areaSel.value) params.set('area', areaSel.value);
    if ($('q').value.trim()) params.set('q', $('q').value.trim());
    status.textContent = 'Loading…';
    try {
      const res = await fetch('/api/events?' + params.toString());
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      render(data);
    } catch (e) {
      status.textContent = 'Error: ' + e.message;
      grid.innerHTML = '';
    }
  }

  function render(data) {
    const items = data.detections || [];
    // Grow the area dropdown as we discover areas.
    for (const d of items) {
      if (d.area && !knownAreas.has(d.area)) {
        knownAreas.add(d.area);
        const opt = document.createElement('option');
        opt.value = opt.textContent = d.area;
        areaSel.appendChild(opt);
      }
    }
    status.textContent = data.count + ' detection' + (data.count === 1 ? '' : 's')
      + (data.truncated ? ' (showing newest, refine filters for more)' : '');
    empty.style.display = items.length ? 'none' : 'block';
    grid.innerHTML = items.map(d => `
      <div class="card">
        <a href="${d.image_url}">
          <img loading="lazy" src="${d.image_url}" alt="">
        </a>
        <div class="body">
          <p class="desc">${escapeHtml(d.description) || '<span class="time">(no description)</span>'}</p>
          <div class="meta">
            <span class="chip">${escapeHtml(d.area)}</span>
            <span class="time">${fmtTime(d.timestamp)}</span>
          </div>
        </div>
      </div>`).join('');
  }

  function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  ['range', 'area'].forEach(id => $(id).addEventListener('change', load));
  $('q').addEventListener('input', debouncedLoad);
  load();
</script>
</body>
</html>"""


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
        system_prompt = data.get('system_prompt')

        # Skip if this location has gone quiet (N consecutive None results within the window)
        if not ignore_cooldown and none_trackers is not None and none_trackers[location].should_skip():
            logger.info(f"Skipping {location} - {CONSECUTIVE_NONE_THRESHOLD} consecutive None results within {NONE_DETECTION_WINDOW}s")
            return jsonify({
                "location": location,
                "result": "skipped_none_streak"
            })

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
            result = analyze_image(image_data, location, system_prompt)

            # Increment analysis counter if configured
            if HA_ANALYSIS_COUNTER:
                ha.increment_counter(HA_ANALYSIS_COUNTER)

            # Log the result
            logger.info(f"=== GEMINI RESPONSE for {location} ===")
            logger.info(f"{result}")
            logger.info(f"=" * 50)

            # Track consecutive None results per location (for the quiet-location pause)
            is_none = result.lower() == "none"
            if none_trackers is not None:
                if is_none:
                    none_trackers[location].record_none()
                else:
                    none_trackers[location].record_detection()

            # Announce via Home Assistant if something detected
            if not is_none:
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

                # Voice announcements (if announce entities configured)
                if HA_ANNOUNCE_ENTITY:
                    should_announce = not HA_VOICE_ENTITY or ha.check_entity_state(HA_VOICE_ENTITY)
                    if should_announce:
                        if not voice_limiter or voice_limiter.check_and_update():
                            logger.info(f"Voice announcement: {announcement}")
                            ha.speak(announcement, HA_ANNOUNCE_ENTITY)
                        else:
                            logger.info("Skipping voice announcement - in global cooldown")
                    else:
                        logger.info("Voice announcements disabled by entity")

                # Check if we should send SMS (when not home)
                if sms:
                    if SMS_DELAY_SECONDS:
                        logger.info(f"Scheduling SMS in {SMS_DELAY_SECONDS}s...")
                        Timer(SMS_DELAY_SECONDS, send_sms_if_not_home, [announcement]).start()
                    else:
                        send_sms_if_not_home(announcement)

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

@app.route('/api/events', methods=['GET'])
def api_events():
    """List parsed detection events from GCS, filtered by date range / area / text."""
    if not gcs:
        return jsonify({"error": "GCS is not configured"}), 503

    start = request.args.get('start')  # YYYY-MM-DD (inclusive)
    end = request.args.get('end')      # YYYY-MM-DD (inclusive)
    area_filter = request.args.get('area')
    query = (request.args.get('q') or '').strip().lower()
    try:
        limit = min(int(request.args.get('limit', 300)), 2000)
    except ValueError:
        limit = 300

    # Names sort lexicographically by their leading timestamp, so bound the
    # listing with date offsets. `end` is inclusive → use next-day prefix.
    start_offset = f"{start.replace('-', '')}_" if start else None
    end_offset = None
    if end:
        try:
            end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
            end_offset = f"{end_dt.strftime('%Y%m%d')}_"
        except ValueError:
            end_offset = None

    detections = []
    for name, _updated in gcs.list_blobs(start_offset, end_offset):
        parsed = parse_detection_name(name)
        if not parsed:
            continue
        if area_filter and parsed['area'] != area_filter:
            continue
        if query and query not in f"{parsed['area']} {parsed['description']}".lower():
            continue
        parsed['image_url'] = f"/events/image/{quote(name)}"
        detections.append(parsed)

    detections.sort(key=lambda d: d['name'], reverse=True)  # newest first
    truncated = len(detections) > limit

    return jsonify({
        "count": len(detections[:limit]),
        "truncated": truncated,
        "detections": detections[:limit],
    })


@app.route('/events/image/<path:blob_name>', methods=['GET'])
def event_image(blob_name):
    """Proxy a detection JPEG from GCS (validated to prevent arbitrary reads)."""
    if not gcs:
        return "Not found", 404
    if not DETECTION_NAME_RE.match(blob_name):
        return "Bad request", 400

    data = gcs.download_bytes(blob_name)
    if data is None:
        return "Not found", 404

    return Response(
        data,
        mimetype='image/jpeg',
        headers={'Cache-Control': 'public, max-age=31536000, immutable'},
    )


@app.route('/events', methods=['GET'])
def events_page():
    """Serve the single-page events viewer."""
    return render_template_string(EVENTS_HTML)


@app.route('/debug/<image_type>')
def debug_image(image_type):
    if not DEBUG_SAVE_IMAGES or image_type not in ('last_scan', 'last_detection'):
        return "Not found", 404
    path = f'config/{image_type}.jpg'
    return send_file(path) if os.path.exists(path) else ("Not found", 404)

if __name__ == '__main__':
    logger.info("Starting motion detection server...")
    logger.info(f"Gemini API configured: {'✓' if GEMINI_API_KEY else '✗'}")
    logger.info(f"Model: {GEMINI_MODEL}")
    from waitress import serve
    serve(
        app,
        host=config['server']['host'],
        port=config['server']['port']
    )
