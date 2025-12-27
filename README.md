# IP Camera Motion Detection AI Notifier

AI-powered server that handles webhooks from security cameras (or an NVR like BlueIris / ZoneMinder) when motion is detected, sends the camera image to an LLM using a system prompt, and sends notifications to Home Assistant and optionally via WhatsApp if interesting activity is detected with a description of the scene.

## Configuration

See [config.yaml](config/config.example.yaml) for configuration details. 

The config file supports `$VAR` syntax for environment variables.

### How I've configured it

1. Blue Iris detects motion and sends a webhook to this server
2. This server fetches the image from the camera URL and sends it to Gemini to analyze using [system_prompt.txt](system_prompt.txt), looking for activity in the main view
3. If activity detected: Announces via Home Assistant and sends me a WhatsApp message if I'm not home (configurable via Home Assistant entity)

## Requirements

- `GOOGLE_API_KEY` - Get at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
- `HA_TOKEN` - Home Assistant long-lived access token

## Using Docker Hub Image (Recommended)

### Quick Start with Docker Run

```bash
# 1. Download config example and edit it with your settings
mkdir -p camera-ai-config
curl -o camera-ai-config/config.yaml https://raw.githubusercontent.com/theak/camera-ai-alerts/refs/heads/main/config/config.example.yaml
# Edit camera-ai-config/config.yaml

# 2. Run container
docker run -d \
  --name motion-detection \
  -p 5427:5427 \
  -v $(pwd)/camera-ai-config:/app/config \
  -e GOOGLE_API_KEY=your_key_here \
  -e HA_TOKEN=your_token_here \
  --restart unless-stopped \
  akshaykannan/homeassistant-camera-ai:latest
```

### Quick Start with Docker Compose

```bash
# 1. Download compose file
curl -o docker-compose.yml https://raw.githubusercontent.com/theak/camera-ai-alerts/refs/heads/main/docker-compose.yml

# 2. Download config example and edit it with your settings
mkdir -p camera-ai-config
curl -o camera-ai-config/config.yaml https://raw.githubusercontent.com/theak/camera-ai-alerts/refs/heads/main/config/config.example.yaml
# Edit camera-ai-config/config.yaml

# 3. Set secrets and run
export GOOGLE_API_KEY=your_key_here
export HA_TOKEN=your_token_here
docker-compose up -d
```

### Webhook to analyze footage with LLM

**URL:**
```
http://your-server-ip:5427/motion
```

**POST Payload:**
```json
{
  "jpegUrl": "http://192.168.0.123/Streaming/channels/1/picture",
  "location": "backyard",
  "username": "admin",
  "password": "your_camera_password"
}
```

## Building from Source

Only needed if you want to modify the code or run without Docker.

```bash
# 1. Clone and enter directory
git clone https://github.com/theak/camera-ai-alerts.git
cd camera-ai-alerts

# 2. Copy example config
cp config/config.example.yaml config/config.yaml
nano config.yaml
# Edit config.yaml with your settings

# 3. Build and run
export GOOGLE_API_KEY=your_key_here
export HA_TOKEN=your_token_here

uv run --with-requirements requirements.txt motion_server.py
```

## Logs

Logs are written to both stdout (visible with `docker logs`) and to `motion_server.log` in the mounted config directory. Log files are automatically rotated at 10MB with 5 backup files kept.

## Blue Iris Configuration

### Setting Up Webhook

1. In Blue Iris, right-click on a camera and select "Camera properties"
2. Go to the "Alerts" tab
3. Click "On alert..." button
4. Add a "Web request or MQTT" action
5. [Configure the webhook](#webhook-to-analyze-footage-with-llm)
