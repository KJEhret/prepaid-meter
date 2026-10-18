"""Backend for the plug-point prepaid energy meter.

Part of the BEng final-year project "Prepaid Energy Monitoring for Student
Residences" (K. Ehret, Stellenbosch University, 2026). The backend design is
described in Section 3.3.4 of the report.

One Python program links the meters to the web dashboard:
  - A Paho MQTT client subscribes to meter/+/telemetry on the local Mosquitto
    broker and stores every message in meter.db.
  - FastAPI routes, served by Uvicorn on port 8000, provide the login page,
    the administrator and client dashboards, and the JSON API they poll.
  - Load and balance commands from the dashboard are published to the
    selected meter over MQTT.

Databases (created on first start, next to this file):
  meter.db    telemetry table: one row per telemetry message
  clients.db  users table: username, salted PBKDF2 password hash, assigned
              meter and role (admin or client)

On first start, an "admin" account is created. Its password is taken from the
ADMIN_PASSWORD environment variable or, if that is not set, generated and
printed once in the console.
"""

import hashlib
import json
import os
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

TELEMETRY_TOPIC = "meter/+/telemetry"  # "+" matches any meter's device ID
METER_DATABASE = BASE_DIR / "meter.db"
CLIENT_DATABASE = BASE_DIR / "clients.db"
OFFLINE_SECONDS = 5  # a meter is shown offline after 5 s without telemetry

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Login sessions, keyed by the random token stored in the browser cookie.
# They are kept in memory, so restarting the backend logs everyone out.
sessions = {}
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

# Opens a SQLite database and allows rows to be accessed by column name.
def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db

# Hashes a password using PBKDF2 and a random salt.
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    ).hex()
    return f"{salt}${password_hash}"

# Checks a password against the stored password hash.
def check_password(password, saved_hash):
    salt = saved_hash.split("$", 1)[0]
    return hash_password(password, salt) == saved_hash

# Reads URL-encoded form data from an HTTP request.
async def form_data(request):
    raw = (await request.body()).decode()
    return {key: values[0] for key, values in parse_qs(raw).items()}

# Returns the user associated with the current browser session.
def get_user(request):
    return sessions.get(request.cookies.get("session"))

# Checks whether a user has administrator privileges.
def is_admin(user):
    return bool(user and user["role"] == "admin")

# Returns the client's assigned meter or the meter selected by an admin.
def get_device(user):
    if not user:
        return None
    return user.get("selected_device") if is_admin(user) else user.get("device_id")

# Creates the required databases, tables and default administrator account.
def setup_databases():
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
        # Adds the load column to databases created before it existed.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(telemetry)")}
        if "load" not in columns:
            db.execute("ALTER TABLE telemetry ADD COLUMN load INTEGER DEFAULT 0")

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
        # Administrators select a meter on the dashboard instead of having one
        # assigned.
        db.execute("UPDATE users SET device_id = NULL WHERE role = 'admin'")
        existing = db.execute(
            "SELECT id FROM users WHERE role = 'admin'"
        ).fetchone()

        # Creates a default admin account only if no admin exists yet.
        if existing is None:
            admin_password = os.environ.get("ADMIN_PASSWORD")
            if not admin_password:
                admin_password = secrets.token_urlsafe(9)
                print("Created user 'admin' with password:", admin_password)
            db.execute("""
                INSERT INTO users (username, password_hash, device_id, role)
                VALUES (?, ?, NULL, 'admin')
            """, ("admin", hash_password(admin_password)))

# Finds and returns a user account using its username.
def find_user(username):
    with open_db(CLIENT_DATABASE) as db:
        row = db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None

# Returns all users for display on the admin dashboard.
def get_users():
    with open_db(CLIENT_DATABASE) as db:
        rows = db.execute("""
            SELECT username, device_id, role
            FROM users
            ORDER BY username
        """).fetchall()
    return [dict(row) for row in rows]

# Returns all known meter IDs from telemetry and client assignments.
def get_meter_ids():
    meter_ids = set()

    with open_db(METER_DATABASE) as db:
        rows = db.execute("""
            SELECT DISTINCT device_id
            FROM telemetry
            WHERE device_id IS NOT NULL
        """).fetchall()
        meter_ids.update(row["device_id"] for row in rows if row["device_id"])

    with open_db(CLIENT_DATABASE) as db:
        rows = db.execute("""
            SELECT DISTINCT device_id
            FROM users
            WHERE role = 'client' AND device_id IS NOT NULL
        """).fetchall()
        meter_ids.update(row["device_id"] for row in rows if row["device_id"])

    return sorted(meter_ids)

# Calculates the energy consumed by a meter over a selected time period.
# The prefix selects the period by timestamp (e.g. "2026-10" for a month); the
# result is the meter's cumulative energy at the last reading minus the first.
def period_energy(device_id, prefix):
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

    if not first or not last:
        return 0.0
    return max(0.0, last["energy_kwh"] - first["energy_kwh"])

# Subscribes to meter telemetry when the MQTT broker connection is established.
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("MQTT connected")
        client.subscribe(TELEMETRY_TOPIC)
    else:
        print("MQTT connection failed:", reason_code)

# Stores incoming MQTT telemetry messages in the meter database.
def on_message(client, userdata, message):
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

# Displays the login page.
@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": ""}
    )

# Verifies login details and creates a browser session.
@app.post("/login")
async def login(request: Request):
    form = await form_data(request)
    user = find_user(form.get("username", ""))

    if user is None or not check_password(
        form.get("password", ""), user["password_hash"]
    ):
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

# Removes the current login session and returns to the login page.
@app.get("/logout")
def logout(request: Request):
    sessions.pop(request.cookies.get("session"), None)
    response = RedirectResponse("/login")
    response.delete_cookie("session")
    return response

# Displays either the administrator or client dashboard.
@app.get("/")
def dashboard(request: Request):
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

# Changes the meter currently selected by an administrator.
@app.post("/api/select-meter")
async def select_meter(request: Request):
    user = get_user(request)
    if not is_admin(user):
        return JSONResponse({"error": "not allowed"}, status_code=403)

    form = await form_data(request)
    device_id = form.get("device_id", "").strip()

    if device_id not in get_meter_ids():
        return JSONResponse({"error": "invalid meter"}, status_code=400)

    user["selected_device"] = device_id
    return {"status": "ok", "device_id": device_id}

# Returns the most recent telemetry values for the current meter.
@app.get("/api/latest")
def latest(request: Request):
    user = get_user(request)
    if user is None:
        return JSONResponse({"error": "login required"}, status_code=401)

    device_id = get_device(user)
    if not device_id:
        return {"status": "waiting", "online": False}

    with open_db(METER_DATABASE) as db:
        row = db.execute("""
            SELECT * FROM telemetry
            WHERE device_id = ?
            ORDER BY id DESC LIMIT 1
        """, (device_id,)).fetchone()

    if row is None:
        return {"status": "waiting", "online": False}

    data = dict(row)
    age = (datetime.now() - datetime.fromisoformat(data["timestamp"])).total_seconds()
    data["online"] = age <= OFFLINE_SECONDS

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

# Returns the meter's energy consumption data for the current day.
@app.get("/api/day")
def day_data(request: Request):
    user = get_user(request)
    if user is None:
        return JSONResponse({"error": "login required"}, status_code=401)

    device_id = get_device(user)
    if not device_id:
        return {"points": [], "total": 0.0}

    today = datetime.now().date().isoformat()

    # One point per minute: the highest cumulative energy in that minute,
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

    start_energy = rows[0]["energy"]
    points = [
        {"time": row["time"], "energy": max(0.0, row["energy"] - start_energy)}
        for row in rows
    ]
    return {"points": points, "total": points[-1]["energy"]}

# Returns the total energy consumed during the current month.
@app.get("/api/month")
def month_data(request: Request):
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

# Sends an ON or OFF relay command to the selected meter.
@app.post("/api/load")
async def set_load(request: Request):
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

# Allows an administrator to add to or set the selected meter balance.
@app.post("/api/balance")
async def change_balance(request: Request):
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
        float(value)
    except ValueError:
        return JSONResponse({"error": "invalid balance"}, status_code=400)

    if action not in ("add", "set"):
        return JSONResponse({"error": "invalid action"}, status_code=400)

    mqtt_client.publish(f"meter/{device_id}/balance/{action}", value)
    return {"status": "ok"}

# Creates a new administrator or client user account.
@app.post("/api/users")
async def add_user(request: Request):
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

# Removes a user account from the user database.
@app.delete("/api/users/{username}")
def remove_user(username: str, request: Request):
    admin = get_user(request)
    if not is_admin(admin):
        return JSONResponse({"error": "not allowed"}, status_code=403)

    if username == admin["username"]:
        return JSONResponse(
            {"error": "you cannot remove your own account"}, status_code=400
        )

    with open_db(CLIENT_DATABASE) as db:
        user = db.execute(
            "SELECT username FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user is None:
            return JSONResponse({"error": "user not found"}, status_code=404)

        db.execute(
            "DELETE FROM users WHERE username = ?", (username,)
        )

    for token, session in list(sessions.items()):
        if session["username"] == username:
            sessions.pop(token)

    return {"status": "ok"}

# Starts the databases, MQTT client and FastAPI web server.
def start():
    setup_databases()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
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