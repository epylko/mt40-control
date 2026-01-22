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
from datetime import datetime
from functools import wraps
from collections import deque
from flask import Flask, request, jsonify, Response, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

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

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Event history (keep last 100 events)
event_history = deque(maxlen=100)

# Runtime debug mode control (can be changed via API)
debug_mode_state = {'mode': DEBUG_MODE}

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
    current_debug_mode = debug_mode_state['mode']
    skip_action = False
    if current_debug_mode == 'all':
        skip_action = True
    elif current_debug_mode == 'webhook' and source == 'webhook':
        skip_action = True
    elif current_debug_mode == 'schedule' and source == 'schedule':
        skip_action = True
    elif current_debug_mode == 'manual' and source == 'manual':
        skip_action = True

    if skip_action:
        logger.info(f"[DEBUG MODE - {source.upper()}] Would send {operation} command to MT40 ({MT40_SERIAL}), but skipping due to DEBUG_MODE={current_debug_mode}")
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


def power_on():
    """Turn MT40 power ON - scheduled function"""
    logger.info("⚡ Scheduled power ON triggered")
    control_mt40_power('on', source='schedule')


def power_off():
    """Turn MT40 power OFF - scheduled function"""
    logger.info("⏻ Scheduled power OFF triggered")
    control_mt40_power('off', source='schedule')


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
        .message {
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 4px;
            display: none;
        }
        .message.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .message.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
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
        .control-section {
            display: flex;
            gap: 15px;
            justify-content: center;
            margin-bottom: 30px;
        }
        .btn-control {
            padding: 15px 40px;
            font-size: 16px;
            font-weight: 600;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-control:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
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
        .status-display {
            text-align: center;
            margin-bottom: 30px;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 6px;
        }
        .status-display h3 {
            margin-bottom: 10px;
            color: #333;
            font-size: 16px;
        }
        .power-status {
            display: inline-block;
            padding: 8px 20px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 18px;
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
        #debugSection {
            border: 2px solid #ffc107;
            background: #fff3cd;
        }
        #currentDebugMode {
            background: #e9ecef;
            color: #495057;
        }
        #currentDebugMode.active {
            background: #ffc107;
            color: #000;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-section">
            <div id="clock" class="clock">--:--:--</div>
            <h1>MT40 Schedule Manager</h1>
            <p class="subtitle">Manage power on/off schedules</p>
        </div>

        <div id="message" class="message"></div>

        <div class="status-display">
            <h3>Current Power Status</h3>
            <div id="powerStatus" class="power-status unknown">Loading...</div>
        </div>

        <div class="control-section">
            <button class="btn-control btn-power-on" onclick="manualPowerControl('on')">⚡ Power ON</button>
            <button class="btn-control btn-power-off" onclick="manualPowerControl('off')">⏻ Power OFF</button>
        </div>

        <div class="form-section" id="debugSection">
            <h2>Debug Mode</h2>
            <p style="color: #666; margin-bottom: 15px;">Test power commands without affecting the actual device</p>

            <div style="margin-bottom: 20px; padding: 12px; background: white; border-radius: 4px; border: 1px solid #ddd;">
                <strong>Current Mode:</strong>
                <span id="currentDebugMode" style="margin-left: 10px; padding: 4px 12px; border-radius: 4px; font-weight: 500;">
                    Loading...
                </span>
            </div>

            <div style="display: flex; align-items: center; gap: 15px;">
                <label style="margin-bottom: 0; font-weight: 500;">Set Mode:</label>
                <select id="debugModeSelect" style="width: auto; flex: 1; max-width: 300px;">
                    <option value="">Disabled (Normal Operation)</option>
                    <option value="manual">Manual Only</option>
                    <option value="webhook">Webhook Only</option>
                    <option value="schedule">Schedule Only</option>
                    <option value="all">All Actions</option>
                </select>
                <button onclick="applyDebugMode()" class="btn-primary">Apply</button>
            </div>
        </div>

        <div class="form-section">
            <h2>Add New Schedule</h2>
            <form id="addForm">
                <div class="form-row">
                    <div class="form-group">
                        <label>Name</label>
                        <input type="text" id="name" required placeholder="e.g., Morning ON">
                    </div>
                    <div class="form-group">
                        <label>Action</label>
                        <select id="action" required>
                            <option value="on">Power ON</option>
                            <option value="off">Power OFF</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Time</label>
                        <input type="time" id="time" required>
                    </div>
                    <div class="form-group">
                        <label>Days</label>
                        <select id="days" required>
                            <option value="daily">Daily</option>
                            <option value="mon-fri">Weekdays (Mon-Fri)</option>
                            <option value="weekends">Weekends (Sat-Sun)</option>
                            <option value="mon">Monday</option>
                            <option value="tue">Tuesday</option>
                            <option value="wed">Wednesday</option>
                            <option value="thu">Thursday</option>
                            <option value="fri">Friday</option>
                            <option value="sat">Saturday</option>
                            <option value="sun">Sunday</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <button type="submit" class="btn-primary">Add</button>
                    </div>
                </div>
            </form>
        </div>

        <div class="form-section">
            <h2>Current Schedules</h2>
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
    </div>

    <script>
        let schedules = [];

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            msg.style.display = 'block';
            setTimeout(() => {
                msg.style.display = 'none';
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
                    statusEl.textContent = '⚡ ON';
                } else if (state === 'off') {
                    statusEl.classList.add('off');
                    statusEl.textContent = '⏻ OFF';
                } else {
                    statusEl.classList.add('unknown');
                    statusEl.textContent = '? UNKNOWN';
                }
            } catch (error) {
                console.error('Error loading power status:', error);
            }
        }

        function getDebugModeLabel(mode) {
            const labels = {
                '': 'Disabled (Normal Operation)',
                'manual': 'Manual Only',
                'webhook': 'Webhook Only',
                'schedule': 'Schedule Only',
                'all': 'All Actions'
            };
            return labels[mode] || mode;
        }

        async function loadDebugMode() {
            try {
                const response = await fetch('/api/debug');
                if (!response.ok) throw new Error('Failed to load debug mode');
                const data = await response.json();

                const currentModeEl = document.getElementById('currentDebugMode');
                const debugSelect = document.getElementById('debugModeSelect');

                // Update current mode display
                const modeLabel = getDebugModeLabel(data.debug_mode);
                currentModeEl.textContent = modeLabel;

                // Highlight if debug mode is active
                if (data.debug_mode) {
                    currentModeEl.classList.add('active');
                } else {
                    currentModeEl.classList.remove('active');
                }

                // Set the selector to current mode
                debugSelect.value = data.debug_mode;
            } catch (error) {
                console.error('Error loading debug mode:', error);
            }
        }

        async function applyDebugMode() {
            const debugSelect = document.getElementById('debugModeSelect');
            const mode = debugSelect.value;

            try {
                const response = await fetch('/api/debug', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ mode: mode })
                });

                if (!response.ok) throw new Error('Failed to set debug mode');

                const data = await response.json();

                // Reload to show the new current mode
                await loadDebugMode();

                if (data.debug_mode) {
                    showMessage(`Debug mode enabled: ${getDebugModeLabel(data.debug_mode)}`, 'success');
                } else {
                    showMessage('Debug mode disabled - normal operation resumed', 'success');
                }
            } catch (error) {
                showMessage(`Error setting debug mode: ${error.message}`, 'error');
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

        document.getElementById('addForm').addEventListener('submit', async (e) => {
            e.preventDefault();

            const newSchedule = {
                name: document.getElementById('name').value,
                action: document.getElementById('action').value,
                time: document.getElementById('time').value,
                days: document.getElementById('days').value,
                enabled: true
            };

            schedules.push(newSchedule);
            await saveSchedules();

            // Reset form
            e.target.reset();
        });

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
        # Get recent commands to determine current state
        commands = dashboard.sensor.getDeviceSensorCommands(MT40_SERIAL)

        # Find the most recent completed power command
        current_state = 'unknown'
        last_update = None

        for cmd in commands:
            operation = cmd.get('operation', '')
            status = cmd.get('status', '')

            if status == 'completed' and operation in ['enableDownstreamPower', 'disableDownstreamPower']:
                current_state = 'on' if operation == 'enableDownstreamPower' else 'off'
                last_update = cmd.get('completedAt', cmd.get('sentAt'))
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
    """Get current debug mode setting"""
    return jsonify({
        'debug_mode': debug_mode_state['mode'],
        'available_modes': ['', 'webhook', 'schedule', 'manual', 'all']
    }), 200


@app.route('/api/debug', methods=['POST'])
@require_auth
def set_debug_mode_api():
    """Set debug mode"""
    try:
        data = request.get_json()
        new_mode = data.get('mode', '').lower()

        # Validate mode
        valid_modes = ['', 'webhook', 'schedule', 'manual', 'all']
        if new_mode not in valid_modes:
            return jsonify({
                'error': f'Invalid mode. Must be one of: {", ".join(valid_modes)}'
            }), 400

        debug_mode_state['mode'] = new_mode
        logger.info(f"Debug mode changed to: '{new_mode}' (empty = disabled)")

        return jsonify({
            'status': 'success',
            'debug_mode': debug_mode_state['mode']
        }), 200
    except Exception as e:
        logger.error(f"Error setting debug mode: {e}")
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
