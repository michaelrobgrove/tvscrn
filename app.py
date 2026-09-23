"""
607 Streaming RMM - Production App
===================================
Remote Monitoring & Management tool for 607 Streaming IPTV devices.

Features:
  - Device CRUD, scrcpy + ffmpeg streaming, VNC proxy, WebRTC (WHEP)
  - ADB input injection (tap/key/text/swipe/longpress)
  - 30-minute session timeout per device
  - Email notifications for expiring IPTV subscriptions
  - Device credentials API (iptv_username / iptv_password)
  - Real-time device status via Flask-SocketIO

Upgrades (v2):
  - Audit logging: every login/connect/disconnect/input/reboot/password-change
  - Multi-user roles: admin (full access) / operator (view-only devices)
  - MariaDB support via PyMySQL with DB_URL env var, SQLite fallback
  - User management page (admin-only)
  - Connection pooling via DB_URL environment variable
"""

import os
import sys
import json
import time
import hashlib
import subprocess
import threading
import smtplib
import logging
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, g
from flask_socketio import SocketIO, emit

# ---------------------------------------------------------------------------
# Database abstraction layer: MariaDB (PyMySQL) with SQLite fallback
# ---------------------------------------------------------------------------

DB_URL = os.environ.get('DB_URL', '')          # e.g. mysql+pymysql://user:pass@host/dbname
DB_PATH = os.environ.get('DB_PATH', '/607stream/devices.db')  # SQLite fallback

_pool = None          # PyMySQL pool (only when MariaDB is active)
_use_mariadb = False  # True when DB_URL is set and pool initialises

def _init_mariadb():
    """
    Attempt to create a PyMySQL connection pool from DB_URL.
    Supported formats:
      mysql+pymysql://user:pass@host:port/dbname
      mariadb://user:pass@host:port/dbname
    Returns True on success, False on failure (caller falls back to SQLite).
    """
    global _pool, _use_mariadb
    if not DB_URL:
        return False

    try:
        import pymysql
    except ImportError:
        log.warning("pymysql not installed, cannot use MariaDB")
        return False

    # Parse URL
    url = DB_URL
    for prefix in ('mysql+pymysql://', 'mariadb://', 'mysql://'):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break

    user_pass, host_db = url.split('@', 1)
    user, password = (user_pass.split(':', 1) + [''])[:2]
    host_port, dbname = host_db.split('/', 1)
    host, port = (host_port.split(':', 1) + ['3306'])[:2]

    try:
        from dbutils.pooled_db import PooledDB
        _pool = PooledDB(
            creator=pymysql,
            mincached=1,
            maxcached=10,
            maxconnections=20,
            blocking=True,
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=dbname,
            charset='utf8mb4',
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
        _use_mariadb = True
        logging.getLogger('607rmm').info(f"MariaDB pool created -> {host}:{port}/{dbname}")
        return True
    except Exception as e:
        logging.getLogger('607rmm').warning(f"MariaDB pool failed ({e}), falling back to SQLite")
        return False


def _get_conn():
    """
    Return a database connection. Uses MariaDB pool when available,
    otherwise falls back to sqlite3. Connections are cached on Flask's `g`.
    """
    if hasattr(g, '_db_conn') and g._db_conn is not None:
        return g._db_conn

    if _use_mariadb and _pool is not None:
        conn = _pool.connection()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

    g._db_conn = conn
    return conn


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24).hex())
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
socketio = SocketIO(app, cors_allowed_origins="*")

@app.teardown_appcontext
def _close_conn(exc):
    """Close the per-request database connection."""
    conn = getattr(g, '_db_conn', None)
    if conn is None:
        return
    g._db_conn = None
    try:
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SQL helpers that abstract sqlite3 / PyMySQL differences
# ---------------------------------------------------------------------------

def _execute(conn, sql, params=(), commit=True):
    """Execute SQL, return cursor. Handles both sqlite3 and PyMySQL."""
    if _use_mariadb:
        cur = conn.cursor()
        cur.execute(sql, params)
        if commit:
            conn.commit()
        return cur
    else:
        cur = conn.execute(sql, params)
        if commit:
            conn.commit()
        return cur


def _fetchone(conn, sql, params=()):
    """Fetch a single row. Returns dict-like row or None."""
    cur = _execute(conn, sql, params, commit=False)
    return cur.fetchone()


def _fetchall(conn, sql, params=()):
    """Fetch all rows."""
    cur = _execute(conn, sql, params, commit=False)
    return cur.fetchall()


def _row_to_dict(row):
    """Convert a row (sqlite3.Row or pymysql dict) to a plain dict."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


# ---------------------------------------------------------------------------
# Flask App Configuration
# ---------------------------------------------------------------------------

# In-memory state
active_scrcpy = {}
active_ffmpeg = {}
session_timers = {}
_sid_to_device_id = {}
SESSION_TIMEOUT = 1800  # 30 minutes

CONFIG_PATH = os.environ.get('CONFIG_PATH', '/607stream/config.json')

# Logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
log = logging.getLogger('607rmm')

# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------

AUDIT_ACTIONS = (
    'login', 'logout', 'password_change',
    'device_connect', 'device_disconnect', 'device_reboot',
    'device_add', 'device_update', 'device_delete',
    'input_tap', 'input_key', 'input_text', 'input_swipe', 'input_longpress',
    'settings_change', 'user_create', 'user_update', 'user_delete',
    'kill_all_adb',
)


def audit_log(action, details='', device_id=None):
    """
    Write an entry to the audit_log table.
    Called explicitly and via the @audit decorator.
    """
    try:
        conn = _get_conn()
        username = session.get('username', 'system')
        ip_addr = request.remote_addr if request else None
        ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        _execute(conn,
                 "INSERT INTO audit_log (timestamp, username, action, device_id, details, ip_address) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (ts, username, action, device_id, details, ip_addr))
    except Exception as e:
        log.warning(f"audit_log write failed: {e}")


def audit(action, device_id_from_kwarg='device_id'):
    """
    Decorator factory that auto-logs an audit entry after the route returns.
    Usage:
        @api_login_required
        @audit('device_connect')
        def connect_device(device_id): ...
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            result = f(*args, **kwargs)
            did = kwargs.get(device_id_from_kwarg)
            try:
                audit_log(action, device_id=did)
            except Exception:
                pass
            return result
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Session Timeout Handler
# ---------------------------------------------------------------------------

def _session_timeout(device_id):
    """Called after 30 minutes -- disconnects scrcpy, ffmpeg, and ADB."""
    log.info(f"[TIMEOUT] Session {device_id} expired after 30 min, disconnecting")
    if device_id in active_scrcpy:
        try:
            active_scrcpy[device_id].terminate()
            active_scrcpy[device_id].wait(timeout=3)
        except Exception:
            try:
                active_scrcpy[device_id].kill()
            except Exception:
                pass
        active_scrcpy.pop(device_id, None)
    if device_id in active_ffmpeg:
        try:
            active_ffmpeg[device_id].terminate()
            active_ffmpeg[device_id].wait(timeout=3)
        except Exception:
            try:
                active_ffmpeg[device_id].kill()
            except Exception:
                pass
        active_ffmpeg.pop(device_id, None)
    try:
        conn = _get_conn()
        row = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
        if row:
            ip = row['ip_address'] if isinstance(row, dict) else row[0]
            subprocess.run(['adb', 'disconnect', ip], timeout=5, check=False)
    except Exception:
        pass
    session_timers.pop(device_id, None)


# ---------------------------------------------------------------------------
# VNC Toolbar Configuration
# ---------------------------------------------------------------------------

VNC_TOOLBAR_BUTTONS = [
    {"keycode": 3,   "label": "⌂ Home",   "title": "Home"},
    {"keycode": 4,   "label": "← Back",   "title": "Back"},
    {"keycode": 24,  "label": "V+",       "title": "Volume Up"},
    {"keycode": 25,  "label": "V-",       "title": "Volume Down"},
    {"keycode": 19,  "label": "↑",        "title": "Up"},
    {"keycode": 20,  "label": "↓",        "title": "Down"},
    {"keycode": 21,  "label": "←",        "title": "Left"},
    {"keycode": 22,  "label": "→",        "title": "Right"},
    {"keycode": 23,  "label": "OK",       "title": "Select / OK"},
]


# ============================================
# Database Initialization
# ============================================

def init_db():
    """Create tables if they don't exist (works for both SQLite and MariaDB)."""
    if _use_mariadb:
        conn = _pool.connection()
        try:
            cur = conn.cursor()
            # Devices table
            cur.execute('''CREATE TABLE IF NOT EXISTS devices
                (id INT AUTO_INCREMENT PRIMARY KEY,
                 customer_name VARCHAR(255) NOT NULL,
                 device_name VARCHAR(255),
                 ip_address VARCHAR(45) NOT NULL,
                 device_type VARCHAR(100) NOT NULL,
                 iptv_username VARCHAR(255),
                 iptv_expiration VARCHAR(20),
                 phone_number VARCHAR(50),
                 iptv_password VARCHAR(255),
                 status VARCHAR(20) DEFAULT 'offline',
                 last_online VARCHAR(30),
                 date_added VARCHAR(30) DEFAULT CURRENT_TIMESTAMP)''')

            # Users table (with role column)
            cur.execute('''CREATE TABLE IF NOT EXISTS users
                (id INT AUTO_INCREMENT PRIMARY KEY,
                 username VARCHAR(255) UNIQUE NOT NULL,
                 password_hash VARCHAR(255) NOT NULL,
                 role VARCHAR(20) NOT NULL DEFAULT 'operator',
                 created_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
                 last_login VARCHAR(30))''')

            # Audit log table
            cur.execute('''CREATE TABLE IF NOT EXISTS audit_log
                (id INT AUTO_INCREMENT PRIMARY KEY,
                 timestamp VARCHAR(30) NOT NULL,
                 username VARCHAR(255) NOT NULL,
                 action VARCHAR(50) NOT NULL,
                 device_id INT,
                 details TEXT,
                 ip_address VARCHAR(45))''')

            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(username)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")

            # Seed default admin
            cur.execute("SELECT id FROM users WHERE username='admin'")
            if not cur.fetchone():
                pw_hash = hashlib.sha256('admin123'.encode()).hexdigest()
                cur.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
                    ('admin', pw_hash, 'admin'))
                log.info("Default admin user created (password: admin123)")

            conn.commit()
        finally:
            conn.close()
    else:
        # SQLite path
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS devices
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             customer_name TEXT NOT NULL,
             device_name TEXT,
             ip_address TEXT NOT NULL,
             device_type TEXT NOT NULL,
             iptv_username TEXT,
             iptv_expiration TEXT,
             phone_number TEXT,
             iptv_password TEXT,
             status TEXT DEFAULT 'offline',
             last_online TEXT,
             date_added TEXT DEFAULT CURRENT_TIMESTAMP)''')

        c.execute('''CREATE TABLE IF NOT EXISTS users
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             username TEXT UNIQUE NOT NULL,
             password_hash TEXT NOT NULL,
             role TEXT NOT NULL DEFAULT 'operator',
             created_at TEXT DEFAULT CURRENT_TIMESTAMP,
             last_login TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS audit_log
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             timestamp TEXT NOT NULL,
             username TEXT NOT NULL,
             action TEXT NOT NULL,
             device_id INTEGER,
             details TEXT,
             ip_address TEXT)''')

        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(username)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")

        # Seed default admin
        c.execute("SELECT * FROM users WHERE username='admin'")
        if not c.fetchone():
            pw_hash = hashlib.sha256('admin123'.encode()).hexdigest()
            c.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                      ('admin', pw_hash, 'admin'))
            log.info("Default admin user created (password: admin123)")

        conn.commit()
        conn.close()


# ============================================
# Configuration Management
# ============================================

def load_config():
    """Load configuration from JSON file."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Error loading config: {e}")
    return {
        'email_enabled': False,
        'smtp_server': '',
        'smtp_port': 587,
        'email_user': '',
        'email_password': '',
        'notification_email': ''
    }


def save_config(config):
    """Save configuration to JSON file."""
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        log.error(f"Error saving config: {e}")
        return False


# ============================================
# Authentication & Authorization
# ============================================

def login_required(f):
    """Decorator to require login for page routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def api_login_required(f):
    """Decorator to require login for API routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Unauthorized', 'success': False}), 401
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Decorator to restrict a route to admin users only."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'success': False}), 401
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Admin access required', 'success': False}), 403
            return "Forbidden - admin access required", 403
        return f(*args, **kwargs)
    return decorated_function


def admin_or_operator(f):
    """Decorator to restrict a route to logged-in users (any role)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Unauthorized', 'success': False}), 401
        return f(*args, **kwargs)
    return decorated_function


# ============================================
# Device Management Functions
# ============================================

def ping_device(ip):
    """Ping a device to check if it's online."""
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '1', ip],
            capture_output=True, timeout=2)
        return result.returncode == 0
    except Exception as e:
        log.debug(f"Ping error for {ip}: {e}")
        return False


def check_devices():
    """Background task to check device status every 2 minutes."""
    while True:
        try:
            if _use_mariadb:
                conn = _pool.connection()
            else:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row

            devices = _fetchall(conn, "SELECT id, ip_address FROM devices")

            for device in devices:
                did = device['id'] if isinstance(device, dict) else device[0]
                ip = device['ip_address'] if isinstance(device, dict) else device[1]
                if not ip:
                    continue
                is_online = ping_device(ip)
                new_status = 'online' if is_online else 'offline'
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                _execute(conn,
                         "UPDATE devices SET status=?, last_online=? WHERE id=?",
                         (new_status, ts, did))
                socketio.emit('status_update', {'device_id': did, 'status': new_status})

            if not _use_mariadb:
                conn.close()
            else:
                conn.close()
        except Exception as e:
            log.error(f"Device check error: {e}")

        time.sleep(120)


# ============================================
# Email Notification Functions
# ============================================

def send_email(to_email, subject, body):
    """Send an email notification via SMTP."""
    try:
        config = load_config()
        if not config.get('email_enabled'):
            return False
        smtp_server = config.get('smtp_server')
        smtp_port = config.get('smtp_port', 587)
        email_user = config.get('email_user')
        email_password = config.get('email_password')
        if not all([smtp_server, email_user, email_password, to_email]):
            log.warning("Email configuration incomplete")
            return False
        msg = MIMEMultipart()
        msg['From'] = email_user
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_user, email_password)
            server.send_message(msg)
        log.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        log.error(f"Email send error: {e}")
        return False


def check_expiring_subscriptions():
    """Check for subscriptions expiring within 2 days and send notifications (daily at 9 AM)."""
    while True:
        try:
            now = datetime.now()
            if now.hour == 9 and now.minute < 5:
                config = load_config()
                if not config.get('email_enabled'):
                    log.info("Email notifications disabled")
                else:
                    if _use_mariadb:
                        conn = _pool.connection()
                    else:
                        conn = sqlite3.connect(DB_PATH)
                        conn.row_factory = sqlite3.Row

                    today = datetime.now().date()

                    devices = _fetchall(conn,
                        """SELECT customer_name, device_name, ip_address,
                                  iptv_username, iptv_expiration
                           FROM devices
                           WHERE iptv_expiration IS NOT NULL AND iptv_expiration != ''""")

                    expiring_devices = []
                    for device in devices:
                        customer = device[0] if not isinstance(device, dict) else device['customer_name']
                        device_name = device[1] if not isinstance(device, dict) else device['device_name']
                        ip = device[2] if not isinstance(device, dict) else device['ip_address']
                        username = device[3] if not isinstance(device, dict) else device['iptv_username']
                        expiration = device[4] if not isinstance(device, dict) else device['iptv_expiration']
                        try:
                            exp_date = datetime.strptime(expiration, '%Y-%m-%d').date()
                            days_left = (exp_date - today).days
                            if 0 <= days_left <= 2:
                                expiring_devices.append({
                                    'customer': customer,
                                    'device': device_name or 'Unknown Device',
                                    'ip': ip,
                                    'username': username,
                                    'expiration': expiration,
                                    'days_left': days_left
                                })
                        except ValueError:
                            continue

                    if not _use_mariadb:
                        conn.close()
                    else:
                        conn.close()

                    if expiring_devices:
                        notification_email = config.get('notification_email')
                        if notification_email:
                            subject = f"\u26a0\ufe0f {len(expiring_devices)} IPTV Subscription(s) Expiring Soon"
                            body = _build_expiration_email(expiring_devices)
                            send_email(notification_email, subject, body)
                            log.info(f"Sent expiration notification for {len(expiring_devices)} devices")
                    else:
                        log.info("No devices expiring soon")
        except Exception as e:
            log.error(f"Expiration check error: {e}")

        time.sleep(300)


def _build_expiration_email(devices):
    """Build HTML email body for expiring subscriptions."""
    body = """
    <html><head><style>
        body { font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }
        .container { background-color: white; padding: 30px; border-radius: 10px; max-width: 800px; margin: 0 auto; }
        h2 { color: #c41e3a; }
        .device-card { background-color: #f8f9fa; padding: 15px; margin: 10px 0; border-left: 4px solid #c41e3a; border-radius: 5px; }
        .urgent { border-left-color: #e74c3c; }
        .warning { border-left-color: #f39c12; }
        .label { font-weight: bold; color: #555; }
        .value { color: #333; }
        .footer { margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; color: #777; font-size: 12px; }
    </style></head><body><div class="container">
        <h2>\U0001f514 IPTV Subscription Expiration Alert</h2>
        <p>The following subscriptions are expiring within the next 2 days:</p>
    """
    for d in devices:
        cls = 'urgent' if d['days_left'] == 0 else 'warning'
        txt = 'EXPIRES TODAY!' if d['days_left'] == 0 else f"Expires in {d['days_left']} day(s)"
        body += f"""
        <div class="device-card {cls}">
            <h3 style="margin-top:0;color:#c41e3a;">{d['customer']}</h3>
            <p><span class="label">Device:</span> <span class="value">{d['device']}</span></p>
            <p><span class="label">IP:</span> <span class="value">{d['ip']}</span></p>
            <p><span class="label">IPTV Username:</span> <span class="value">{d['username'] or 'N/A'}</span></p>
            <p><span class="label">Expires:</span> <span class="value">{d['expiration']}</span></p>
            <p style="color:#c41e3a;font-weight:bold;font-size:16px;">\u26a0\ufe0f {txt}</p>
        </div>"""
    body += """
        <div class="footer">
            <p>Automated notification from 607 Streaming Manager.</p>
            <p>Please renew these subscriptions to avoid service interruption.</p>
        </div>
    </div></body></html>"""
    return body


# ============================================
# Routes -- Authentication
# ============================================

@app.route('/')
def index():
    """Redirect to dashboard or login."""
    if 'username' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))



@app.before_request
def check_setup_required():
    """Redirect to setup page if no admin user exists."""
    # Skip setup check for static files and the setup page itself
    if request.path.startswith('/static') or request.path == '/setup':
        return
    try:
        conn = _get_conn()
        row = _fetchone(conn, "SELECT id FROM users WHERE role='admin' LIMIT 1")
        if not row:
            return redirect('/setup')
    except Exception:
        pass

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """First-run setup wizard."""
    # Check if admin already exists
    try:
        conn = _get_conn()
        admin = _fetchone(conn, "SELECT id FROM users WHERE role='admin' LIMIT 1")
        if admin:
            return redirect('/login')
    except Exception:
        pass
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        
        if not username or not password:
            return render_template('setup.html', error='Username and password required')
        if password != confirm:
            return render_template('setup.html', error='Passwords do not match')
        if len(password) < 8:
            return render_template('setup.html', error='Password must be at least 8 characters')
        
        try:
            conn = _get_conn()
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
            _execute(conn, "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                     (username, pw_hash, 'admin'))
            return redirect('/login')
        except Exception as e:
            return render_template('setup.html', error=f'Setup failed: {str(e)}')
    
    return render_template('setup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and authentication."""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            return render_template('login.html', error="Username and password required")

        p_hash = hashlib.sha256(password.encode()).hexdigest()
        try:
            conn = _get_conn()
            user = _fetchone(conn,
                "SELECT * FROM users WHERE username=? AND password_hash=?",
                (username, p_hash))

            if user:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                _execute(conn, "UPDATE users SET last_login=? WHERE username=?", (now, username))
                session['username'] = username
                session['role'] = user.get('role', 'operator') if isinstance(user, dict) else (user['role'] if 'role' in user.keys() else 'operator')
                session.permanent = True
                audit_log('login')
                return redirect(url_for('dashboard'))

            return render_template('login.html', error="Invalid credentials")
        except Exception as e:
            log.error(f"Login error: {e}")
            return render_template('login.html', error="An error occurred")

    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout and clear session."""
    audit_log('logout')
    session.pop('username', None)
    session.pop('role', None)
    return redirect(url_for('login'))


# ============================================
# Routes -- Main Pages
# ============================================

@app.route('/device/<ip_path>')
@login_required
def device_control(ip_path):
    """Device control page (WebRTC + VNC)."""
    device_id = None
    device_name = None
    try:
        conn = _get_conn()
        row = _fetchone(conn,
            "SELECT id, device_name FROM devices WHERE ip_address=?",
            (ip_path.replace('_', '.'),))
        if row:
            device_id = row['id'] if isinstance(row, dict) else row[0]
            device_name = row['device_name'] if isinstance(row, dict) else row[1]
    except Exception:
        pass
    return render_template("device_control.html", device_id=device_id, device_name=device_name)


@app.route('/vnc/<path:subpath>', methods=['GET'])
def vnc_proxy(subpath):
    """Proxy noVNC requests through Flask to avoid CORS/HTTPS issues."""
    import requests as _req
    from flask import Response
    if subpath.startswith('websockify'):
        from flask import redirect as _redir
        return _redir(f'http://100.67.146.57:6080/{subpath}', code=302)

    novnc_dir = '/usr/share/novnc'
    filepath = os.path.join(novnc_dir, subpath)
    if not os.path.exists(filepath):
        return Response('Not found', status=404)

    mime_map = {
        '.html': 'text/html', '.js': 'application/javascript',
        '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg',
    }
    ext = os.path.splitext(subpath)[1]
    mimetype = mime_map.get(ext, 'application/octet-stream')

    with open(filepath, 'rb') as f:
        body = f.read()

    if subpath == 'vnc.html':
        inject = (
            b"<style>"
            b"#noVNC_settings_button { display: none !important; }"
            b"#noVNC_settings { display: none !important; }"
            b"#noVNC_clipboard_button { display: none !important; }"
            b"#noVNC_clipboard { display: none !important; }"
            b"#noVNC_fullscreen_button { display: none !important; }"
            b"#noVNC_screenshot_button { display: none !important; }"
            b"#noVNC_control_bar { background: rgba(20,20,20,0.7) !important; }"
            b"</style>"
        )
        body = body.replace(b'</head>', inject + b'</head>')

    return Response(body, mimetype=mimetype)


@app.route('/vnc')
def vnc_root():
    """Redirect to noVNC viewer."""
    from flask import redirect as _redir
    return _redir('/vnc/vnc.html?autoconnect=true&resize=scale&path=/websockify', code=302)


@app.route('/device/<ip_path>/whep', methods=['POST'])
def whep_proxy(ip_path):
    """Proxy WebRTC WHEP signaling to mediamtx."""
    import requests as _req
    from flask import Response
    try:
        ctype = request.headers.get('Content-Type', 'application/sdp')
        resp = _req.post(
            f'http://127.0.0.1:8889/{ip_path}/whep',
            data=request.get_data(),
            headers={'Content-Type': ctype},
            timeout=5)
        return Response(resp.content, status=resp.status_code,
                        content_type=resp.headers.get('Content-Type', 'application/sdp'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.context_processor
def inject_vnc_toolbar():
    """Make VNC_TOOLBAR_BUTTONS available to all templates."""
    return dict(vnc_toolbar_buttons=VNC_TOOLBAR_BUTTONS)


@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard page."""
    try:
        conn = _get_conn()
        devices = _fetchall(conn,
            """SELECT id, customer_name, device_name, ip_address, device_type,
                      iptv_username, iptv_expiration, phone_number, iptv_password,
                      status, last_online
               FROM devices ORDER BY customer_name ASC, device_name ASC""")
        # Convert Row objects to tuples for Jinja2 tojson compatibility
        if devices and not isinstance(devices[0], dict):
            devices = [tuple(d) for d in devices]
        return render_template('dashboard.html',
                               devices=devices,
                               now=datetime.now().strftime("%Y-%m-%d"),
                               current_user=session.get('username'),
                               user_role=session.get('role', 'operator'))
    except Exception as e:
        log.error(f"Dashboard error: {e}")
        return "Error loading dashboard", 500


@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    """Settings page (admin-only)."""
    if request.method == 'POST':
        config = {
            'email_enabled': request.form.get('email_enabled') == 'on',
            'smtp_server': request.form.get('smtp_server', '').strip(),
            'smtp_port': int(request.form.get('smtp_port', 587)),
            'email_user': request.form.get('email_user', '').strip(),
            'email_password': request.form.get('email_password', ''),
            'notification_email': request.form.get('notification_email', '').strip()
        }
        if save_config(config):
            audit_log('settings_change', details='Email config updated')
            return render_template('settings.html', config=config, success=True)
        else:
            return render_template('settings.html', config=config, error=True)

    config = load_config()
    return render_template('settings.html', config=config)


# ============================================
# User Management (Admin-only)
# ============================================

@app.route('/users')
@login_required
@admin_required
def users_page():
    """User management page (admin-only)."""
    try:
        conn = _get_conn()
        users = _fetchall(conn,
            "SELECT id, username, role, created_at, last_login FROM users ORDER BY id")
        return render_template('users.html',
                               users=users,
                               current_user=session.get('username'))
    except Exception as e:
        log.error(f"Users page error: {e}")
        return "Error loading users", 500


@app.route('/api/users', methods=['GET'])
@login_required
@admin_required
def api_list_users():
    """List all users (admin-only)."""
    try:
        conn = _get_conn()
        users = _fetchall(conn,
            "SELECT id, username, role, created_at, last_login FROM users ORDER BY id")
        result = []
        for u in users:
            d = _row_to_dict(u)
            d.pop('password_hash', None)  # never expose hash
            result.append(d)
        return jsonify({'success': True, 'users': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/users', methods=['POST'])
@login_required
@admin_required
def api_create_user():
    """Create a new user (admin-only)."""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '')
        role = data.get('role', 'operator')

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'}), 400
        if len(password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters'}), 400
        if role not in ('admin', 'operator'):
            return jsonify({'success': False, 'error': 'Role must be admin or operator'}), 400

        p_hash = hashlib.sha256(password.encode()).hexdigest()
        conn = _get_conn()

        # Check for duplicate username
        existing = _fetchone(conn, "SELECT id FROM users WHERE username=?", (username,))
        if existing:
            return jsonify({'success': False, 'error': 'Username already exists'}), 409

        _execute(conn,
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, p_hash, role))
        audit_log('user_create', details=f'Created user {username} with role {role}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@login_required
@admin_required
def api_update_user(user_id):
    """Update a user's role or password (admin-only)."""
    try:
        data = request.json
        conn = _get_conn()
        user = _fetchone(conn, "SELECT * FROM users WHERE id=?", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        username = user.get('username') if isinstance(user, dict) else user['username']
        role = data.get('role')
        new_password = data.get('password', '')

        if role and role in ('admin', 'operator'):
            _execute(conn, "UPDATE users SET role=? WHERE id=?", (role, user_id))

        if new_password:
            if len(new_password) < 8:
                return jsonify({'success': False, 'error': 'Password must be at least 8 characters'}), 400
            p_hash = hashlib.sha256(new_password.encode()).hexdigest()
            _execute(conn, "UPDATE users SET password_hash=? WHERE id=?", (p_hash, user_id))

        audit_log('user_update', details=f'Updated user {username}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def api_delete_user(user_id):
    """Delete a user (admin-only). Cannot delete the last admin."""
    try:
        conn = _get_conn()
        user = _fetchone(conn, "SELECT * FROM users WHERE id=?", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        username = user.get('username') if isinstance(user, dict) else user['username']
        role = user.get('role') if isinstance(user, dict) else user.get('role', 'operator')

        # Prevent deleting the last admin
        if role == 'admin':
            admins = _fetchall(conn, "SELECT id FROM users WHERE role='admin'")
            if len(admins) <= 1:
                return jsonify({'success': False, 'error': 'Cannot delete the last admin user'}), 400

        _execute(conn, "DELETE FROM users WHERE id=?", (user_id,))
        audit_log('user_delete', details=f'Deleted user {username}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Audit Log API (Admin-only)
# ============================================

@app.route('/audit-log')
@login_required
@admin_required
def audit_log_page():
    """Audit log viewer page (admin-only)."""
    return render_template('audit_log.html', current_user=session.get('username'))


@app.route('/api/audit-log', methods=['GET'])
@login_required
@admin_required
def api_audit_log():
    """Query audit log entries (admin-only). Supports pagination and filtering."""
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        action_filter = request.args.get('action', '')
        user_filter = request.args.get('user', '')
        offset = (page - 1) * per_page

        conn = _get_conn()
        conditions = []
        params = []

        if action_filter:
            conditions.append("action = ?")
            params.append(action_filter)
        if user_filter:
            conditions.append("username = ?")
            params.append(user_filter)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        count_row = _fetchone(conn, f"SELECT COUNT(*) as cnt FROM audit_log {where}", tuple(params))
        total = count_row['cnt'] if isinstance(count_row, dict) else count_row[0]

        rows = _fetchall(conn,
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (per_page, offset))

        entries = []
        for r in rows:
            d = _row_to_dict(r)
            entries.append(d)

        return jsonify({
            'success': True,
            'entries': entries,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# API Routes -- Device Management
# ============================================

@app.route('/api/devices', methods=['POST'])
@api_login_required
@admin_required
def add_device():
    """Add a new device (admin-only)."""
    try:
        data = request.json
        if not data.get('customer_name') or not data.get('ip_address'):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        conn = _get_conn()
        _execute(conn,
            """INSERT INTO devices
               (customer_name, device_name, ip_address, device_type,
                iptv_username, iptv_expiration, phone_number, iptv_password)
               VALUES (?,?,?,?,?,?,?,?)""",
            (data['customer_name'], data.get('device_name', ''), data['ip_address'],
             data.get('device_type', 'Onn Box'), data.get('iptv_username', ''),
             data.get('iptv_expiration', ''), data.get('phone_number', ''),
             data.get('iptv_password', '')))

        audit_log('device_add', details=f"Added device {data['customer_name']} ({data['ip_address']})")
        return jsonify({'success': True})
    except Exception as e:
        log.error(f"Add device error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>', methods=['PUT'])
@api_login_required
@admin_required
def update_device(device_id):
    """Update an existing device (admin-only)."""
    try:
        data = request.json
        if not data.get('customer_name') or not data.get('ip_address'):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        conn = _get_conn()
        _execute(conn,
            """UPDATE devices SET
               customer_name=?, device_name=?, ip_address=?, device_type=?,
               iptv_username=?, iptv_expiration=?, phone_number=?, iptv_password=?
               WHERE id=?""",
            (data['customer_name'], data.get('device_name', ''), data['ip_address'],
             data.get('device_type', 'Onn Box'), data.get('iptv_username', ''),
             data.get('iptv_expiration', ''), data.get('phone_number', ''),
             data.get('iptv_password', ''), device_id))

        audit_log('device_update', device_id=device_id,
                  details=f"Updated device {data['customer_name']}")
        return jsonify({'success': True})
    except Exception as e:
        log.error(f"Update device error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>', methods=['DELETE'])
@api_login_required
@admin_required
def delete_device(device_id):
    """Delete a device (admin-only)."""
    try:
        conn = _get_conn()
        _execute(conn, "DELETE FROM devices WHERE id=?", (device_id,))
        audit_log('device_delete', device_id=device_id)
        return jsonify({'success': True})
    except Exception as e:
        log.error(f"Delete device error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/ping', methods=['POST'])
@api_login_required
@admin_or_operator
def manual_ping(device_id):
    """Manually ping a device."""
    try:
        conn = _get_conn()
        result = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
        if not result:
            return jsonify({'success': False, 'error': 'Device not found'}), 404

        ip = result['ip_address'] if isinstance(result, dict) else result[0]
        is_online = ping_device(ip)
        new_status = 'online' if is_online else 'offline'
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _execute(conn, "UPDATE devices SET status=?, last_online=? WHERE id=?",
                 (new_status, ts, device_id))
        return jsonify({'success': True, 'status': new_status})
    except Exception as e:
        log.error(f"Ping error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# API Routes -- Device Control (all authenticated users)
# ============================================

@app.route('/api/devices/<int:device_id>/connect', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('device_connect')
def connect_device(device_id):
    """Connect to device via scrcpy/VNC."""
    try:
        conn = _get_conn()
        result = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
        if not result:
            return jsonify({'success': False, 'error': 'Device not found'}), 404

        ip = result['ip_address'] if isinstance(result, dict) else result[0]

        # Connect via ADB
        try:
            subprocess.run(['adb', 'connect', ip], timeout=5, check=False)
        except subprocess.TimeoutExpired:
            return jsonify({'success': False, 'error': 'ADB connection timeout'}), 500

        # Setup environment for scrcpy
        env = os.environ.copy()
        env["DISPLAY"] = ":99"
        env["XAUTHORITY"] = "/root/.Xauthority"
        env["SNAP_LAUNCHER_NOTICE_ENABLED"] = "false"
        env["LD_LIBRARY_PATH"] = "/usr/lib/x86_64-linux-gnu"

        cmd = [
            '/snap/bin/scrcpy', '-s', ip,
            '--render-driver=software', '--no-audio',
            '--max-size=1920', '--always-on-top', '--stay-awake'
        ]

        try:
            process = subprocess.Popen(cmd, env=env)
            active_scrcpy[device_id] = process

            # Start ffmpeg for WebRTC streaming
            rtsp_path = ip.replace('.', '_')
            ffmpeg_cmd = [
                'ffmpeg', '-fflags', 'nobuffer', '-flags', 'low_delay',
                '-probesize', '32', '-analyzeduration', '0',
                '-f', 'x11grab', '-video_size', '1920x1080', '-framerate', '30',
                '-i', ':99.0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-profile:v', 'main', '-level', '4.0', '-pix_fmt', 'yuv420p',
                '-b:v', '3000k', '-maxrate', '3000k', '-bufsize', '600k',
                '-g', '30', '-threads', '2',
                '-f', 'rtsp', '-rtsp_transport', 'tcp', '-flush_packets', '1',
                f'rtsp://localhost:8555/{rtsp_path}'
            ]
            ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            active_ffmpeg[device_id] = ffmpeg_proc

            # Start 30-minute session timeout
            if device_id in session_timers:
                session_timers[device_id].cancel()
            timer = threading.Timer(SESSION_TIMEOUT, _session_timeout, args=[device_id])
            timer.daemon = True
            timer.start()
            session_timers[device_id] = timer

            return jsonify({
                'success': True,
                'redirect_url': '/vnc/vnc.html?autoconnect=true&resize=scale&path=websockify&host=100.67.146.57&port=6080',
                'message': 'VNC + control ready (30 min session)'
            })
        except Exception as e:
            return jsonify({'success': False, 'error': f'Failed to start scrcpy: {str(e)}'}), 500

    except Exception as e:
        log.error(f"Connect error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/disconnect', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('device_disconnect')
def disconnect_device(device_id):
    """Disconnect from device -- kills scrcpy, ffmpeg, ADB, cancels timer."""
    try:
        if device_id in active_scrcpy:
            try:
                active_scrcpy[device_id].terminate()
                active_scrcpy[device_id].wait(timeout=5)
            except Exception:
                try:
                    active_scrcpy[device_id].kill()
                except Exception:
                    pass
            active_scrcpy.pop(device_id, None)

        if device_id in active_ffmpeg:
            try:
                active_ffmpeg[device_id].terminate()
                active_ffmpeg[device_id].wait(timeout=3)
            except Exception:
                try:
                    active_ffmpeg[device_id].kill()
                except Exception:
                    pass
            active_ffmpeg.pop(device_id, None)

        if device_id in session_timers:
            session_timers[device_id].cancel()
            session_timers.pop(device_id, None)

        try:
            conn = _get_conn()
            row = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
            if row:
                ip = row['ip_address'] if isinstance(row, dict) else row[0]
                subprocess.run(['adb', 'disconnect', ip], timeout=5, check=False)
        except Exception:
            pass

        return jsonify({'success': True})
    except Exception as e:
        log.error(f"Disconnect error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# -----------------------------------------------
# ADB Input Injection
# -----------------------------------------------

def _get_device_ip(device_id):
    """Look up IP for a device_id."""
    try:
        conn = _get_conn()
        row = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
        if row:
            return row['ip_address'] if isinstance(row, dict) else row[0]
    except Exception:
        pass
    return None


def _adb_input(ip, *args, timeout=5):
    """Run adb -s <ip> shell input <args>."""
    try:
        r = subprocess.run(['adb', '-s', ip, 'shell', 'input'] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return (r.returncode == 0, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        return (False, '', 'adb input timeout')
    except Exception as e:
        return (False, '', str(e))


@app.route('/api/devices/<int:device_id>/input/tap', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('input_tap')
def input_tap(device_id):
    try:
        ip = _get_device_ip(device_id)
        if not ip:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        data = request.get_json(silent=True) or {}
        x = int(data.get('x', 0)); y = int(data.get('y', 0))
        ok, out, err = _adb_input(ip, 'tap', str(x), str(y))
        if not ok:
            return jsonify({'success': False, 'error': f'adb input tap failed: {err.strip() or out.strip()}'}), 500
        return jsonify({'success': True, 'x': x, 'y': y})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/input/key', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('input_key')
def input_key(device_id):
    try:
        ip = _get_device_ip(device_id)
        if not ip:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        data = request.get_json(silent=True) or {}
        keycode = data.get('keycode')
        if keycode is None:
            return jsonify({'success': False, 'error': 'Missing keycode'}), 400
        ok, out, err = _adb_input(ip, 'keyevent', str(keycode))
        if not ok:
            return jsonify({'success': False, 'error': f'adb input keyevent failed: {err.strip()}'}), 500
        return jsonify({'success': True, 'keycode': keycode})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/input/text', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('input_text')
def input_text(device_id):
    try:
        ip = _get_device_ip(device_id)
        if not ip:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        data = request.get_json(silent=True) or {}
        text = data.get('text', '')
        if not text:
            return jsonify({'success': False, 'error': 'Empty text'}), 400
        text_escaped = text.replace(' ', '%s')
        ok, out, err = _adb_input(ip, 'text', text_escaped, timeout=15)
        if not ok:
            return jsonify({'success': False, 'error': f'adb input text failed: {err.strip()}'}), 500
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/input/swipe', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('input_swipe')
def input_swipe(device_id):
    try:
        ip = _get_device_ip(device_id)
        if not ip:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        data = request.get_json(silent=True) or {}
        x1 = int(data.get('x1', 0)); y1 = int(data.get('y1', 0))
        x2 = int(data.get('x2', 0)); y2 = int(data.get('y2', 0))
        duration = int(data.get('duration', 300))
        ok, out, err = _adb_input(ip, 'swipe', str(x1), str(y1), str(x2), str(y2), str(duration))
        if not ok:
            return jsonify({'success': False, 'error': f'adb input swipe failed: {err.strip()}'}), 500
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/input/longpress', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('input_longpress')
def input_longpress(device_id):
    try:
        ip = _get_device_ip(device_id)
        if not ip:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        data = request.get_json(silent=True) or {}
        x = int(data.get('x', 0)); y = int(data.get('y', 0))
        duration = int(data.get('duration', 800))
        ok, out, err = _adb_input(ip, 'swipe', str(x), str(y), str(x), str(y), str(duration))
        if not ok:
            return jsonify({'success': False, 'error': f'adb input longpress failed: {err.strip()}'}), 500
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/reboot', methods=['POST'])
@api_login_required
@admin_or_operator
@audit('device_reboot')
def reboot_device(device_id):
    """Reboot a device."""
    try:
        conn = _get_conn()
        result = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
        if not result:
            return jsonify({'success': False, 'error': 'Device not found'}), 404

        ip = result['ip_address'] if isinstance(result, dict) else result[0]
        subprocess.run(['adb', 'connect', ip], timeout=3, check=False)
        subprocess.Popen(['adb', '-s', ip, 'reboot'])
        return jsonify({'success': True, 'message': 'Device is rebooting...'})
    except Exception as e:
        log.error(f"Reboot error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/terminal', methods=['POST'])
@api_login_required
@admin_or_operator
def device_terminal(device_id):
    """Open terminal connected to specific device."""
    try:
        conn = _get_conn()
        result = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
        if not result:
            return jsonify({'success': False, 'error': 'Device not found'}), 404

        ip = result['ip_address'] if isinstance(result, dict) else result[0]

        subprocess.run(['adb', 'kill-server'], timeout=3, check=False)
        time.sleep(1)
        subprocess.run(['adb', 'start-server'], timeout=3, check=False)
        time.sleep(1)

        result = subprocess.run(['adb', 'connect', f'{ip}:5555'],
                                capture_output=True, text=True, timeout=5)

        if 'connected' in result.stdout.lower() or 'already connected' in result.stdout.lower():
            return jsonify({'success': True, 'ip': ip, 'message': f'Connected to {ip}'})
        else:
            return jsonify({'success': False, 'error': f'Failed to connect: {result.stdout}'})
    except Exception as e:
        log.error(f"Terminal connect error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# API Routes -- System Management
# ============================================

@app.route('/api/change-password', methods=['POST'])
@api_login_required
@admin_or_operator
def change_password():
    """Change current user's password."""
    try:
        data = request.json
        new_password = data.get('new_password', '')
        if len(new_password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters'}), 400

        p_hash = hashlib.sha256(new_password.encode()).hexdigest()
        conn = _get_conn()
        _execute(conn,
            "UPDATE users SET password_hash=? WHERE username=?",
            (p_hash, session['username']))

        audit_log('password_change', details=f"Password changed for {session['username']}")
        return jsonify({'success': True})
    except Exception as e:
        log.error(f"Change password error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/kill-all-adb', methods=['POST'])
@api_login_required
@admin_required
def kill_all_adb():
    """Kill all ADB, scrcpy, and ffmpeg processes (admin-only)."""
    try:
        subprocess.run(['adb', 'disconnect'], timeout=5, check=False)
        subprocess.run(['pkill', '-9', 'scrcpy'], timeout=5, check=False)
        subprocess.run(['pkill', '-9', 'ffmpeg'], timeout=5, check=False)
        active_scrcpy.clear()
        active_ffmpeg.clear()
        for t in session_timers.values():
            try:
                t.cancel()
            except Exception:
                pass
        session_timers.clear()
        _sid_to_device_id.clear()
        audit_log('kill_all_adb')
        return jsonify({'success': True, 'message': 'All ADB, Scrcpy, and FFmpeg processes terminated'})
    except Exception as e:
        log.error(f"Kill ADB error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/test-email', methods=['POST'])
@api_login_required
@admin_required
def test_email():
    """Send a test email to verify email configuration."""
    try:
        config = load_config()
        if not config.get('email_enabled'):
            return jsonify({'success': False, 'error': 'Email notifications are disabled'}), 400

        notification_email = config.get('notification_email')
        if not notification_email:
            return jsonify({'success': False, 'error': 'No notification email configured'}), 400

        subject = "✅ 607 Streaming Manager - Test Email"
        body = """
        <html><head><style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }
            .container { background-color: white; padding: 30px; border-radius: 10px; max-width: 600px; margin: 0 auto; }
            h2 { color: #2ecc71; }
            .success-icon { font-size: 48px; text-align: center; margin: 20px 0; }
            .info { background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0; }
        </style></head><body><div class="container">
            <div class="success-icon">✅</div>
            <h2 style="text-align:center;">Email Configuration Successful!</h2>
            <p>Your email notifications are configured correctly and working.</p>
            <div class="info">
                <p><strong>Email Notifications:</strong> Enabled</p>
                <p><strong>Check Time:</strong> Daily at 9:00 AM</p>
                <p><strong>Alert Window:</strong> 2 days before expiration</p>
            </div>
            <p style="color:#666;font-size:12px;text-align:center;margin-top:30px;">
                Test email from 607 Streaming Manager
            </p>
        </div></body></html>
        """

        success = send_email(notification_email, subject, body)
        if success:
            return jsonify({'success': True, 'message': f'Test email sent to {notification_email}'})
        else:
            return jsonify({'success': False, 'error': 'Failed to send email. Check SMTP settings.'}), 500
    except Exception as e:
        log.error(f"Test email error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>/credentials', methods=['GET'])
@api_login_required
@admin_or_operator
def get_device_credentials(device_id):
    """Get IPTV username and password for a device."""
    try:
        conn = _get_conn()
        row = _fetchone(conn,
            "SELECT iptv_username, iptv_password FROM devices WHERE id=?",
            (device_id,))
        if not row:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        d = _row_to_dict(row)
        return jsonify({
            'success': True,
            'username': (d.get('iptv_username') if isinstance(d, dict) else (row[0] if row else '')) or '',
            'password': (d.get('iptv_password') if isinstance(d, dict) else (row[1] if row else '')) or ''
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Error Handlers
# ============================================

@app.errorhandler(404)
def not_found(error):
    return render_template('login.html', error="Page not found"), 404


@app.errorhandler(500)
def internal_error(error):
    return "Internal Server Error", 500


# ============================================
# SocketIO Handlers
# ============================================

@socketio.on('register_device')
def handle_register_device(data):
    """Client tells us which device it's controlling."""
    device_id = data.get('device_id')
    if device_id:
        _sid_to_device_id[request.sid] = device_id


@socketio.on('disconnect')
def handle_socket_disconnect():
    """When a browser disconnects, clean up ONLY the device it was controlling."""
    sid = request.sid
    device_id = _sid_to_device_id.pop(sid, None)
    if device_id is None:
        return

    if device_id in active_scrcpy:
        try:
            active_scrcpy[device_id].terminate()
            active_scrcpy[device_id].wait(timeout=3)
        except Exception:
            try:
                active_scrcpy[device_id].kill()
            except Exception:
                pass
        active_scrcpy.pop(device_id, None)

    if device_id in active_ffmpeg:
        try:
            active_ffmpeg[device_id].terminate()
            active_ffmpeg[device_id].wait(timeout=3)
        except Exception:
            try:
                active_ffmpeg[device_id].kill()
            except Exception:
                pass
        active_ffmpeg.pop(device_id, None)

    if device_id in session_timers:
        session_timers[device_id].cancel()
        session_timers.pop(device_id, None)

    try:
        conn = _get_conn()
        row = _fetchone(conn, "SELECT ip_address FROM devices WHERE id=?", (device_id,))
        if row:
            ip = row['ip_address'] if isinstance(row, dict) else row[0]
            subprocess.run(['adb', 'disconnect', ip], timeout=5, check=False)
    except Exception:
        pass


# ============================================
# Application Startup
# ============================================

if __name__ == '__main__':
    # Try MariaDB first, fall back to SQLite
    mariadb_ok = _init_mariadb()
    db_backend = "MariaDB" if mariadb_ok else "SQLite"
    log.info(f"Database backend: {db_backend}")

    # Initialize database tables
    init_db()

    # Startup cleanup -- kill orphaned scrcpy/ffmpeg from previous runs
    try:
        subprocess.run(['pkill', '-9', 'scrcpy'], timeout=3, check=False)
        subprocess.run(['pkill', '-9', 'ffmpeg'], timeout=3, check=False)
        subprocess.run(['adb', 'disconnect'], timeout=5, check=False)
    except Exception:
        pass

    # Start device monitoring thread
    monitor_thread = threading.Thread(target=check_devices, daemon=True)
    monitor_thread.start()

    # Start email notification thread
    email_thread = threading.Thread(target=check_expiring_subscriptions, daemon=True)
    email_thread.start()

    print()
    print("=" * 60)
    print("607 STREAMING MANAGER v2")
    print("=" * 60)
    print(f"  Database:       {db_backend}")
    print(f"  DB_URL:         {DB_URL or '(not set -- using SQLite)'}")
    print(f"  Session timeout: 30 minutes")
    print(f"  Audit logging:  enabled")
    print(f"  Email:          checks daily at 9:00 AM")
    print(f"  User roles:     admin / operator")
    print("=" * 60)
    print()

    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
