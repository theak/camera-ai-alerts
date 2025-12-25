# Home Assistant Camera AI Motion Detection

AI-powered motion detection server that integrates Blue Iris security cameras with Home Assistant using Google Gemini. Automatically detects people and dogs in camera feeds and announces them via Home Assistant voice assistants.

## How It Works

1. **Blue Iris detects motion** and sends a webhook to the server
2. **Server fetches the image** from the camera URL (with auth if needed)
3. **Gemini analyzes** the image looking for people or dogs in the main view
4. **Rate limiting check**: Skips processing if same location was analyzed within 30 seconds
5. **If detected**: Announces via Home Assistant (e.g., "backyard: A person walking with a dog")

## Requirements

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `GOOGLE_API_KEY` | Google Gemini API key | `AIzaSy...` |
| `HA_URL` | Home Assistant URL | `http://homeassistant.local:8123` |
| `HA_TOKEN` | Home Assistant long-lived access token | `eyJhbG...` |
| `HA_ANNOUNCE_ENTITY` | Home Assistant Assist Satellite entity ID | `assist_satellite.living_room` |

## Quick Start

### Using Docker

```bash
docker run -d \
  -p 5427:5427 \
  -e GOOGLE_API_KEY=$GEMINI_API_KEY \
  -e HA_URL=$HA_URL \
  -e HA_TOKEN=$HA_TOKEN \
  -e HA_ANNOUNCE_ENTITY=$HA_ANNOUNCE_ENTITY \
  --name motion-detection \
  --restart unless-stopped \
  your-image-name
```

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
  "jpegUrl": "http://192.168.0.195/Streaming/channels/1/picture",
  "location": "backyard",
  "username": "admin",
  "password": "your_camera_password"
}
```

## Configuration

Edit `system_prompt.txt` to customize the AI detection behavior. Use `{location}` as a placeholder for the camera name.

### Debug Mode

The server automatically saves the last analyzed image to `tmp.jpg` in the working directory. This is useful for troubleshooting false positives/negatives.

## Development

### Running Without Docker

1. Set environment variables:
```bash
export GOOGLE_API_KEY=your_key
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=your_token
export HA_ANNOUNCE_ENTITY=assist_satellite.living_room
```

2. Run the server:
```bash
uv run --with-requirements requirements.txt motion_server.py
```

### Building Docker Image

```bash
docker build -t motion-detection .
```
