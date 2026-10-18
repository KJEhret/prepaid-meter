# Plug-Point Prepaid Energy Meter

Firmware and backend for a prepaid plug-point energy meter.
BEng project *Prepaid Energy Monitoring for Student Residences* (K. Ehret, Stellenbosch University, 2026). Design: report Section 3.3.

- Measures voltage, current, power and energy
- Deducts energy from a prepaid balance; load off when the balance runs out
- Local server stores the data and serves a web dashboard

```
Meter (ESP32-S3) <--MQTT--> Mosquitto broker <--> Python backend <--> Web dashboard
```

## Folders

- `firmware/`: ESP32-S3 code (PlatformIO)
- `backend/`: Python server and web dashboard

## Firmware

1. `firmware/include/wifi_settings.h`: enter Wi-Fi name, password and broker IP address
2. Build and upload with [PlatformIO](https://platformio.org/):

   ```bash
   pio run --target upload
   ```

## Backend

1. Install [Mosquitto](https://mosquitto.org/); start it with this `mosquitto.conf`:

   ```
   listener 1883
   allow_anonymous true
   ```

2. In `backend/`:

   ```bash
   pip install -r requirements.txt
   python app.py
   ```

3. Open `http://localhost:8000`; log in as `admin`
   - Password: printed in the console on first start, or set with the `ADMIN_PASSWORD` environment variable

## MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `meter/<id>/telemetry` | meter → backend, every second | JSON (below) |
| `meter/<id>/load/set` | backend → meter | `ON` or `OFF` |
| `meter/<id>/balance/set` | backend → meter | new balance (kWh) |
| `meter/<id>/balance/add` | backend → meter | kWh to add |

```json
{"vrms":230.1,"irms":4.567,"p_w":1050.0,"s_va":1051.0,"pf":0.999,"energy_kwh":1.234,"balance_kwh":8.766,"load":1}
```
