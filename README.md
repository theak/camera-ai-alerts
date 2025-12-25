# Home Assistant Camera AI Motion Detection

AI-powered motion detection server that integrates Blue Iris security cameras with Home Assistant using Google Gemini. Automatically detects interesting activity in camera feeds and announces them via Home Assistant voice assistants or SMS.

## How It Works

1. Blue Iris detects motion and sends a webhook to the server
2. Server fetches the image from the camera URL (with auth if needed)
3. Gemini analyzes the image looking for people, dogs, or cats in the main view
4. Rate limiting check: Skips processing if same location was analyzed within 30 seconds
5. If detected: Announces via Home Assistant and/or sends SMS (based on input_boolean controls)

## Requirements

- `GOOGLE_API_KEY` - Get at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
- `HA_TOKEN` - Home Assistant long-lived access token

## Using Docker Hub Image (Recommended)

Pre-built multi-architecture images are available on Docker Hub supporting AMD64 and ARM64 (Apple Silicon).

### Quick Start with Docker Run

```bash
# 1. Create config file
curl -o config.yaml https://raw.githubusercontent.com/theak/camera-ai-alerts/refs/heads/main/config.example.yaml
# Edit config.yaml with your settings

# 2. Run container
docker run -d \
  --name motion-detection \
  -p 5427:5427 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -e GOOGLE_API_KEY=your_key_here \
  -e HA_TOKEN=your_token_here \
  --restart unless-stopped \
  akshaykannan/homeassistant-camera-ai:latest
```

### Quick Start with Docker Compose

```bash
# 1. Download compose file
curl -o docker-compose.yml https://raw.githubusercontent.com/theak/camera-ai-alerts/refs/heads/main/docker-compose.yml

# 2. Create config
curl -o config.yaml https://raw.githubusercontent.com/theak/camera-ai-alerts/refs/heads/main/config.example.yaml
# Edit config.yaml with your settings

# 3. Set secrets and run
export GOOGLE_API_KEY=your_key_here
export HA_TOKEN=your_token_here
docker-compose up -d
```

## Building from Source

Only needed if you want to modify the code or build locally.

```bash
# 1. Clone and enter directory
git clone https://github.com/theak/camera-ai-alerts.git
cd camera-ai-alerts

# 2. Copy and edit config
cp config.example.yaml config.yaml
# Edit config.yaml with your Home Assistant and CallMeBot settings

# 3. Build and run
export GOOGLE_API_KEY=your_key_here
export HA_TOKEN=your_token_here
docker-compose build
docker-compose up -d
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and customize it. The config file supports `$VAR` syntax for environment variables (secrets only).

Edit `system_prompt.txt` to customize AI detection behavior.

## Blue Iris Configuration

### Setting Up Webhook

1. In Blue Iris, right-click on a camera and select "Camera properties"
2. Go to the "Alerts" tab
3. Click "On alert..." button
4. Add a "Web request or MQTT" action
5. Configure the webhook:

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

## Development

### Running Without Docker

```bash
uv run --with-requirements requirements.txt motion_server.py
```
