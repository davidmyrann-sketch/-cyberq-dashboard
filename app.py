#!/usr/bin/env python3
"""
CyberQ Dashboard — HTTP polling mot myflameboss.com API
Data leses via REST (fungerer overalt), kommandoer sendes via MQTT hvis tilkoblet.
Device ID: 324579
"""
import json, time, threading, ssl, os, uuid
from flask import Flask, render_template, jsonify, request
from collections import deque
from datetime import datetime, timezone
import urllib.request
import paho.mqtt.client as mqtt

app = Flask(__name__)

# ── Credentials ───────────────────────────────────────────────────────────────
API_BASE    = "https://myflameboss.com/api/v1"
MQTT_HOST   = "s2.myflameboss.com"
MQTT_PORT   = 8084           # WebSocket over TLS
MQTT_USER   = "T-252541"
MQTT_PASS   = "hx9HHAh49xSR3F6rb6KyuF87fQADvGai1Q"
DEVICE_ID   = "324579"
MQTT_CID    = f"cyberq-{uuid.uuid4().hex[:8]}"
POLL_SECS   = 12             # HTTP poll-intervall

import base64, ssl as _ssl
_API_AUTH_HEADER = "Basic " + base64.b64encode(f"{MQTT_USER}:{MQTT_PASS}".encode()).decode()
_SSL_CTX = _ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = _ssl.CERT_NONE

TOPIC_RECV  = f"flameboss/{DEVICE_ID}/recv"

# ── Avansert kontrollparametere ────────────────────────────────────────────────
RAMP_WINDOW_C    = 15.0
RAMP_MAX_REDUCE  = 0.45
OPEN_LID_DROP_C  = 8.0
OPEN_LID_PAUSE_S = 45

# ── State ─────────────────────────────────────────────────────────────────────
history  = deque(maxlen=240)
state    = {
    "connected":      False,   # HTTP API nåbar + device online
    "mqtt_connected": False,
    "last_data":      0,
    "temps":          {},
    "set_temp":       None,    # tideler av °C
    "user_set_temp":  None,    # brukerens pit-mål (tideler av °C)
    "food_alarms":    {},      # {1: tdc, 2: tdc, 3: tdc}
    "blower":         0,
    "labels":         {},
    "ts":             "--",
    "raw_msgs":       deque(maxlen=20),
    "cook_id":        None,
    "last_cnt":       0,
    "cook_timers":    {
        1: {"start": 0.0, "pause_elapsed": 0.0, "running": False},
        2: {"start": 0.0, "pause_elapsed": 0.0, "running": False},
        3: {"start": 0.0, "pause_elapsed": 0.0, "running": False},
    },
}

ctrl = {
    "food_override":       False,
    "food_override_probe": None,
    "ramping":             False,
    "ramp_factor":         0.0,
    "ramp_active_set_c":   None,
    "open_lid":            False,
    "open_lid_since":      0.0,
    "pit_history":         deque(maxlen=8),
    "ramp_locked_until":   0.0,
}

settings = {
    "opendetect":      1,
    "cook_ramp":       0,
    "propband":        5,
    "cyctime":         20,
    "timeout_action":  "Hold",
}

# ── HTTP hjelper ───────────────────────────────────────────────────────────────
def api_get(path):
    req = urllib.request.Request(
        f"{API_BASE}/{path}",
        headers={"Authorization": _API_AUTH_HEADER, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as r:
        return json.loads(r.read())

# ── Konverteringshjelpere ──────────────────────────────────────────────────────
def tdc_to_c(tdc):
    """Tideler av Celsius → Celsius"""
    try:
        v = int(tdc)
        return None if v <= -1000 else round(v / 10.0, 1)
    except:
        return None

def c_to_tdc(c):
    """Celsius → tideler av Celsius (for kommandoer)"""
    return int(float(c) * 10)

# ── Status-beregning ───────────────────────────────────────────────────────────
def compute_probe_status(c, target_c, alarm_c=None):
    if c is None:
        return "ERROR"
    if alarm_c is not None and c >= alarm_c:
        return "DONE"
    if target_c is not None:
        dev = c - target_c
        if dev > 8:  return "HIGH"
        if dev < -8: return "LOW"
    return "OK"

# ── Avansert kontrolllogikk ───────────────────────────────────────────────────
def run_advanced_control():
    now   = time.time()
    pit_c = state["temps"].get(0, {}).get("c")
    base_tdc = state["user_set_temp"] or state["set_temp"]
    base_c   = tdc_to_c(base_tdc) if base_tdc else None

    if pit_c is not None:
        ctrl["pit_history"].append((now, pit_c))
        hist = list(ctrl["pit_history"])
        if len(hist) >= 2 and settings["opendetect"] == 0:
            for old_ts, old_t in hist[:-1]:
                if now - old_ts <= 30 and (old_t - pit_c) >= OPEN_LID_DROP_C:
                    if not ctrl["open_lid"]:
                        ctrl["open_lid"]       = True
                        ctrl["open_lid_since"] = now
                    break
        if ctrl["open_lid"] and settings["opendetect"] == 0:
            elapsed   = now - ctrl["open_lid_since"]
            recovered = len(hist) >= 3 and all(abs(hist[i][1] - hist[i-1][1]) < 2 for i in range(-2, 0))
            if elapsed > OPEN_LID_PAUSE_S or recovered:
                ctrl["open_lid"] = False

    ctrl["food_override"] = False
    for idx in range(1, 4):
        alarm_tdc = state["food_alarms"].get(idx)
        food_c    = state["temps"].get(idx, {}).get("c")
        if alarm_tdc and food_c is not None:
            alarm_c = tdc_to_c(alarm_tdc)
            if food_c >= alarm_c:
                ctrl["food_override"]       = True
                ctrl["food_override_probe"] = idx
                ctrl["ramping"]             = False
                ctrl["ramp_factor"]         = 0.0
                hold_tdc = c_to_tdc(60)
                print(f"✅ Mat {idx} ferdig: {food_c}°C ≥ {alarm_c}°C — senker pit til 60°C")
                mqtt_publish({"name": "set_temp", "set_temp": hold_tdc})
                return

    if now < ctrl["ramp_locked_until"]:
        return

    if base_c is not None:
        worst = 0.0
        for idx in range(1, 4):
            alarm_tdc = state["food_alarms"].get(idx)
            food_c    = state["temps"].get(idx, {}).get("c")
            if alarm_tdc and food_c is not None:
                alarm_c = tdc_to_c(alarm_tdc)
                gap = alarm_c - food_c
                if 0 < gap < RAMP_WINDOW_C:
                    worst = max(worst, (RAMP_WINDOW_C - gap) / RAMP_WINDOW_C)

        if worst > 0.01:
            reduced_c = max(60.0, round(base_c * (1.0 - worst * RAMP_MAX_REDUCE), 1))
            if not ctrl["ramping"] or abs((ctrl["ramp_active_set_c"] or 0) - reduced_c) > 2:
                ctrl["ramping"]           = True
                ctrl["ramp_factor"]       = round(worst, 2)
                ctrl["ramp_active_set_c"] = reduced_c
                mqtt_publish({"name": "set_temp", "set_temp": c_to_tdc(reduced_c)})
        else:
            if ctrl["ramping"] and base_tdc:
                ctrl["ramping"]           = False
                ctrl["ramp_factor"]       = 0.0
                ctrl["ramp_active_set_c"] = None
                mqtt_publish({"name": "set_temp", "set_temp": base_tdc})

# ── HTTP polling thread ───────────────────────────────────────────────────────
def poll_thread():
    """Henter siste data fra Flame Boss HTTP API hvert POLL_SECS sekund."""
    while True:
        try:
            # 1. Enhetsstatus
            device = api_get(f"devices/{DEVICE_ID}")
            online = device.get("online", False)
            state["connected"] = online

            if state["set_temp"] is None or state["user_set_temp"] is None:
                raw_st = device.get("set_temp")
                if raw_st:
                    state["set_temp"] = raw_st
                    if state["user_set_temp"] is None:
                        state["user_set_temp"] = raw_st

            cook_info = device.get("most_recent_cook", {})
            cook_id   = cook_info.get("id") if cook_info else None
            if cook_id:
                state["cook_id"] = cook_id

            # Probe-navn fra enhetsnivå (om tilgjengelig)
            for i in range(4):
                key = f"probe_name_{i}"
                if cook_info and cook_info.get(key):
                    state["labels"].setdefault(str(i), cook_info[key])

            # 2. Siste datapunkter fra cook
            if cook_id and online:
                cook_data = api_get(f"cooks/{cook_id}")
                points    = cook_data.get("data", [])
                if points:
                    # Kun nye punkter
                    new_pts = [p for p in points if p["cnt"] > state["last_cnt"]]
                    if new_pts:
                        state["last_cnt"] = new_pts[-1]["cnt"]

                    # Oppdater state fra siste punkt
                    latest = points[-1]
                    ts = datetime.now().strftime("%H:%M:%S")
                    state["ts"] = ts
                    state["last_data"] = time.time()
                    state["set_temp"] = latest["set_temp"]

                    raw_temps = [
                        latest["pit_temp"],
                        latest["meat_temp1"],
                        latest["meat_temp2"],
                        latest["meat_temp3"],
                    ]
                    state["temps"] = {}
                    for i, raw in enumerate(raw_temps):
                        c = tdc_to_c(raw)
                        state["temps"][i] = {"c": c, "raw": raw}

                    state["blower"] = latest["fan_dc"] // 100

                    # Historikk — legg til nye punkter
                    for p in new_pts:
                        entry = {
                            "ts":     datetime.fromtimestamp(p["sec"]).strftime("%H:%M"),
                            "blower": p["fan_dc"] // 100,
                            "probe0": tdc_to_c(p["pit_temp"]),
                            "probe1": tdc_to_c(p["meat_temp1"]),
                            "probe2": tdc_to_c(p["meat_temp2"]),
                            "probe3": tdc_to_c(p["meat_temp3"]),
                        }
                        history.append(entry)

                    # Kjør avansert kontroll
                    threading.Thread(target=run_advanced_control, daemon=True).start()

                    msg = {"ts": ts, "cook_id": cook_id, "cnt": latest["cnt"],
                           "temps": raw_temps, "set_temp": latest["set_temp"],
                           "blower": latest["fan_dc"]}
                    state["raw_msgs"].appendleft({"ts": ts, "source": "http", "data": msg})

        except Exception as e:
            print(f"HTTP poll feil: {e}")
            state["connected"] = False

        time.sleep(POLL_SECS)

# ── MQTT (kun for kommandosending) ─────────────────────────────────────────────
mqttc = None

def mqtt_publish(payload: dict):
    """Send MQTT-kommando hvis tilkoblet."""
    if mqttc and state["mqtt_connected"]:
        mqttc.publish(TOPIC_RECV, json.dumps(payload))
        return True
    print(f"MQTT ikke tilkoblet — kommando ikke sendt: {payload}")
    return False

def _on_connect(client, userdata, flags, rc, props=None):
    state["mqtt_connected"] = rc == 0
    if rc == 0:
        print(f"✅ MQTT tilkoblet (WebSocket)")
    else:
        print(f"❌ MQTT feil rc={rc}")

def _on_disconnect(client, userdata, *args):
    state["mqtt_connected"] = False
    print("MQTT frakoblet")

def mqtt_thread():
    global mqttc
    delay = 5
    while True:
        try:
            mqttc = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=MQTT_CID,
                transport="websockets",
            )
            mqttc.ws_set_options(path="/mqtt")
            mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
            mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
            mqttc.tls_insecure_set(True)
            mqttc.on_connect    = _on_connect
            mqttc.on_disconnect = _on_disconnect
            mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            mqttc.loop_forever()
            delay = 5
        except Exception as e:
            print(f"MQTT feil: {e} — prøver igjen om {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 120)

# Start bakgrunnstråder
threading.Thread(target=poll_thread,  daemon=True).start()
threading.Thread(target=mqtt_thread,  daemon=True).start()

# ── Flask ruter ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", device_id=DEVICE_ID)

@app.route("/api/status")
def api_status():
    label_map    = state.get("labels", {})
    set_c        = tdc_to_c(state["set_temp"]) if state["set_temp"] else None
    user_set_c   = tdc_to_c(state["user_set_temp"]) if state["user_set_temp"] else None
    food_alarm_c = {str(k): tdc_to_c(v) for k, v in state["food_alarms"].items() if v}

    probes = []
    for i in range(4):
        t_data  = state["temps"].get(i, {})
        c       = t_data.get("c")
        alarm_c = food_alarm_c.get(str(i)) if i > 0 else None
        target  = set_c if i == 0 else None
        probes.append({
            "index":  i,
            "name":   label_map.get(str(i), "Pit" if i == 0 else f"Mat {i}"),
            "c":      c,
            "type":   "pit" if i == 0 else "food",
            "status": compute_probe_status(c, target, alarm_c),
        })

    device_online = (time.time() - state["last_data"]) < 90

    now = time.time()
    timers_out = {}
    for k, v in state["cook_timers"].items():
        elapsed = v["pause_elapsed"]
        if v["running"]:
            elapsed += now - v["start"]
        timers_out[str(k)] = {"running": v["running"], "elapsed": round(elapsed, 1)}

    return jsonify({
        "connected":       state["connected"],
        "mqtt_connected":  state["mqtt_connected"],
        "device_online":   device_online,
        "probes":          probes,
        "set_temp_c":      set_c,
        "user_set_temp_c": user_set_c,
        "food_alarm_c":    food_alarm_c,
        "blower":          state["blower"],
        "ts":              state["ts"],
        "cook_id":         state["cook_id"],
        "settings":        settings,
        "cook_timers":     timers_out,
        "ctrl": {
            "food_override":       ctrl["food_override"],
            "food_override_probe": ctrl["food_override_probe"],
            "ramping":             ctrl["ramping"],
            "ramp_factor":         ctrl["ramp_factor"],
            "ramp_active_set_c":   ctrl["ramp_active_set_c"],
            "open_lid":            ctrl["open_lid"],
        },
    })

@app.route("/api/history")
def api_history():
    return jsonify(list(history))

@app.route("/api/set_temp", methods=["POST"])
def api_set_temp():
    body    = request.json
    temp_c  = float(body.get("temp_c", 0))
    tdc     = c_to_tdc(temp_c)
    state["user_set_temp"]    = tdc
    ctrl["ramping"]           = False
    ctrl["ramp_factor"]       = 0.0
    ctrl["ramp_active_set_c"] = None
    ctrl["ramp_locked_until"] = time.time() + 120
    sent = mqtt_publish({"name": "set_temp", "set_temp": tdc})
    return jsonify({"ok": True, "set_c": temp_c, "mqtt_sent": sent})

@app.route("/api/set_food_temp", methods=["POST"])
def api_set_food_temp():
    body   = request.json
    idx    = int(body.get("index", 1))
    temp_c = float(body.get("temp_c", 0))
    tdc    = c_to_tdc(temp_c)
    state["food_alarms"][idx] = tdc
    if ctrl["food_override_probe"] == idx:
        ctrl["food_override"] = False
    sent = mqtt_publish({"name": "set_food", "food": idx, "set_temp": tdc})
    return jsonify({"ok": True, "index": idx, "set_c": temp_c, "mqtt_sent": sent})

@app.route("/api/set_label", methods=["POST"])
def api_set_label():
    body  = request.json
    idx   = int(body.get("index", 0))
    label = body.get("label", "")[:16]
    labels = [state["labels"].get(str(i), "") for i in range(4)]
    labels[idx] = label
    state["labels"][str(idx)] = label
    mqtt_publish({"name": "labels", "labels": labels})
    return jsonify({"ok": True})

@app.route("/api/timer", methods=["POST"])
def api_timer():
    body   = request.json
    idx    = int(body.get("index", 1))
    action = body.get("action", "start")
    t      = state["cook_timers"].get(idx)
    if t is None:
        return jsonify({"ok": False})
    now = time.time()
    if action == "start":
        if not t["running"]:
            t["start"]   = now
            t["running"] = True
    elif action == "stop":
        if t["running"]:
            t["pause_elapsed"] += now - t["start"]
            t["running"] = False
    elif action == "reset":
        t["start"] = t["pause_elapsed"] = 0.0
        t["running"] = False
    return jsonify({"ok": True})

@app.route("/api/settings", methods=["POST"])
def api_settings():
    body = request.json
    for key in ("opendetect", "cook_ramp", "propband", "cyctime"):
        if key in body:
            settings[key] = int(body[key])
            mqtt_publish({f"name": f"set_{key}", key: settings[key]})
    if "timeout_action" in body:
        settings["timeout_action"] = body["timeout_action"]
        mqtt_publish({"name": "set_timeout_action", "timeout_action": settings["timeout_action"]})
    return jsonify({"ok": True, "settings": settings})

@app.route("/api/sync")
def api_sync():
    mqtt_publish({"name": "sync"})
    return jsonify({"ok": True, "mqtt_connected": state["mqtt_connected"]})

@app.route("/api/raw")
def api_raw():
    return jsonify(list(state["raw_msgs"]))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8088))
    print(f"CyberQ Dashboard → http://localhost:{port}")
    print(f"Device: {DEVICE_ID} @ {API_BASE}")
    app.run(host="0.0.0.0", port=port, debug=False)
