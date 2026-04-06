#!/usr/bin/env python3
"""
CyberQ Dashboard — MQTT-basert, kobler til via myflameboss.com
Device ID: 324579 | Host: s2.myflameboss.com
"""
import json, time, threading, ssl, os
from flask import Flask, render_template, jsonify, request
from collections import deque
from datetime import datetime
import paho.mqtt.client as mqtt

app = Flask(__name__)

# ── Credentials ───────────────────────────────────────────────────────────────
MQTT_HOST   = "s2.myflameboss.com"
MQTT_PORT   = 8883
MQTT_USER   = "T-252541"
MQTT_PASS   = "hx9HHAh49xSR3F6rb6KyuF87fQADvGai1Q"
DEVICE_ID   = "324579"

TOPIC_DATA  = f"flameboss/{DEVICE_ID}/send/data"
TOPIC_OPEN  = f"flameboss/{DEVICE_ID}/send/open"
TOPIC_RECV  = f"flameboss/{DEVICE_ID}/recv"

# ── Avansert kontrollparametere ────────────────────────────────────────────────
RAMP_WINDOW_C    = 15.0   # start ramp når mat er innen 15°C av mål
RAMP_MAX_REDUCE  = 0.45   # reduser pit-mål med maks 45% under ramp
OPEN_LID_DROP_C  = 8.0    # °C-fall på 30s for å trigge åpent lokk
OPEN_LID_PAUSE_S = 45     # sekunder vifte pauses etter åpent lokk

# ── State ─────────────────────────────────────────────────────────────────────
history  = deque(maxlen=240)
state    = {
    "connected":      False,
    "last_data":      0,
    "temps":          {},
    "set_temp":       None,   # tenths °F fra enheten
    "user_set_temp":  None,   # brukerens ønskede pit-mål (tenths °F)
    "food_alarms":    {},     # {1: tf, 2: tf, 3: tf}
    "blower":         0,
    "labels":         {},
    "alarms":         {},
    "ts":             "--",
    "raw_msgs":       deque(maxlen=20),
}

ctrl = {
    "food_override":       False,
    "food_override_probe": None,
    "ramping":             False,
    "ramp_factor":         0.0,
    "ramp_active_set_c":   None,   # nedskrudd pit-mål under ramp
    "open_lid":            False,
    "open_lid_since":      0.0,
    "pit_history":         deque(maxlen=8),   # (ts, °C)
}

# ── Konverteringshjelpere ──────────────────────────────────────────────────────
def tf_to_c(tenths_f):
    try:
        return round((int(tenths_f) / 10.0 - 32) * 5 / 9, 1)
    except:
        return None

def c_to_tf(c):
    return int((float(c) * 9 / 5 + 32) * 10)

# ── Avansert kontrolllogikk ───────────────────────────────────────────────────
def run_advanced_control():
    """
    Kjøres etter hver temps-melding.
    Håndterer: open lid, food override, ramp.
    Sender MQTT-kommandoer til enheten ved behov.
    """
    now   = time.time()
    pit_c = state["temps"].get(0, {}).get("c")

    # Bruk user_set_temp som referansepunkt (om satt)
    base_tf = state["user_set_temp"] or state["set_temp"]
    base_c  = tf_to_c(base_tf) if base_tf else None

    # ── 1. Open lid detection ──────────────────────────────────────────────────
    if pit_c is not None:
        ctrl["pit_history"].append((now, pit_c))
        hist = list(ctrl["pit_history"])
        if len(hist) >= 2:
            for old_ts, old_t in hist[:-1]:
                if now - old_ts <= 30 and (old_t - pit_c) >= OPEN_LID_DROP_C:
                    if not ctrl["open_lid"]:
                        ctrl["open_lid"]       = True
                        ctrl["open_lid_since"] = now
                        print(f"🔓 Lokk åpnet — fall {old_t:.1f}→{pit_c:.1f}°C")
                    break

    if ctrl["open_lid"]:
        elapsed = now - ctrl["open_lid_since"]
        # Gjenåpne: temp har stabilisert seg ELLER timeout
        hist = list(ctrl["pit_history"])
        recovered = (
            len(hist) >= 3
            and all(abs(hist[i][1] - hist[i-1][1]) < 2 for i in range(-2, 0))
        )
        if elapsed > OPEN_LID_PAUSE_S or recovered:
            ctrl["open_lid"] = False
            print("🔒 Lokk lukket igjen — gjenopptar normal drift")
        else:
            # Stopp viften, ikke endre set_temp
            mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_blower", "blower": 0}))
            return

    # ── 2. Food override: mat ferdig → stopp vifte ─────────────────────────────
    ctrl["food_override"] = False
    for idx in range(1, 4):
        alarm_tf = state["food_alarms"].get(idx)
        food_c   = state["temps"].get(idx, {}).get("c")
        if alarm_tf and food_c is not None:
            alarm_c = tf_to_c(alarm_tf)
            if food_c >= alarm_c:
                ctrl["food_override"]       = True
                ctrl["food_override_probe"] = idx
                ctrl["ramping"]             = False
                ctrl["ramp_factor"]         = 0.0
                print(f"✅ Mat {idx} ferdig: {food_c}°C ≥ {alarm_c}°C — vifte stoppt")
                mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_blower", "blower": 0}))
                return

    # ── 3. Ramp: senk pit-mål gradvis når mat nærmer seg target ───────────────
    if base_c is not None:
        worst_factor = 0.0
        for idx in range(1, 4):
            alarm_tf = state["food_alarms"].get(idx)
            food_c   = state["temps"].get(idx, {}).get("c")
            if alarm_tf and food_c is not None:
                alarm_c = tf_to_c(alarm_tf)
                gap = alarm_c - food_c
                if 0 < gap < RAMP_WINDOW_C:
                    factor = (RAMP_WINDOW_C - gap) / RAMP_WINDOW_C
                    worst_factor = max(worst_factor, factor)

        if worst_factor > 0.01:
            reduced_c = base_c * (1.0 - worst_factor * RAMP_MAX_REDUCE)
            reduced_c = round(max(60.0, reduced_c), 1)
            if not ctrl["ramping"] or abs((ctrl["ramp_active_set_c"] or 0) - reduced_c) > 2:
                ctrl["ramping"]           = True
                ctrl["ramp_factor"]       = round(worst_factor, 2)
                ctrl["ramp_active_set_c"] = reduced_c
                new_tf = c_to_tf(reduced_c)
                print(f"📉 Ramp: pit → {reduced_c}°C (faktor {worst_factor:.2f})")
                mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_temp", "set_temp": new_tf}))
        else:
            # Ramp ferdig — gjenopprett brukerens pit-mål
            if ctrl["ramping"] and base_tf:
                ctrl["ramping"]           = False
                ctrl["ramp_factor"]       = 0.0
                ctrl["ramp_active_set_c"] = None
                print(f"✅ Ramp ferdig — gjenoppretter pit {base_c}°C")
                mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_temp", "set_temp": base_tf}))

# ── MQTT callbacks ─────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc, props=None):
    if rc == 0:
        state["connected"] = True
        client.subscribe(TOPIC_DATA)
        client.subscribe(TOPIC_OPEN)
        print(f"✅ MQTT tilkoblet — lytter på enhet {DEVICE_ID}")
        client.publish(TOPIC_RECV, json.dumps({"name": "sync"}))
    else:
        state["connected"] = False
        print(f"❌ MQTT feil: {rc}")

def on_disconnect(client, userdata, rc, props=None, reason=None):
    state["connected"] = False
    print("MQTT frakoblet, kobler til på nytt...")

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
        name = data.get("name", "")
        ts   = datetime.now().strftime("%H:%M:%S")
        state["ts"] = ts
        state["raw_msgs"].appendleft({"ts": ts, "topic": msg.topic, "data": data})

        if name == "temps":
            temps_raw = data.get("temps", [])
            state["temps"] = {}
            for i, raw in enumerate(temps_raw):
                c = None if (raw is None or raw <= -1000) else round(raw / 10.0, 1)
                state["temps"][i] = {"c": c, "raw": raw}

            raw_blower = int(data.get("blower", 0))
            state["blower"] = raw_blower // 100

            if "set_temp" in data:
                state["set_temp"] = data["set_temp"]
                # Første gang: sett user_set_temp fra enheten
                if state["user_set_temp"] is None:
                    state["user_set_temp"] = data["set_temp"]

            state["last_data"] = time.time()

            entry = {"ts": ts, "blower": state["blower"]}
            for i, v in state["temps"].items():
                entry[f"probe{i}"] = v["c"]
            history.append(entry)

            # Kjør avansert kontroll i bakgrunnen
            threading.Thread(target=run_advanced_control, daemon=True).start()

        elif name == "set_temp":
            state["set_temp"] = data.get("set_temp")

        elif name == "labels":
            state["labels"] = {str(i): v for i, v in enumerate(data.get("labels", []))}

        elif name in ("meat_alarm", "pit_alarm"):
            state["alarms"][name] = data

    except Exception as e:
        print(f"Meldingsfeil: {e} — {msg.payload}")

# ── MQTT klient ───────────────────────────────────────────────────────────────
mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
mqttc.tls_insecure_set(True)
mqttc.on_connect    = on_connect
mqttc.on_disconnect = on_disconnect
mqttc.on_message    = on_message

def mqtt_thread():
    while True:
        try:
            mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            mqttc.loop_forever()
        except Exception as e:
            print(f"MQTT feil: {e} — prøver igjen om 10s")
            time.sleep(10)

t = threading.Thread(target=mqtt_thread, daemon=True)
t.start()

# ── Flask ruter ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", device_id=DEVICE_ID)

@app.route("/api/status")
def api_status():
    probes    = []
    label_map = state.get("labels", {})
    for i in range(4):
        t_data = state["temps"].get(i, {})
        probes.append({
            "index": i,
            "name":  label_map.get(str(i), f"Probe {i}"),
            "c":     t_data.get("c"),
            "type":  "pit" if i == 0 else "food",
        })
    set_c      = tf_to_c(state["set_temp"]) if state["set_temp"] else None
    user_set_c = tf_to_c(state["user_set_temp"]) if state["user_set_temp"] else None
    food_alarm_c = {str(k): tf_to_c(v) for k, v in state["food_alarms"].items() if v}
    device_online = (time.time() - state["last_data"]) < 60
    return jsonify({
        "connected":     state["connected"],
        "device_online": device_online,
        "probes":        probes,
        "set_temp_c":    set_c,
        "user_set_temp_c": user_set_c,
        "food_alarm_c":  food_alarm_c,
        "blower":        state["blower"],
        "ts":            state["ts"],
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
    body   = request.json
    temp_c = float(body.get("temp_c", 0))
    tf     = c_to_tf(temp_c)
    state["user_set_temp"] = tf          # lagre brukerens ønskede mål
    ctrl["ramping"]        = False       # reset ramp ved ny brukerinnstilling
    ctrl["ramp_factor"]    = 0.0
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_temp", "set_temp": tf}))
    return jsonify({"ok": True, "set_c": temp_c})

@app.route("/api/set_blower", methods=["POST"])
def api_set_blower():
    body = request.json
    pct  = int(body.get("blower", 0))
    if pct < 0:
        mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_blower", "blower": -1}))
        return jsonify({"ok": True, "blower": "auto"})
    pct = max(0, min(100, pct))
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_blower", "blower": pct * 100}))
    return jsonify({"ok": True, "blower": pct})

@app.route("/api/set_food_temp", methods=["POST"])
def api_set_food_temp():
    body   = request.json
    idx    = int(body.get("index", 1))
    temp_c = float(body.get("temp_c", 0))
    tf     = c_to_tf(temp_c)
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_food", "food": idx, "set_temp": tf}))
    state["food_alarms"][idx] = tf
    # Reset food override for denne proben
    if ctrl["food_override_probe"] == idx:
        ctrl["food_override"] = False
    return jsonify({"ok": True, "index": idx, "set_c": temp_c})

@app.route("/api/set_label", methods=["POST"])
def api_set_label():
    body  = request.json
    idx   = int(body.get("index", 0))
    label = body.get("label", "")[:16]
    labels = [state["labels"].get(str(i), "") for i in range(4)]
    labels[idx] = label
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "labels", "labels": labels}))
    return jsonify({"ok": True})

@app.route("/api/sync")
def api_sync():
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "sync"}))
    return jsonify({"ok": True})

@app.route("/api/raw")
def api_raw():
    return jsonify(list(state["raw_msgs"]))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8088))
    print(f"CyberQ Dashboard → http://localhost:{port}")
    print(f"Device: {DEVICE_ID} @ {MQTT_HOST}")
    app.run(host="0.0.0.0", port=port, debug=False)
