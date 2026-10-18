/*
 * Dashboard script shared by the login, administrator and client pages.
 * Each init function returns early if its page elements are missing, so one
 * file serves all three pages. The dashboards poll the backend API every
 * second for the latest telemetry and the day and month energy totals.
 */
"use strict";

/* Shorthand for document.getElementById. */
const E = (id) => document.getElementById(id);

/* Sets an element's text if the element exists on this page. */
function text(id, value) {
    const el = E(id);
    if (el) el.textContent = value;
}

/* Calls the API and returns its JSON reply; redirects to the login page if
   the session has expired and throws the server's error message on failure. */
async function requestJSON(url, options = {}) {
    const response = await fetch(url, options);

    if (response.status === 401) {
        location.href = "/login";
        throw new Error("Login required");
    }

    let data = {};

    try {
        data = await response.json();
    } catch (_) {}

    if (!response.ok) {
        throw new Error(data.error || "Request failed");
    }

    return data;
}

/* Sends form-encoded data with a POST request. */
function post(url, data) {
    return requestJSON(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/x-www-form-urlencoded"
        },
        body: new URLSearchParams(data)
    });
}

/* Shows a success or error message next to a control. */
function message(anchorId, value, ok) {
    const anchor = E(anchorId);
    if (!anchor) return;

    let el = anchor.parentElement.querySelector(".action-message");

    if (!el) {
        el = document.createElement("p");
        el.className = "action-message";
        anchor.parentElement.appendChild(el);
    }

    el.textContent = value;
    el.classList.toggle("success", ok);
    el.classList.toggle("error", !ok);
}

/* Initialises common page elements. */
function initCommon() {
    const logout = E("logout-link");
    if (logout) logout.href = "/logout";
}

/* Configures the login form. */
function initLogin() {
    const form = E("login-form");
    const button = E("login-button");
    if (!form || !button) return;
    button.type = "submit";
}

/* Handles administrator meter selection. */
function initMeterSelector() {
    const select = E("meter-select");
    if (!select) return;

    async function selectMeter() {
        const deviceId = select.value;
        if (!deviceId) return;

        try {
            await post("/api/select-meter", {device_id: deviceId});
            await updateLatest();
            await updateEnergy();
            await updateMonthEnergy();
        } catch (error) {
            console.error("Could not select meter:", error);
        }
    }

    select.addEventListener("change", selectMeter);
    if (select.value) selectMeter();
}

/* Disables meter assignment when creating an administrator account. */
function updateUserMeterState() {
    const role = E("new-user-role");
    const meter = E("new-user-meter");
    if (!role || !meter) return;

    const admin = role.value === "admin";
    meter.disabled = admin;
    if (admin) meter.value = "";
}

/* Watches for changes to the new user's role. */
function initUserRole() {
    const role = E("new-user-role");
    if (!role) return;

    role.addEventListener("change", updateUserMeterState);
    updateUserMeterState();
}

/* Updates the meter online status and last telemetry time. */
function setStatus(online, timestamp) {
    const status = E("meter-status");

    if (status) {
        status.textContent = online ? "Meter Online" : "Meter Offline";
        status.classList.toggle("text-success", online);
        status.classList.toggle("text-danger", !online);
    }

    text(
        "last-update",
        timestamp ? "Last update: " + timestamp.replace("T", " ") : "Last update: --"
    );
}

/* Updates the displayed relay state. */
function setLoadState(on) {
    const status = E("load-status");

    if (status) {
        status.textContent = on ? "ON" : "OFF";
        status.classList.toggle("text-success", on);
        status.classList.toggle("text-danger", !on);
    }

    text("load-control-status", "Current status: " + (on ? "ON" : "OFF"));
}

/* Gets and displays the latest meter telemetry. */
async function updateLatest() {
    if (!E("real-power")) return;

    try {
        const data = await requestJSON("/api/latest");

        if (data.status === "waiting") {
            text("real-power", "0.0 W");
            text("balance-remaining", "0.000 kWh");
            text("today-energy", "0.000 kWh");
            text("voltage", "0.0 V");
            text("current", "0.00 A");
            text("power-factor", "0.000");
            text("apparent-power", "0.0 VA");

            if (E("admin-current-balance-1")) {
                text("admin-current-balance-1", "Current Balance: 0.000 kWh");
            }

            setLoadState(false);
            setStatus(false, null);
            return;
        }

        text("real-power", Number(data.p_w || 0).toFixed(1) + " W");
        text("voltage", Number(data.vrms || 0).toFixed(1) + " V");
        text("current", Number(data.irms || 0).toFixed(3) + " A");
        text("power-factor", Number(data.pf || 0).toFixed(3));
        text("apparent-power", Number(data.s_va || 0).toFixed(1) + " VA");

        if (data.balance_kwh !== null && data.balance_kwh !== undefined) {
            const balance = Number(data.balance_kwh).toFixed(3) + " kWh";
            text("balance-remaining", balance);
            text("admin-current-balance-1", "Current Balance: " + balance);
        }

        setLoadState(Boolean(data.load));
        setStatus(Boolean(data.online), data.timestamp || null);
    } catch (error) {
        console.error(error);
    }
}

/* Converts "HH:MM" to minutes after midnight, or null if invalid. */
function parseMinutes(timeString) {
    const parts = String(timeString || "").split(":").map(Number);

    if (
        parts.length < 2 ||
        Number.isNaN(parts[0]) ||
        Number.isNaN(parts[1])
    ) {
        return null;
    }

    return parts[0] * 60 + parts[1];
}

/* Converts minutes after midnight to "HH:MM" for the graph's time axis. */
function formatTime(totalMinutes) {
    totalMinutes = Math.max(0, Math.min(1440, Math.round(totalMinutes)));

    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;

    return String(hours).padStart(2, "0") + ":" +
        String(minutes).padStart(2, "0");
}

/* Rounds a graph axis step up to 1, 2 or 5 times a power of ten. */
function niceStep(value) {
    if (value <= 0) return 1;

    const exponent = Math.floor(Math.log10(value));
    const fraction = value / Math.pow(10, exponent);

    let niceFraction = 1;

    if (fraction <= 1) niceFraction = 1;
    else if (fraction <= 2) niceFraction = 2;
    else if (fraction <= 5) niceFraction = 5;
    else niceFraction = 10;

    return niceFraction * Math.pow(10, exponent);
}

/* Draws the daily cumulative energy graph. */
function drawGraph(points) {
    const canvas = E("energy-chart");
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    const width = canvas.width;
    const height = canvas.height;
    const left = 55;
    const top = 20;
    const right = 20;
    const bottom = 35;
    const graphWidth = width - left - right;
    const graphHeight = height - top - bottom;

    ctx.clearRect(0, 0, width, height);
    ctx.font = "12px Arial";
    ctx.fillStyle = "#6c757d";
    ctx.strokeStyle = "#dee2e6";
    ctx.lineWidth = 1;

    const data = (points || [])
        .map(point => ({
            minutes: parseMinutes(point.time),
            energy: Number(point.energy) || 0
        }))
        .filter(point => point.minutes !== null)
        .sort((a, b) => a.minutes - b.minutes);

    if (!data.length) {
        ctx.fillText("No data recorded today", width / 2 - 60, height / 2);
        return;
    }

    let xMin = data[0].minutes;
    let xMax = data[data.length - 1].minutes;

    xMin = Math.floor(xMin / 15) * 15;
    xMax = Math.ceil(xMax / 15) * 15;

    if (xMax <= xMin) xMax = xMin + 60;
    if (xMax - xMin < 60) xMax = xMin + 60;

    if (xMax > 1440) {
        xMax = 1440;
        xMin = Math.max(0, xMax - 60);
    }

    const maxEnergyRaw = Math.max(...data.map(point => point.energy), 0.001);
    const yStep = niceStep(maxEnergyRaw / 4);
    const yMax = Math.max(yStep * 4, maxEnergyRaw);

    for (let i = 0; i <= 4; i++) {
        const yValue = i * yStep;
        const y = top + graphHeight - (yValue / yMax) * graphHeight;

        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(left + graphWidth, y);
        ctx.stroke();

        ctx.fillText(yValue.toFixed(3), 5, y + 4);
    }

    for (let i = 0; i <= 4; i++) {
        const minuteValue = xMin + i * (xMax - xMin) / 4;
        const x = left + i * graphWidth / 4;

        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + graphHeight);
        ctx.stroke();

        const label = formatTime(minuteValue);
        ctx.fillText(label, x - 18, height - 8);
    }

    ctx.beginPath();
    ctx.rect(left, top, graphWidth, graphHeight);
    ctx.strokeStyle = "#ced4da";
    ctx.stroke();

    ctx.beginPath();
    ctx.strokeStyle = "#0d6efd";
    ctx.lineWidth = 2.5;

    data.forEach((point, i) => {
        const x = left +
            (point.minutes - xMin) / (xMax - xMin) * graphWidth;

        const y = top + graphHeight -
            point.energy / yMax * graphHeight;

        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });

    if (data.length > 1) ctx.stroke();

    ctx.fillStyle = "#0d6efd";

    data.forEach(point => {
        const x = left +
            (point.minutes - xMin) / (xMax - xMin) * graphWidth;

        const y = top + graphHeight -
            point.energy / yMax * graphHeight;

        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
    });
}

/* Gets today's energy usage and updates the graph. */
async function updateEnergy() {
    if (!E("today-energy")) return;

    try {
        const data = await requestJSON("/api/day");
        text("today-energy", Number(data.total || 0).toFixed(3) + " kWh");
        drawGraph(data.points || []);
    } catch (error) {
        console.error(error);
    }
}

/* Gets this month's recorded energy usage. */
async function updateMonthEnergy() {
    if (!E("month-energy")) return;

    try {
        const data = await requestJSON("/api/month");
        text("month-energy", Number(data.total || 0).toFixed(3) + " kWh");
    } catch (error) {
        console.error(error);
    }
}

/* Handles relay ON and OFF controls. */
function initLoadControls() {
    const on = E("load-on-button");
    const off = E("load-off-button");

    if (on) {
        on.addEventListener("click", async () => {
            try {
                await post("/api/load", {value: "ON"});
                message("load-on-button", "Load command sent.", true);
            } catch (error) {
                message("load-on-button", error.message, false);
            }
        });
    }

    if (off) {
        off.addEventListener("click", async () => {
            try {
                await post("/api/load", {value: "OFF"});
                message("load-off-button", "Load command sent.", true);
            } catch (error) {
                message("load-off-button", error.message, false);
            }
        });
    }
}

/* Handles adding or setting the meter balance. */
function initBalanceControls() {
    const input = E("balance-add-input");
    const add = E("balance-add-button");
    const set = E("balance-set-button");
    if (!input) return;

    async function send(action, anchor) {
        const value = input.value.trim();

        if (!value) {
            message(anchor, "Enter a balance value first.", false);
            return;
        }

        try {
            await post("/api/balance", {action, value});
            message(anchor, "Balance command sent.", true);
            input.value = "";
        } catch (error) {
            message(anchor, error.message, false);
        }
    }

    if (add) {
        add.addEventListener(
            "click",
            () => send("add", "balance-add-button")
        );
    }

    if (set) {
        set.addEventListener(
            "click",
            () => send("set", "balance-set-button")
        );
    }
}

/* Creates a new user account. */
function initAddUser() {
    const form = E("add-user-form");
    if (!form) return;

    form.addEventListener("submit", async event => {
        event.preventDefault();

        const role = E("new-user-role").value;

        const data = {
            username: E("new-user-name").value.trim(),
            password: E("new-password").value,
            role,
            device_id: role === "admin" ? "" : E("new-user-meter").value
        };

        if (
            !data.username ||
            !data.password ||
            (role === "client" && !data.device_id)
        ) {
            message("add-user-button", "Complete all required fields.", false);
            return;
        }

        try {
            await post("/api/users", data);
            message("add-user-button", "User added.", true);
            setTimeout(() => location.reload(), 500);
        } catch (error) {
            message("add-user-button", error.message, false);
        }
    });
}

/* Removes the user selected in the existing users table. */
function initRemoveUser() {
    const button = E("remove-user-button");
    if (!button) return;

    button.addEventListener("click", async () => {
        const selected = document.querySelector(
            'input[name="selected-user"]:checked'
        );

        if (!selected) {
            alert("Select a user first.");
            return;
        }

        const username = selected.value;

        if (!confirm(`Remove user "${username}"?`)) return;

        try {
            await requestJSON(`/api/users/${encodeURIComponent(username)}`, {
                method: "DELETE"
            });

            location.reload();
        } catch (error) {
            alert(error.message);
        }
    });
}

/* Starts all page functions once the page has loaded. */
document.addEventListener("DOMContentLoaded", () => {
    initCommon();
    initLogin();
    initMeterSelector();
    initUserRole();
    initLoadControls();
    initBalanceControls();
    initAddUser();
    initRemoveUser();

    updateLatest();
    updateEnergy();
    updateMonthEnergy();

    if (E("real-power")) {
        setInterval(updateLatest, 1000);
        setInterval(updateEnergy, 1000);
        setInterval(updateMonthEnergy, 1000);
    }
});