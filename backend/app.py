"""Backend for the plug-point prepaid energy meter.

Part of the BEng final-year project "Prepaid Energy Monitoring for Student
Residences" (K. Ehret, Stellenbosch University, 2026). The backend design is
described in Section 3.3.4 of the report.

Receives meter readings over MQTT, stores them in meter.db, and serves
the login and dashboards on port 8000. Sends load and balance commands
to meters over MQTT.

User accounts are stored in clients.db. An admin account is created
if none exists, with a random password printed once in the console.

Backend design: report Section 3.3.4.
"""

import hashlib
import json
import math
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import paho.mqtt.client as mqtt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent

MQTT_BROKER = "localhost"
MQTT_PORT = 1883

TELEMETRY_TOPIC = "meter/+/telemetry"  # + matches any device ID
METER_DATABASE = BASE_DIR / "meter.db"
CLIENT_DATABASE = BASE_DIR / "clients.db"
OFFLINE_SECONDS = 5  # a meter is shown offline after 5s without telemetry

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Login sessions, keyed by the random token stored in the browser cookie.
# They are kept in memory so restarting the backend logs everyone out.
sessions = {}
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

def open_db(path):
    """Open a SQLite database with rows accessible by column name.

    Args:
        path: Path to the database file.

    Returns:
        An open sqlite3 connection.
    """
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db

def hash_password(password, salt=None):
    """Hash a password with PBKDF2-SHA256 (100 000 iterations).

    Args:
        password: Plain-text password.
        salt: Hex salt to reuse.

    Returns:
        A "salt$hash" string for storing in clients.db.
    """
    salt = salt or secrets.token_hex(16) # use random 32 character hex string if no salt provided
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    ).hex()
    return f"{salt}${password_hash}"

def check_password(password, saved_hash):
    """Check a password against a stored "salt$hash" string.

    Args:
        password: Password entered at login.
        saved_hash: Stored "salt$hash" string from clients.db.

    Returns:
        True if the password matches.
    """
    salt = saved_hash.split("$", 1)[0]
    if hash_password(password, salt) == saved_hash:
        return True
    else: return False

async def form_data(request):
    """Read URL-encoded form data from a request.

    Returns:
        A dict of field names to values (first value of each field).
    """
    body = await request.body()
    text = body.decode()
    parsed_data = parse_qs(text)

    form = {}
    for key, values in parsed_data.items():
        form[key] = values[0]

    return form

def get_user(request):
    """Return the logged in user's session or None.

    The session is found with the "session" cookie set at login.
    """
    return sessions.get(request.cookies.get("session"))

def is_admin(user):
    """Return True if the user is logged in as an administrator."""
    return bool(user and user["role"] == "admin")

def get_device(user):
    """Return the meter the user is working with.

    Administrators use selected meter on the dashboard, clients use assigned meters.

    Returns:
        The meter's device ID or None
    """
    if not user:
        return None
    
    if is_admin(user):
        return user.get("selected_device")
    else:
        return user.get("device_id")

def setup_databases():
    """Create the meter.db and clients.db tables if they do not exist.

    Creates an "admin" account if no admin user exists with a
    random password printed in the terminal.
    """
    with open_db(METER_DATABASE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                device_id TEXT,
                vrms REAL,
                irms REAL,
                p_w REAL,
                s_va REAL,
                pf REAL,
                energy_kwh REAL,
                balance_kwh REAL,
                load INTEGER
            )
        """)

    with open_db(CLIENT_DATABASE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                device_id TEXT,
                role TEXT
            )
        """)
        
        # Creates a default admin account only if no admin exists yet.
        existing = db.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()
        if existing is None:
            admin_password = secrets.token_urlsafe(9) # creates a random 12 character password
            print("Created user 'admin' with password:", admin_password)
            db.execute("""
                INSERT INTO users (username, password_hash, device_id, role) VALUES (?, ?, NULL, 'admin')""", 
            ("admin", hash_password(admin_password)))

def find_user(username):
    """Return the user account with this username or None."""
    with open_db(CLIENT_DATABASE) as db:
        row = db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None

def get_users():
    """Return all user accounts (username, meter and role) for admin dashboard."""
    with open_db(CLIENT_DATABASE) as db:
        rows = db.execute("""
            SELECT username, device_id, role
            FROM users
            ORDER BY username
        """).fetchall()
    
    users = []
    for row in rows:
        users.append(dict(row))
    return users

def get_meter_ids():
    """Return the sorted IDs of all meters that have sent telemetry."""
    meter_ids = set()

    with open_db(METER_DATABASE) as db:
        rows = db.execute("""
            SELECT DISTINCT device_id
            FROM telemetry
            WHERE device_id IS NOT NULL
        """).fetchall()

    for row in rows:
       meter_ids.add(row["device_id"])

    return sorted(meter_ids)

def period_energy(device_id, prefix):
    """Return the energy a meter used during a period.

    Args:
        device_id: Meter ID.
        prefix: Start of the timestamps in the period, e.g. "2026-10" for a month.

    Returns:
        Last minus first cumulative energy reading in kWh, or 0 if there is no data.
    """
    # Find the first and last energy_kwh readings for the device in the given period
    with open_db(METER_DATABASE) as db:
        first = db.execute("""
            SELECT energy_kwh FROM telemetry
            WHERE device_id = ? AND timestamp LIKE ?
            ORDER BY id ASC LIMIT 1
        """, (device_id, prefix + "%")).fetchone()

        last = db.execute("""
            SELECT energy_kwh FROM telemetry
            WHERE device_id = ? AND timestamp LIKE ?
            ORDER BY id DESC LIMIT 1
        """, (device_id, prefix + "%")).fetchone()

    if first is None or last is None:
        return 0.0
    
    # Return the difference between the last and first readings
    return max(0.0, last["energy_kwh"] - first["energy_kwh"])

def on_connect(client, userdata, flags, reason_code, properties):
    """MQTT callback: subscribe to all meters' telemetry once connected."""
    if reason_code == 0:
        print("MQTT connected")
        client.subscribe(TELEMETRY_TOPIC)
    else:
        print("MQTT connection failed:", reason_code)

def on_message(client, userdata, message):
    """MQTT callback: store one telemetry message in meter.db.

    The meter ID is taken from the topic meter/<device_id>/telemetry, and the
    timestamp is the time the backend received the message.
    """
    try:
        data = json.loads(message.payload.decode())
        device_id = message.topic.split("/")[1]  # meter/<device_id>/telemetry

        with open_db(METER_DATABASE) as db:
            db.execute("""
                INSERT INTO telemetry (
                    timestamp, device_id, vrms, irms, p_w,
                    s_va, pf, energy_kwh, balance_kwh, load
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(timespec="seconds"),
                device_id,
                data.get("vrms", 0),
                data.get("irms", 0),
                data.get("p_w", 0),
                data.get("s_va", 0),
                data.get("pf", 0),
                data.get("energy_kwh", 0),
                data.get("balance_kwh", 0),
                data.get("load", 0)
            ))
    except Exception as error:
        print("MQTT error:", error)

@app.get("/login")
def login_page(request: Request):
    """GET /login: show the login page."""
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": ""}
    )

@app.post("/login")
async def login(request: Request):
    """POST /login: check the username and password and start a session.

    On success, stores a new session, sets its random token as an HttpOnly
    cookie and redirects to the dashboard.
    """
    form = await form_data(request)
    user = find_user(form.get("username", ""))

    if user is None or not check_password(form.get("password", ""), user["password_hash"]):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Incorrect username or password"},
            status_code=401
        )

    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "username": user["username"],
        "role": user["role"],
        "device_id": user["device_id"],
        "selected_device": None
    }

    response = RedirectResponse("/", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response

@app.get("/logout")
def logout(request: Request):
    """GET /logout: end the session and return to the login page."""
    sessions.pop(request.cookies.get("session"), None)
    response = RedirectResponse("/login")
    response.delete_cookie("session")
    return response

@app.get("/")
def dashboard(request: Request):
    """GET /: show the administrator or client dashboard, or redirect to login."""
    user = get_user(request)
    if user is None:
        return RedirectResponse("/login")

    admin = is_admin(user)
    context = {
        "username": user["username"],
        "role": user["role"].title(),
        "device_id": get_device(user),
        "meters": get_meter_ids() if admin else [],
        "users": get_users() if admin else [],
        "selected_device": user.get("selected_device")
    }

    return templates.TemplateResponse(
        request=request,
        name="admin.html" if admin else "client.html",
        context=context
    )

@app.post("/api/select-meter")
async def select_meter(request: Request):
    """POST /api/select-meter: choose which meter an administrator views.

    The choice is stored in the admin's session and used by get_device().
    Admin only.
    """
    user = get_user(request)
    if not is_admin(user):
        return JSONResponse({"error": "not allowed"}, status_code=403)

    form = await form_data(request)
    device_id = form.get("device_id", "").strip()

    if device_id not in get_meter_ids():
        return JSONResponse({"error": "invalid meter"}, status_code=400)

    user["selected_device"] = device_id
    return {"status": "ok", "device_id": device_id}

@app.get("/api/latest")
def latest(request: Request):
    """GET /api/latest: return the latest telemetry for the current meter.

    A meter with no telemetry for OFFLINE_SECONDS is shown as offline with
    its values and load set to 0.
    """
    user = get_user(request)
    if user is None:
        return JSONResponse({"error": "login required"}, status_code=401)

    # Get the meter ID for user
    device_id = get_device(user)
    # Check if the user has a meter assigned or selected
    if not device_id:
        return {"status": "waiting", "online": False}

    with open_db(METER_DATABASE) as db:
        row = db.execute("""
            SELECT * FROM telemetry
            WHERE device_id = ?
            ORDER BY id DESC LIMIT 1
        """, (device_id,)).fetchone()
        
    #check if the meter has sent any telemetry yet
    if row is None:
        return {"status": "waiting", "online": False}

    data = dict(row)
    age = (datetime.now() - datetime.fromisoformat(data["timestamp"])).total_seconds()
    data["online"] = (age <= OFFLINE_SECONDS) # offline if no telemetry for more than 5s

    if not data["online"]:
        data.update({
            "vrms": 0,
            "irms": 0,
            "p_w": 0,
            "s_va": 0,
            "pf": 0,
            "load": 0
        })

    return data

@app.get("/api/day")
def day_data(request: Request):
    """GET /api/day: return today's cumulative energy, one point per minute."""
    user = get_user(request)
    if user is None:
        return JSONResponse({"error": "login required"}, status_code=401)

    device_id = get_device(user)
    if not device_id:
        return {"points": [], "total": 0.0}

    today = datetime.now().date().isoformat()

    # One point per minute, the highest cumulative energy in that minute,
    # shifted so the day starts at zero.
    with open_db(METER_DATABASE) as db:
        rows = db.execute("""
            SELECT substr(timestamp, 12, 5) AS time,
                   MAX(energy_kwh) AS energy
            FROM telemetry
            WHERE device_id = ? AND timestamp LIKE ?
            GROUP BY substr(timestamp, 1, 16)
            ORDER BY timestamp
        """, (device_id, today + "%")).fetchall()

    if not rows:
        return {"points": [], "total": 0.0}
    # first energy reading of the day
    start_energy = rows[0]["energy"]
    
    points = []
    for row in rows:
        points.append({"time": row["time"], "energy": max(0.0, row["energy"] - start_energy)})
        
    return {"points": points, "total": points[-1]["energy"]}

@app.get("/api/month")
def month_data(request: Request):
    """GET /api/month: return the energy used this month."""
    user = get_user(request)
    if user is None:
        return JSONResponse({"error": "login required"}, status_code=401)

    device_id = get_device(user)
    if not device_id:
        return {"total": 0.0}

    current_month = datetime.now().strftime("%Y-%m")
    return {
        "total": period_energy(device_id, current_month)
    }

@app.post("/api/load")
async def set_load(request: Request):
    """POST /api/load: send an ON or OFF command to the current meter over MQTT."""
    user = get_user(request)
    if user is None:
        return JSONResponse({"error": "login required"}, status_code=401)

    device_id = get_device(user)
    if not device_id:
        return JSONResponse({"error": "select a meter first"}, status_code=400)

    form = await form_data(request)
    value = form.get("value", "OFF").upper()

    if value not in ("ON", "OFF"):
        return JSONResponse({"error": "invalid command"}, status_code=400)

    mqtt_client.publish(f"meter/{device_id}/load/set", value)
    return {"status": "ok"}

@app.post("/api/balance")
async def change_balance(request: Request):
    """POST /api/balance: add to or set the current meter's balance over MQTT.

    Admin only.
    """
    user = get_user(request)
    if not is_admin(user):
        return JSONResponse({"error": "not allowed"}, status_code=403)

    device_id = get_device(user)
    if not device_id:
        return JSONResponse({"error": "select a meter first"}, status_code=400)

    form = await form_data(request)
    action = form.get("action", "add")
    value = form.get("value", "")

    try:
        balance = float(value)
    except ValueError:
        return JSONResponse({"error": "invalid balance"}, status_code=400)

    # Rejects negative values, nan and inf
    if not 0 <= balance < math.inf:
        return JSONResponse({"error": "invalid balance"}, status_code=400)

    if action not in ("add", "set"):
        return JSONResponse({"error": "invalid action"}, status_code=400)

    mqtt_client.publish(f"meter/{device_id}/balance/{action}", value)
    return {"status": "ok"}

@app.post("/api/users")
async def add_user(request: Request):
    """POST /api/users: create an administrator or client account.

    Clients must be assigned an existing meter. Admin only.
    """
    admin = get_user(request)
    if not is_admin(admin):
        return JSONResponse({"error": "not allowed"}, status_code=403)

    form = await form_data(request)
    username = form.get("username", "").strip()
    password = form.get("password", "")
    role = form.get("role", "client")
    device_id = form.get("device_id", "").strip() or None

    if not username or not password:
        return JSONResponse(
            {"error": "username and password are required"}, status_code=400
        )

    if role not in ("admin", "client"):
        return JSONResponse({"error": "invalid role"}, status_code=400)

    if role == "admin":
        device_id = None
    elif not device_id or device_id not in get_meter_ids():
        return JSONResponse(
            {"error": "select a valid meter for the client"}, status_code=400
        )

    try:
        with open_db(CLIENT_DATABASE) as db:
            db.execute("""
                INSERT INTO users (username, password_hash, device_id, role)
                VALUES (?, ?, ?, ?)
            """, (username, hash_password(password), device_id, role))
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "username already exists"}, status_code=400)

    return {"status": "ok"}

@app.delete("/api/users/{username}")
def remove_user(username: str, request: Request):
    """DELETE /api/users/<username>: delete an account and end its sessions.

    Administrators cannot delete their own account. Admin only.
    """
    # Check permissions
    admin = get_user(request)
    if not is_admin(admin):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    
    # Check if user is not trying to delete their own account
    if username == admin["username"]:
        return JSONResponse(
            {"error": "you cannot remove your own account"}, status_code=400
        )
    # Remove the user from the database
    with open_db(CLIENT_DATABASE) as db:
        user = db.execute(
            "SELECT username FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user is None:
            return JSONResponse({"error": "user not found"}, status_code=404)

        db.execute(
            "DELETE FROM users WHERE username = ?", (username,)
        )
    # Remove sessions for the deleted user
    for token, session in list(sessions.items()):
        if session["username"] == username:
            sessions.pop(token)

    return {"status": "ok"}

def start():
    """Set up the databases, connect to the MQTT broker and run the web server."""
    setup_databases()
    # Regisrter the MQTT callbacks
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    # Connect to the MQTT broker and start the loop in a background thread
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
    mqtt_client.loop_start()
    
    print("Dashboard: http://localhost:8000")
    print("Other devices on the network: http://<this-computer's-IP>:8000")

    try:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

if __name__ == "__main__":
    start()