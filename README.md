# MT40 Power Controller

Automated power scheduling and button control for Meraki MT40 Smart Power Controller using MT30 Automation Button.

This app runs on a HTTP server on the configured port (default 3001). Meraki webhooks require a HTTPS endpoint with a valid SSL certificate. You'll need a reverse proxy to handle the SSL termination and forward requests to the app.

## Features

- **Scheduled Power Control**: Automatically turn MT40 on/off at specific times
- **Button Control**: MT30 button triggers power on/off via webhooks
- **Web Admin UI**: Manage schedules, view events, and control power from a browser
- **Debug Mode**: Test without affecting the actual device
- **Monthly Log Rotation**: Automatic log compression on the 1st of each month

## Prerequisites

- Python 3.10+
- Meraki Dashboard API access
- Meraki MT40 Smart Power Controller
- Meraki MT30 Automation Button
- Public HTTPS endpoint for webhooks

## Installation

1. Clone the repo and create a virtual environment:
```bash
git clone https://github.com/epylko/mt40-control.git
cd mt40-control
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment variables:
```bash
cp .env.example .env
# Edit .env with your Meraki API key, org ID, network name, MT40 serial, etc.
```

## Configuration

### Environment Variables (.env)

```
MERAKI_API_KEY=your_api_key
MERAKI_ORG_ID=your_org_id
MERAKI_NETWORK_NAME=Your Network
MT40_SERIAL=XXXX-XXXX-XXXX
WEBHOOK_PORT=3001
WEBHOOK_HOST=0.0.0.0
UI_USERNAME=admin
UI_PASSWORD=your_password
DEBUG_MODE=
LONG_PRESS_TIMEOUT=20
```

### Meraki Dashboard Setup

Configure MT30 button automations in the Meraki Dashboard:

1. Navigate to: **Sensors > Configure > Automation**
2. Create a **Short Press** automation:
   - Trigger: Short Press (< 1 second)
   - Action: Webhook to your server's `/webhook` endpoint
3. Create a **Long Press** automation:
   - Trigger: Long Press (> 1 second)
   - Action: Webhook to your server's `/webhook` endpoint

## Usage

### Starting the Application

```bash
source venv/bin/activate
python3 mt40_controller.py
```

For production, use a process manager like pm2:
```bash
pm2 start mt40_controller.py --name mt40-controller --interpreter ./venv/bin/python3
pm2 save
pm2 startup
```

### Web Admin UI

Access the admin interface at `http://your-server:3001/admin`

From here you can:
- View current power status
- Manually turn power on/off
- Add, edit, and delete schedules
- Enable/disable debug mode
- View recent events

### Button Control

- **Short Press**: Powers ON the MT40
- **Double Long Press**: Powers OFF the MT40 (requires two long presses within the timeout period for safety)

### Debug Mode

Debug mode lets you test without affecting the actual MT40. Options:
- `manual` - Skip manual control actions
- `webhook` - Skip webhook-triggered actions
- `schedule` - Skip scheduled actions
- `all` - Skip all actions

Set via the web UI or the `DEBUG_MODE` environment variable.

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/webhook` | POST | No | Receives webhooks from MT30 |
| `/health` | GET | No | Health check |
| `/schedules` | GET | No | List active schedules |
| `/admin` | GET | Yes | Web admin UI |
| `/control/on` | POST | Yes | Power ON |
| `/control/off` | POST | Yes | Power OFF |
| `/api/schedules` | GET/POST | Yes | Get/save schedules |
| `/api/schedules/reload` | POST | Yes | Reload schedules |
| `/api/status` | GET | Yes | Current power status |
| `/api/events` | GET | Yes | Recent events |
| `/api/debug` | GET/POST | Yes | Get/set debug mode |

## Logs

Logs are written to `mt40_controller.log`. On the 1st of each month, the log is compressed to `mt40_controller.log.1.gz` and a fresh log is started.

## References

- [Meraki MT40 Documentation](https://documentation.meraki.com/MT/MT_General_Articles/MT40_Smart_Power_Controller_FAQ)
- [Meraki MT30 Automation Builder](https://documentation.meraki.com/MT/MT_General_Articles/MT_Automation_Builder)
- [Meraki Dashboard API Python](https://github.com/meraki/dashboard-api-python)
