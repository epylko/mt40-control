#!/usr/bin/env python3
"""
MT40 Power Controller Application
- Receives webhooks from MT30 button presses
- Controls MT40 power state via Meraki API
- Runs scheduled power on/off based on cron-style schedule
"""

import meraki
import os
import json
import logging
import gzip
import shutil
from datetime import datetime, timedelta
from functools import wraps
from collections import deque
from flask import Flask, request, jsonify, Response, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv, set_key

# Load environment variables
load_dotenv()

# Configuration
API_KEY = os.getenv('MERAKI_API_KEY')
ORG_ID = os.getenv('MERAKI_ORG_ID')
NETWORK_NAME = os.getenv('MERAKI_NETWORK_NAME')
MT40_SERIAL = os.getenv('MT40_SERIAL')
WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', 3001))
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST', '0.0.0.0')
CONFIG_FILE = 'schedules.json'
DEBUG_MODE = os.getenv('DEBUG_MODE', '').lower()  # Options: 'webhook', 'schedule', 'all', or empty for no debug
UI_USERNAME = os.getenv('UI_USERNAME', 'admin')
UI_PASSWORD = os.getenv('UI_PASSWORD', 'admin')
LONG_PRESS_TIMEOUT = int(os.getenv('LONG_PRESS_TIMEOUT', 20))  # Seconds to wait for second press
MISFIRE_GRACE_TIME = int(os.getenv('MISFIRE_GRACE_TIME', 600))

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mt40_controller.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Initialize Meraki Dashboard API
dashboard = meraki.DashboardAPI(API_KEY, suppress_logging=True)

# Initialize scheduler with explicit timezone and generous misfire grace time
# misfire_grace_time: if a job fires late (e.g., after reboot + NTP sync delay), still run it
scheduler = BackgroundScheduler(
    timezone='America/New_York',
    job_defaults={'misfire_grace_time': MISFIRE_GRACE_TIME}
)
scheduler.start()

# Event history (keep last 100 events)
event_history = deque(maxlen=100)

# Runtime debug mode control (can be changed via API)
# Each action type can be independently set to debug mode
def parse_debug_mode(mode_str):
    """Parse DEBUG_MODE env var to individual toggles"""
    mode = mode_str.lower() if mode_str else ''
    return {
        'webhook': mode in ('webhook', 'all'),
        'schedule': mode in ('schedule', 'all'),
        'manual': mode in ('manual', 'all')
    }

debug_mode_state = parse_debug_mode(DEBUG_MODE)

# Long press confirmation tracking (for double press to turn off)
long_press_pending = {'timestamp': None, 'timeout_seconds': LONG_PRESS_TIMEOUT}


def require_auth(f):
    """Decorator for routes that require HTTP Basic Authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != UI_USERNAME or auth.password != UI_PASSWORD:
            return Response(
                'Authentication required',
                401,
                {'WWW-Authenticate': 'Basic realm="MT40 Controller"'}
            )
        return f(*args, **kwargs)
    return decorated


def get_network_id():
    """Get the network ID for the configured network name"""
    try:
        networks = dashboard.organizations.getOrganizationNetworks(ORG_ID)
        for network in networks:
            if network['name'] == NETWORK_NAME:
                return network['id']
        logger.error(f"Network '{NETWORK_NAME}' not found")
        return None
    except Exception as e:
        logger.error(f"Error getting network ID: {e}")
        return None


def control_mt40_power(action, source='unknown'):
    """
    Control MT40 downstream power state

    Args:
        action: 'on' or 'off'
        source: 'webhook', 'schedule', 'manual', or 'unknown'

    Returns:
        bool: True if successful, False otherwise
    """
    # Map friendly names to MT40 API operations
    operation_map = {
        'on': 'enableDownstreamPower',
        'off': 'disableDownstreamPower'
    }

    operation = operation_map.get(action)
    if not operation:
        logger.error(f"Invalid action: {action}. Must be 'on' or 'off'")
        return False

    # Check if this action should be skipped due to debug mode
    skip_action = debug_mode_state.get(source, False)

    if skip_action:
        logger.info(f"[DEBUG MODE - {source.upper()}] Would send {operation} command to MT40 ({MT40_SERIAL}), but skipping due to debug enabled for {source}")
        logger.info(f"[DEBUG MODE] MT40 power {action.upper()} - ACTION SKIPPED")
        # Log event
        event_history.append({
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'source': source,
            'status': 'debug_skipped'
        })
        return True  # Return True to indicate "successful" debug execution

    try:
        logger.info(f"Sending {operation} command to MT40 ({MT40_SERIAL})...")

        response = dashboard.sensor.createDeviceSensorCommand(
            MT40_SERIAL,
            operation=operation
        )

        command_id = response.get('commandId', 'unknown')
        status = response.get('status', 'unknown')
        logger.info(f"✓ MT40 power {action.upper()} command sent successfully (ID: {command_id}, Status: {status})")
        logger.debug(f"Full response: {response}")

        # Log if there are immediate errors
        if response.get('errors'):
            logger.warning(f"Command queued but has errors: {response.get('errors')}")

        # Log event
        event_history.append({
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'source': source,
            'status': 'success'
        })

        return True

    except meraki.exceptions.APIError as e:
        logger.error(f"Meraki API Error controlling MT40: {e}")
        # Log event
        event_history.append({
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'source': source,
            'status': 'failed',
            'error': str(e)
        })
        return False
    except Exception as e:
        logger.error(f"Error controlling MT40: {e}")
        # Log event
        event_history.append({
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'source': source,
            'status': 'failed',
            'error': str(e)
        })
        return False


def rotate_log():
    """Rotate the log file - compress and keep one backup"""
    log_file = 'mt40_controller.log'
    backup_file = 'mt40_controller.log.1.gz'

    try:
        if not os.path.exists(log_file):
            logger.info("Log rotation skipped - no log file exists")
            return

        logger.info("Starting monthly log rotation...")

        # Close the file handler
        for handler in logging.root.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()

        # Compress log to backup (overwrites previous backup)
        with open(log_file, 'rb') as f_in:
            with gzip.open(backup_file, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

        # Delete the original
        os.remove(log_file)

        # Reopen the handler (creates new empty file)
        for handler in logging.root.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.stream = open(log_file, 'a')

        logger.info("Log rotation complete")

    except Exception as e:
        logger.error(f"Error during log rotation: {e}")


def power_on(is_retry=False):
    """Turn MT40 power ON - scheduled function"""
    label = "⚡ Scheduled power ON retry" if is_retry else "⚡ Scheduled power ON triggered"
    logger.info(label)
    success = control_mt40_power('on', source='schedule')
    if not success and not is_retry:
        retry_time = datetime.now() + timedelta(minutes=2)
        scheduler.add_job(
            func=lambda: power_on(is_retry=True),
            trigger=DateTrigger(run_date=retry_time),
            id='retry_power_on',
            name='Retry Power ON',
            replace_existing=True
        )
        logger.warning(f"Power ON failed — retry scheduled for {retry_time.strftime('%H:%M:%S')}")


def power_off(is_retry=False):
    """Turn MT40 power OFF - scheduled function"""
    label = "⏻ Scheduled power OFF retry" if is_retry else "⏻ Scheduled power OFF triggered"
    logger.info(label)
    success = control_mt40_power('off', source='schedule')
    if not success and not is_retry:
        retry_time = datetime.now() + timedelta(minutes=2)
        scheduler.add_job(
            func=lambda: power_off(is_retry=True),
            trigger=DateTrigger(run_date=retry_time),
            id='retry_power_off',
            name='Retry Power OFF',
            replace_existing=True
        )
        logger.warning(f"Power OFF failed — retry scheduled for {retry_time.strftime('%H:%M:%S')}")


def load_schedules():
    """Load schedules from JSON config file and setup cron jobs"""
    try:
        if not os.path.exists(CONFIG_FILE):
            logger.warning(f"Config file '{CONFIG_FILE}' not found. No schedules loaded.")
            return

        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)

        schedules = config.get('schedules', [])

        # Clear existing jobs
        scheduler.remove_all_jobs()

        # Add new jobs
        for schedule in schedules:
            if not schedule.get('enabled', True):
                logger.info(f"Skipping disabled schedule: {schedule.get('name', 'Unnamed')}")
                continue

            name = schedule.get('name', 'Unnamed Schedule')
            action = schedule.get('action')  # 'on' or 'off'
            time_str = schedule.get('time')  # HH:MM format
            days = schedule.get('days', 'mon-fri')  # e.g., 'mon-fri', 'mon,wed,fri', 'daily'

            if not action or not time_str:
                logger.warning(f"Invalid schedule: {schedule}")
                continue

            # Parse time
            hour, minute = map(int, time_str.split(':'))

            # Parse days
            if days == 'daily':
                day_of_week = '*'
            elif days == 'mon-fri':
                day_of_week = 'mon-fri'
            elif days == 'weekends':
                day_of_week = 'sat,sun'
            else:
                day_of_week = days

            # Choose function based on action
            if action == 'on':
                func = power_on
                action_name = "Power ON"
            else:
                func = power_off
                action_name = "Power OFF"

            # Add cron job
            trigger = CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute
            )

            scheduler.add_job(
                func=func,
                trigger=trigger,
                id=f"schedule_{name}",
                name=f"{name} - {action_name}",
                replace_existing=True
            )

            logger.info(f"✓ Loaded schedule: '{name}' - {action_name} at {time_str} on {days}")

        logger.info(f"Total schedules loaded: {len(scheduler.get_jobs())}")

    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON config: {e}")
    except Exception as e:
        logger.error(f"Error loading schedules: {e}")

    # Add log rotation job (1st of each month at midnight)
    scheduler.add_job(
        func=rotate_log,
        trigger=CronTrigger(day=1, hour=0, minute=0),
        id='log_rotation',
        name='Monthly Log Rotation',
        replace_existing=True
    )
    logger.info("Log rotation scheduled for 1st of each month at midnight")


@app.route('/webhook', methods=['POST', 'GET'])
def webhook_handler():
    """Handle incoming webhooks from MT30 button"""
    # Handle GET requests (Meraki validation)
    if request.method == 'GET':
        logger.info("Webhook GET validation received")
        return "Webhook GET Received", 200

    try:
        # Check if body is empty
        if not request.data:
            logger.warning("Empty webhook body received")
            return "Webhook POST Received", 200

        data = request.get_json(force=True)

        # Extract trigger data (sensor automation structure)
        # Check both top-level and inside alertData for Meraki compatibility
        trigger = data.get('trigger', {})
        alert_data = data.get('alertData', {})

        # If trigger not at top level, check inside alertData
        if not trigger and alert_data:
            trigger = alert_data.get('trigger', {})

        metric = trigger.get('metric', '')
        button_data = trigger.get('button', {})
        press_type = button_data.get('pressType', '')

        # Also check alternative payload structures for compatibility
        alert_type = data.get('alertType', '')

        # Fallback: check automation message
        automation_message = alert_data.get('message', '').lower() if alert_data else ''
        if not automation_message:
            automation_message = data.get('automationMessage', '').lower()

        # Check if this is a button press event
        if metric == 'button' or 'button' in alert_type.lower():
            # Determine press type from multiple possible sources
            if press_type == 'short' or 'short' in automation_message:
                logger.info("🔘 SHORT PRESS detected - Turning MT40 ON")
                control_mt40_power('on', source='webhook')
                return "Webhook POST Received", 200

            elif press_type == 'long' or 'long' in automation_message:
                # Double long press confirmation for turning off
                now = datetime.now()
                last_press = long_press_pending['timestamp']
                timeout = long_press_pending['timeout_seconds']

                if last_press and (now - last_press).total_seconds() <= timeout:
                    # Second press within timeout - execute OFF
                    logger.info("🔘 SECOND LONG PRESS detected - Turning MT40 OFF")
                    long_press_pending['timestamp'] = None  # Clear pending
                    control_mt40_power('off', source='webhook')
                    return "Webhook POST Received", 200
                else:
                    # First press or timeout expired - wait for confirmation
                    logger.info(f"🔘 FIRST LONG PRESS detected - Press again within {timeout}s to turn OFF")
                    long_press_pending['timestamp'] = now
                    # Log this as a pending confirmation event
                    event_history.append({
                        'timestamp': now.isoformat(),
                        'action': 'off',
                        'source': 'webhook',
                        'status': 'pending_confirmation'
                    })
                    return "Webhook POST Received", 200

            else:
                logger.warning(f"Unknown button press type. Payload: {json.dumps(data, indent=2)}")
                return "Webhook POST Received", 200

        else:
            logger.info(f"Non-button event received. Alert type: {alert_type}, Metric: {metric}")
            return "Webhook POST Received", 200

    except Exception as e:
        logger.error(f"Error handling webhook: {e}", exc_info=True)
        return "No data received", 400


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'running',
        'timestamp': datetime.now().isoformat(),
        'schedules_active': len(scheduler.get_jobs())
    }), 200


@app.route('/schedules', methods=['GET'])
def list_schedules():
    """List all active schedules"""
    jobs = scheduler.get_jobs()
    schedule_list = []

    for job in jobs:
        schedule_list.append({
            'id': job.id,
            'name': job.name,
            'next_run': job.next_run_time.isoformat() if job.next_run_time else None
        })

    return jsonify({
        'schedules': schedule_list,
        'count': len(schedule_list)
    }), 200


@app.route('/control/<action>', methods=['POST'])
@require_auth
def manual_control(action):
    """Manual control endpoint for testing"""
    if action == 'on':
        result = control_mt40_power('on', source='manual')
        return jsonify({'status': 'success' if result else 'failed', 'action': 'power_on'}), 200
    elif action == 'off':
        result = control_mt40_power('off', source='manual')
        return jsonify({'status': 'success' if result else 'failed', 'action': 'power_off'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'Invalid action. Use "on" or "off"'}), 400


@app.route('/admin', methods=['GET'])
@require_auth
def admin_ui():
    """Serve the schedule management UI"""
    # Get server timezone
    server_tz = datetime.now().astimezone().tzname()

    html_template = '''
<!DOCTYPE html>
<html>
<head>
    <title>MT40 Schedule Manager</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            line-height: 1.6;
        }
        .container {
            max-width: 1000px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            position: relative;
        }
        .header-section {
            position: relative;
            margin-bottom: 30px;
        }
        .clock {
            position: absolute;
            top: 0;
            right: 0;
            font-size: 18px;
            font-weight: 600;
            color: #333;
            background: #f8f9fa;
            padding: 10px 20px;
            border-radius: 6px;
            border: 1px solid #ddd;
        }
        h1 {
            color: #333;
            margin-bottom: 10px;
        }
        .subtitle {
            color: #666;
            margin-bottom: 0;
        }
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            padding: 12px 20px;
            border-radius: 6px;
            display: none;
            z-index: 1000;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            max-width: 350px;
            animation: slideIn 0.3s ease;
        }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        .toast.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .toast.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .version {
            font-size: 12px;
            color: #999;
            margin-top: 5px;
        }
        .form-section {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 6px;
            margin-bottom: 30px;
        }
        .form-section h2 {
            margin-bottom: 15px;
            font-size: 18px;
            color: #333;
        }
        .form-group {
            margin-bottom: 15px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: 500;
            color: #333;
        }
        input[type="text"],
        input[type="time"],
        select {
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }
        input[type="checkbox"] {
            margin-right: 8px;
        }
        .form-row {
            display: grid;
            grid-template-columns: 2fr 1fr 1fr 1.5fr 0.5fr;
            gap: 10px;
            align-items: end;
        }
        button {
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            font-size: 14px;
            cursor: pointer;
            font-weight: 500;
        }
        .btn-primary {
            background: #007bff;
            color: white;
        }
        .btn-primary:hover {
            background: #0056b3;
        }
        .btn-danger {
            background: #dc3545;
            color: white;
            padding: 6px 12px;
            font-size: 12px;
        }
        .btn-danger:hover {
            background: #c82333;
        }
        .btn-secondary {
            background: #6c757d;
            color: white;
            padding: 6px 12px;
            font-size: 12px;
        }
        .btn-secondary:hover {
            background: #545b62;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        th {
            background: #f8f9fa;
            font-weight: 600;
            color: #333;
        }
        tr:hover {
            background: #f8f9fa;
        }
        .status-badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 3px;
            font-size: 12px;
            font-weight: 500;
        }
        .status-enabled {
            background: #d4edda;
            color: #155724;
        }
        .status-disabled {
            background: #f8d7da;
            color: #721c24;
        }
        .status-pending {
            background: #fff3cd;
            color: #856404;
        }
        .action-badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 3px;
            font-size: 12px;
            font-weight: 500;
        }
        .action-on {
            background: #d1ecf1;
            color: #0c5460;
        }
        .action-off {
            background: #f8d7da;
            color: #721c24;
        }
        .actions {
            display: flex;
            gap: 8px;
        }
        .power-control-bar {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
            margin-bottom: 25px;
            padding: 12px 20px;
            background: #f8f9fa;
            border-radius: 6px;
        }
        .power-control-row {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 15px;
        }
        .power-control-bar .label {
            font-weight: 500;
            color: #555;
        }
        .debug-status-line {
            font-size: 13px;
            color: #856404;
            background: rgba(255, 193, 7, 0.2);
            padding: 4px 12px;
            border-radius: 4px;
        }
        .power-status {
            display: inline-block;
            padding: 6px 16px;
            border-radius: 16px;
            font-weight: 600;
            font-size: 14px;
            min-width: 70px;
            text-align: center;
        }
        .power-status.on {
            background: #28a745;
            color: white;
        }
        .power-status.off {
            background: #dc3545;
            color: white;
        }
        .power-status.unknown {
            background: #6c757d;
            color: white;
        }
        .btn-control {
            padding: 8px 20px;
            font-size: 14px;
            font-weight: 500;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-control:hover {
            transform: translateY(-1px);
            box-shadow: 0 2px 6px rgba(0,0,0,0.15);
        }
        .btn-power-on {
            background: #28a745;
            color: white;
        }
        .btn-power-on:hover {
            background: #218838;
        }
        .btn-power-off {
            background: #dc3545;
            color: white;
        }
        .btn-power-off:hover {
            background: #c82333;
        }
        #debugSection {
            border: 2px solid #ffc107;
            background: #fff3cd;
        }
        .debug-toggle-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 16px;
            background: white;
            border-radius: 6px;
            margin-bottom: 10px;
            border: 1px solid #ddd;
        }
        .debug-toggle-row:last-child {
            margin-bottom: 0;
        }
        .debug-toggle-label {
            font-weight: 500;
            color: #333;
        }
        .toggle-switch {
            position: relative;
            width: 50px;
            height: 26px;
        }
        .toggle-switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: #ccc;
            transition: 0.3s;
            border-radius: 26px;
        }
        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 20px;
            width: 20px;
            left: 3px;
            bottom: 3px;
            background-color: white;
            transition: 0.3s;
            border-radius: 50%;
        }
        .toggle-switch input:checked + .toggle-slider {
            background-color: #ffc107;
        }
        .toggle-switch input:checked + .toggle-slider:before {
            transform: translateX(24px);
        }
        .debug-note {
            color: #856404;
            font-size: 0.9em;
            margin-top: 15px;
            padding: 10px 12px;
            background: rgba(255, 193, 7, 0.2);
            border-radius: 4px;
            border-left: 3px solid #ffc107;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-section">
            <div id="clock" class="clock">--:--:--</div>
            <h1>MT40 Schedule Manager</h1>
            <p class="subtitle">Manage power on/off schedules</p>
            <p class="version">v1.3.0</p>
        </div>

        <div id="toast" class="toast"></div>

        <div class="power-control-bar">
            <div class="power-control-row">
                <span class="label">Power Status:</span>
                <span id="powerStatus" class="power-status unknown">...</span>
                <span class="label" style="margin-left: 10px;">Set Power:</span>
                <button class="btn-control btn-power-on" onclick="manualPowerControl('on')">ON</button>
                <button class="btn-control btn-power-off" onclick="manualPowerControl('off')">OFF</button>
            </div>
            <div class="power-control-row" style="margin-top: 8px;">
                <span class="label">Misfire Grace:</span>
                <input type="number" id="misfireGraceTime" min="30" max="3600" style="width:70px; padding:4px 8px; border:1px solid #ccc; border-radius:4px; font-size:14px;">
                <span class="label">sec</span>
                <button class="btn-control" style="background:#6c757d; color:white;" onclick="saveMisfireGraceTime()">Save</button>
            </div>
            <div id="debugStatusLine" class="debug-status-line">Debugging: None</div>
        </div>

        <div class="form-section">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <h2 style="margin:0">Current Schedules</h2>
                <button class="btn-primary" onclick="addInlineRow()">+ Add</button>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Action</th>
                        <th>Time</th>
                        <th>Days</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="scheduleTable">
                    <tr>
                        <td colspan="6" style="text-align: center; color: #999;">Loading...</td>
                    </tr>
                </tbody>
            </table>
        </div>


        <div class="form-section">
            <h2>Recent Events</h2>
            <table>
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>Action</th>
                        <th>Source</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="eventsTable">
                    <tr>
                        <td colspan="4" style="text-align: center; color: #999;">Loading...</td>
                    </tr>
                </tbody>
            </table>
        </div>

        <div class="form-section" id="debugSection">
            <h2>Debug Mode</h2>
            <p style="color: #666; margin-bottom: 15px;">Test power commands without affecting the actual device</p>

            <div class="debug-toggle-row">
                <span class="debug-toggle-label">Manual (UI Buttons)</span>
                <label class="toggle-switch">
                    <input type="checkbox" id="debugManual" onchange="updateDebugMode('manual', this.checked)">
                    <span class="toggle-slider"></span>
                </label>
            </div>

            <div class="debug-toggle-row">
                <span class="debug-toggle-label">Webhook (MT30 Button)</span>
                <label class="toggle-switch">
                    <input type="checkbox" id="debugWebhook" onchange="updateDebugMode('webhook', this.checked)">
                    <span class="toggle-slider"></span>
                </label>
            </div>

            <div class="debug-toggle-row">
                <span class="debug-toggle-label">Schedule (Automated)</span>
                <label class="toggle-switch">
                    <input type="checkbox" id="debugSchedule" onchange="updateDebugMode('schedule', this.checked)">
                    <span class="toggle-slider"></span>
                </label>
            </div>

            <div class="debug-note">
                Enabling debug for an action prevents it from changing the power status.
            </div>
        </div>
    </div>

    <script>
        let schedules = [];

        function showMessage(text, type) {
            const toast = document.getElementById('toast');
            toast.textContent = text;
            toast.className = 'toast ' + type;
            toast.style.display = 'block';
            setTimeout(() => {
                toast.style.display = 'none';
            }, 3000);
        }

        async function manualPowerControl(action) {
            try {
                const response = await fetch(`/control/${action}`, {
                    method: 'POST'
                });

                if (!response.ok) throw new Error(`Failed to ${action} power`);

                const data = await response.json();
                if (data.status === 'success') {
                    showMessage(`Power ${action.toUpperCase()} command sent successfully`, 'success');
                    // Refresh events and status to show the new action
                    setTimeout(() => {
                        loadEvents();
                        loadPowerStatus();
                    }, 1000);
                } else {
                    showMessage(`Failed to ${action} power`, 'error');
                }
            } catch (error) {
                showMessage(`Error: ${error.message}`, 'error');
            }
        }

        async function loadPowerStatus() {
            try {
                const response = await fetch('/api/status');
                if (!response.ok) throw new Error('Failed to load status');
                const data = await response.json();

                const statusEl = document.getElementById('powerStatus');
                const state = data.power_state;

                // Remove all state classes
                statusEl.className = 'power-status';

                if (state === 'on') {
                    statusEl.classList.add('on');
                    statusEl.textContent = 'ON';
                } else if (state === 'off') {
                    statusEl.classList.add('off');
                    statusEl.textContent = 'OFF';
                } else {
                    statusEl.classList.add('unknown');
                    statusEl.textContent = '?';
                }
            } catch (error) {
                console.error('Error loading power status:', error);
            }
        }

        async function loadDebugMode() {
            try {
                const response = await fetch('/api/debug');
                if (!response.ok) throw new Error('Failed to load debug mode');
                const data = await response.json();

                // Update toggle states
                document.getElementById('debugManual').checked = data.manual;
                document.getElementById('debugWebhook').checked = data.webhook;
                document.getElementById('debugSchedule').checked = data.schedule;

                // Update debug status line in power control bar
                updateDebugStatusLine(data);
            } catch (error) {
                console.error('Error loading debug mode:', error);
            }
        }

        function updateDebugStatusLine(data) {
            const statusLine = document.getElementById('debugStatusLine');
            const enabled = [];
            if (data.manual) enabled.push('Manual');
            if (data.webhook) enabled.push('Webhook');
            if (data.schedule) enabled.push('Schedule');

            if (enabled.length > 0) {
                statusLine.textContent = 'Debugging: ' + enabled.join(', ');
            } else {
                statusLine.textContent = 'Debugging: None';
            }
            statusLine.style.display = 'block';
        }

        async function updateDebugMode(action, enabled) {
            try {
                const response = await fetch('/api/debug', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ action: action, enabled: enabled })
                });

                if (!response.ok) throw new Error('Failed to update debug mode');

                const actionLabels = {
                    'manual': 'Manual',
                    'webhook': 'Webhook',
                    'schedule': 'Schedule'
                };

                if (enabled) {
                    showMessage(`Debug enabled for ${actionLabels[action]} actions`, 'success');
                } else {
                    showMessage(`Debug disabled for ${actionLabels[action]} actions`, 'success');
                }

                // Update the status line with current checkbox states
                updateDebugStatusLine({
                    manual: document.getElementById('debugManual').checked,
                    webhook: document.getElementById('debugWebhook').checked,
                    schedule: document.getElementById('debugSchedule').checked
                });
            } catch (error) {
                showMessage(`Error updating debug mode: ${error.message}`, 'error');
                // Reload to restore correct state
                await loadDebugMode();
            }
        }

        async function loadSettings() {
            try {
                const response = await fetch('/api/settings');
                if (!response.ok) throw new Error('Failed to load settings');
                const data = await response.json();
                document.getElementById('misfireGraceTime').value = data.misfire_grace_time;
            } catch (error) {
                console.error('Error loading settings:', error);
            }
        }

        async function saveMisfireGraceTime() {
            const value = parseInt(document.getElementById('misfireGraceTime').value);
            if (isNaN(value) || value < 30 || value > 3600) {
                showMessage('Grace time must be between 30 and 3600 seconds', 'error');
                return;
            }
            try {
                const response = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ misfire_grace_time: value })
                });
                if (!response.ok) throw new Error('Failed to save settings');
                showMessage(`Misfire grace time set to ${value}s`, 'success');
            } catch (error) {
                showMessage('Error saving settings: ' + error.message, 'error');
            }
        }

        function timeToMinutes(timeStr) {
            const [hours, minutes] = timeStr.split(':').map(Number);
            return hours * 60 + minutes;
        }

        function sortSchedules(scheduleList) {
            return scheduleList.sort((a, b) => {
                return timeToMinutes(a.time) - timeToMinutes(b.time);
            });
        }

        async function loadSchedules() {
            try {
                const response = await fetch('/api/schedules');
                if (!response.ok) throw new Error('Failed to load schedules');
                const data = await response.json();
                schedules = data.schedules || [];
                renderSchedules();
            } catch (error) {
                showMessage('Error loading schedules: ' + error.message, 'error');
            }
        }

        function renderSchedules() {
            const tbody = document.getElementById('scheduleTable');

            if (schedules.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #999;">No schedules configured</td></tr>';
                return;
            }

            tbody.innerHTML = schedules.map((schedule, index) => `
                <tr>
                    <td>${schedule.name}</td>
                    <td><span class="action-badge action-${schedule.action}">${schedule.action.toUpperCase()}</span></td>
                    <td>${schedule.time}</td>
                    <td>${schedule.days}</td>
                    <td><span class="status-badge status-${schedule.enabled ? 'enabled' : 'disabled'}">${schedule.enabled ? 'Enabled' : 'Disabled'}</span></td>
                    <td class="actions">
                        <button class="btn-secondary" onclick="editSchedule(${index})">Edit</button>
                        <button class="btn-secondary" onclick="toggleSchedule(${index})">${schedule.enabled ? 'Disable' : 'Enable'}</button>
                        <button class="btn-danger" onclick="deleteSchedule(${index})">Delete</button>
                    </td>
                </tr>
            `).join('');
        }

        async function saveSchedules() {
            try {
                // Sort schedules by time before saving
                schedules = sortSchedules(schedules);

                const response = await fetch('/api/schedules', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ schedules: schedules })
                });

                if (!response.ok) throw new Error('Failed to save schedules');

                // Reload the scheduler
                const reloadResponse = await fetch('/api/schedules/reload', {
                    method: 'POST'
                });

                if (!reloadResponse.ok) throw new Error('Failed to reload schedules');

                renderSchedules();
                showMessage('Schedules saved and reloaded successfully', 'success');
            } catch (error) {
                showMessage('Error saving schedules: ' + error.message, 'error');
            }
        }

        function esc(str) {
            return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }

        function daysOptions(selected) {
            const opts = [
                ['daily','Daily'],['mon-fri','Weekdays (Mon-Fri)'],['weekends','Weekends (Sat-Sun)'],
                ['mon','Monday'],['tue','Tuesday'],['wed','Wednesday'],['thu','Thursday'],
                ['fri','Friday'],['sat','Saturday'],['sun','Sunday']
            ];
            return opts.map(([v,l]) => `<option value="${v}"${v===selected?' selected':''}>${l}</option>`).join('');
        }

        function editSchedule(index) {
            const s = schedules[index];
            const tbody = document.getElementById('scheduleTable');
            const rows = tbody.querySelectorAll('tr');
            rows[index].innerHTML = `
                <td><input type="text" value="${esc(s.name)}" id="edit_name_${index}" style="width:100%"></td>
                <td><select id="edit_action_${index}">
                    <option value="on"${s.action==='on'?' selected':''}>ON</option>
                    <option value="off"${s.action==='off'?' selected':''}>OFF</option>
                </select></td>
                <td><input type="time" value="${esc(s.time)}" id="edit_time_${index}"></td>
                <td><select id="edit_days_${index}">${daysOptions(s.days)}</select></td>
                <td><span class="status-badge status-${s.enabled ? 'enabled' : 'disabled'}">${s.enabled ? 'Enabled' : 'Disabled'}</span></td>
                <td class="actions">
                    <button class="btn-primary" onclick="saveInlineEdit(${index})">Save</button>
                    <button class="btn-secondary" onclick="renderSchedules()">Cancel</button>
                </td>
            `;
            document.getElementById(`edit_name_${index}`).focus();
        }

        async function saveInlineEdit(index) {
            schedules[index] = {
                ...schedules[index],
                name: document.getElementById(`edit_name_${index}`).value,
                action: document.getElementById(`edit_action_${index}`).value,
                time: document.getElementById(`edit_time_${index}`).value,
                days: document.getElementById(`edit_days_${index}`).value,
            };
            await saveSchedules();
        }

        function addInlineRow() {
            const tbody = document.getElementById('scheduleTable');
            if (tbody.querySelector('tr[data-new]')) return;
            const emptyRow = tbody.querySelector('tr td[colspan]');
            if (emptyRow) emptyRow.parentElement.remove();
            const tr = document.createElement('tr');
            tr.setAttribute('data-new', '1');
            tr.innerHTML = `
                <td><input type="text" id="new_name" style="width:100%" placeholder="e.g., Morning ON"></td>
                <td><select id="new_action">
                    <option value="on">ON</option>
                    <option value="off">OFF</option>
                </select></td>
                <td><input type="time" id="new_time"></td>
                <td><select id="new_days">${daysOptions('mon-fri')}</select></td>
                <td></td>
                <td class="actions">
                    <button class="btn-primary" onclick="saveNewInline()">Save</button>
                    <button class="btn-secondary" onclick="cancelNewInline()">Cancel</button>
                </td>
            `;
            tbody.appendChild(tr);
            document.getElementById('new_name').focus();
        }

        function cancelNewInline() {
            const tr = document.querySelector('tr[data-new]');
            if (tr) tr.remove();
            if (schedules.length === 0) renderSchedules();
        }

        async function saveNewInline() {
            const name = document.getElementById('new_name').value.trim();
            const time = document.getElementById('new_time').value;
            if (!name || !time) {
                showMessage('Name and time are required', 'error');
                return;
            }
            schedules.push({
                name,
                action: document.getElementById('new_action').value,
                time,
                days: document.getElementById('new_days').value,
                enabled: true
            });
            await saveSchedules();
        }

        async function toggleSchedule(index) {
            schedules[index].enabled = !schedules[index].enabled;
            await saveSchedules();
        }

        async function deleteSchedule(index) {
            if (confirm('Are you sure you want to delete this schedule?')) {
                schedules.splice(index, 1);
                await saveSchedules();
            }
        }

        async function loadEvents() {
            try {
                const response = await fetch('/api/events');
                if (!response.ok) throw new Error('Failed to load events');
                const data = await response.json();
                renderEvents(data.events || []);
            } catch (error) {
                console.error('Error loading events:', error);
            }
        }

        function formatTimestamp(isoString) {
            const date = new Date(isoString);
            return date.toLocaleString();
        }

        function renderEvents(events) {
            const tbody = document.getElementById('eventsTable');

            if (events.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #999;">No events recorded</td></tr>';
                return;
            }

            tbody.innerHTML = events.slice(0, 20).map(event => {
                let statusClass = '';
                let statusText = event.status;

                if (event.status === 'success') {
                    statusClass = 'status-enabled';
                    statusText = 'Success';
                } else if (event.status === 'failed') {
                    statusClass = 'status-disabled';
                    statusText = event.error ? `Failed: ${event.error}` : 'Failed';
                } else if (event.status === 'debug_skipped') {
                    statusClass = 'status-disabled';
                    statusText = 'Debug Skipped';
                } else if (event.status === 'pending_confirmation') {
                    statusClass = 'status-pending';
                    statusText = 'Pending Confirmation';
                }

                return `
                    <tr>
                        <td>${formatTimestamp(event.timestamp)}</td>
                        <td><span class="action-badge action-${event.action}">${event.action.toUpperCase()}</span></td>
                        <td>${event.source}</td>
                        <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                    </tr>
                `;
            }).join('');
        }

        // Update clock display
        function updateClock() {
            const now = new Date();
            const hours = String(now.getHours()).padStart(2, '0');
            const minutes = String(now.getMinutes()).padStart(2, '0');
            const seconds = String(now.getSeconds()).padStart(2, '0');
            const timezone = '__SERVER_TIMEZONE__';
            document.getElementById('clock').textContent = `${hours}:${minutes}:${seconds} ${timezone}`;
        }

        // Load schedules, events, status, and debug mode on page load
        loadSchedules();
        loadEvents();
        loadPowerStatus();
        loadDebugMode();
        loadSettings();
        updateClock(); // Initial clock update

        // Auto-refresh events and status every 10 seconds
        setInterval(() => {
            loadEvents();
            loadPowerStatus();
        }, 10000);

        // Update clock every second
        setInterval(updateClock, 1000);
    </script>
</body>
</html>
    '''
    # Replace timezone placeholder with server timezone
    html_template = html_template.replace('__SERVER_TIMEZONE__', server_tz)
    return render_template_string(html_template)


@app.route('/api/schedules', methods=['GET'])
@require_auth
def get_schedules_api():
    """Get current schedules from JSON file"""
    try:
        if not os.path.exists(CONFIG_FILE):
            return jsonify({'schedules': []}), 200

        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)

        return jsonify(config), 200
    except Exception as e:
        logger.error(f"Error reading schedules: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedules', methods=['POST'])
@require_auth
def save_schedules_api():
    """Save schedules to JSON file"""
    try:
        data = request.get_json()

        if 'schedules' not in data:
            return jsonify({'error': 'Missing schedules field'}), 400

        # Write to file
        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)

        logger.info(f"Schedules saved to {CONFIG_FILE}")
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        logger.error(f"Error saving schedules: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedules/reload', methods=['POST'])
@require_auth
def reload_schedules_api():
    """Reload schedules from JSON file"""
    try:
        load_schedules()
        logger.info("Schedules reloaded from web UI")
        return jsonify({'status': 'success', 'message': 'Schedules reloaded'}), 200
    except Exception as e:
        logger.error(f"Error reloading schedules: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/events', methods=['GET'])
@require_auth
def get_events_api():
    """Get recent power control events"""
    try:
        # Return events in reverse chronological order (newest first)
        events = list(reversed(event_history))
        return jsonify({'events': events, 'count': len(events)}), 200
    except Exception as e:
        logger.error(f"Error retrieving events: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/status', methods=['GET'])
@require_auth
def get_status_api():
    """Get current MT40 power status"""
    try:
        # Query the actual current state from the dashboard
        readings = dashboard.sensor.getOrganizationSensorReadingsLatest(
            ORG_ID,
            serials=[MT40_SERIAL],
            metrics=['downstreamPower']
        )

        current_state = 'unknown'
        last_update = None

        for reading in readings:
            if reading.get('serial') == MT40_SERIAL:
                downstream_power = reading.get('readings', [])
                for r in downstream_power:
                    if r.get('metric') == 'downstreamPower':
                        enabled = r.get('downstreamPower', {}).get('enabled')
                        if enabled is not None:
                            current_state = 'on' if enabled else 'off'
                        last_update = r.get('ts')
                        break
                break

        return jsonify({
            'power_state': current_state,
            'last_update': last_update
        }), 200
    except Exception as e:
        logger.error(f"Error retrieving status: {e}")
        return jsonify({'error': str(e), 'power_state': 'unknown'}), 500


@app.route('/api/debug', methods=['GET'])
@require_auth
def get_debug_mode_api():
    """Get current debug mode settings for all action types"""
    return jsonify({
        'webhook': debug_mode_state.get('webhook', False),
        'schedule': debug_mode_state.get('schedule', False),
        'manual': debug_mode_state.get('manual', False)
    }), 200


@app.route('/api/debug', methods=['POST'])
@require_auth
def set_debug_mode_api():
    """Set debug mode for a specific action type"""
    try:
        data = request.get_json()
        action = data.get('action', '').lower()
        enabled = data.get('enabled', False)

        # Validate action
        valid_actions = ['webhook', 'schedule', 'manual']
        if action not in valid_actions:
            return jsonify({
                'error': f'Invalid action. Must be one of: {", ".join(valid_actions)}'
            }), 400

        debug_mode_state[action] = bool(enabled)
        logger.info(f"Debug mode for '{action}' set to: {enabled}")

        return jsonify({
            'status': 'success',
            'action': action,
            'enabled': debug_mode_state[action]
        }), 200
    except Exception as e:
        logger.error(f"Error setting debug mode: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings', methods=['GET'])
@require_auth
def get_settings_api():
    """Get current scheduler settings"""
    return jsonify({'misfire_grace_time': MISFIRE_GRACE_TIME}), 200


@app.route('/api/settings', methods=['POST'])
@require_auth
def set_settings_api():
    """Update scheduler settings and persist to .env"""
    global MISFIRE_GRACE_TIME
    try:
        data = request.get_json()
        value = int(data.get('misfire_grace_time', MISFIRE_GRACE_TIME))
        if value < 30 or value > 3600:
            return jsonify({'error': 'misfire_grace_time must be between 30 and 3600'}), 400

        MISFIRE_GRACE_TIME = value
        set_key(ENV_FILE, 'MISFIRE_GRACE_TIME', str(value))

        for job in scheduler.get_jobs():
            job.modify(misfire_grace_time=value)

        logger.info(f"Misfire grace time updated to {value}s")
        return jsonify({'misfire_grace_time': MISFIRE_GRACE_TIME}), 200
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid value'}), 400
    except Exception as e:
        logger.error(f"Error updating settings: {e}")
        return jsonify({'error': str(e)}), 500


def main():
    """Main application entry point"""
    # Check for default credentials
    if UI_USERNAME == 'admin' and UI_PASSWORD == 'change_me':
        logger.error("="*60)
        logger.error("SECURITY ERROR: Default credentials detected!")
        logger.error("Please change UI_USERNAME and UI_PASSWORD in .env")
        logger.error("="*60)
        return

    logger.info("="*60)
    logger.info("MT40 Power Controller Starting...")
    logger.info("="*60)
    logger.info(f"MT40 Serial: {MT40_SERIAL}")
    logger.info(f"Network: {NETWORK_NAME}")
    logger.info(f"Webhook Port: {WEBHOOK_PORT}")

    # Display debug mode status
    if DEBUG_MODE:
        logger.warning("="*60)
        logger.warning(f"DEBUG MODE ENABLED: {DEBUG_MODE.upper()}")
        if DEBUG_MODE == 'webhook':
            logger.warning("Webhook actions will be logged but NOT executed")
            logger.warning("Scheduled actions WILL be executed normally")
        elif DEBUG_MODE == 'schedule':
            logger.warning("Scheduled actions will be logged but NOT executed")
            logger.warning("Webhook actions WILL be executed normally")
        elif DEBUG_MODE == 'all':
            logger.warning("ALL actions will be logged but NOT executed")
        logger.warning("="*60)
    else:
        logger.info("Debug Mode: Disabled (all actions will be executed)")

    # Load schedules
    logger.info("\nLoading schedules...")
    load_schedules()

    # Start Flask app
    logger.info(f"\nStarting webhook server on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    logger.info("Ready to receive webhooks!\n")

    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)


if __name__ == "__main__":
    main()
