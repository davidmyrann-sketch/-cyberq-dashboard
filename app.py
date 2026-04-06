#!/usr/bin/env python3
"""
CyberQ Dashboard — MQTT-basert, kobler til via myflameboss.com
Device ID: 324579 | Host: s2.myflameboss.com
"""
import json, time, threading, ssl
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

# ── State ─────────────────────────────────────────────────────────────────────
history  = deque(maxlen=240)
state    = {
    "connected": False,
    "last_data": 0,       # unix timestamp for siste mottatte temps-melding
    "temps": {},
    "set_temp": None,
    "blower": 0,
    "labels": {},
    "alarms": {},
    "ts": "--",
    "raw_msgs": deque(maxlen=20),
}

def tf_to_c(tenths_f):
    """Tiendels Fahrenheit → Celsius."""
    try:
        return round((int(tenths_f) / 10.0 - 32) * 5 / 9, 1)
    except:
        return None

def c_to_tf(c):
    """Celsius → tiendels Fahrenheit."""
    return int((float(c) * 9 / 5 + 32) * 10)

# ── MQTT callbacks ─────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc, props=None):
    if rc == 0:
        state["connected"] = True
        client.subscribe(TOPIC_DATA)
        client.subscribe(TOPIC_OPEN)
        print(f"✅ MQTT tilkoblet — lytter på enhet {DEVICE_ID}")
        # Be om alle data med én gang
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
            # API sender flat array: tenths of Celsius. -32767 = ikke tilkoblet.
            temps_raw = data.get("temps", [])
            state["temps"] = {}
            for i, raw in enumerate(temps_raw):
                if raw is None or raw <= -1000:
                    c = None          # probe ikke tilkoblet
                else:
                    c = round(raw / 10.0, 1)   # tiendels Celsius → Celsius
                state["temps"][i] = {"c": c, "raw": raw}
            # Blåser: skala 0–10000 → 0–100 %
            raw_blower = int(data.get("blower", 0))
            state["blower"] = raw_blower // 100
            # set_temp kommer også i temps-meldingen (tiendels Fahrenheit)
            if "set_temp" in data:
                state["set_temp"] = data["set_temp"]
            state["last_data"] = time.time()
            # Logg til historikk
            entry = {"ts": ts, "blower": state["blower"]}
            for i, v in state["temps"].items():
                entry[f"probe{i}"] = v["c"]
            history.append(entry)

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
    probes = []
    label_map = state.get("labels", {})
    for i in range(4):
        t_data = state["temps"].get(i, {})
        probes.append({
            "index": i,
            "name":  label_map.get(str(i), f"Probe {i}"),
            "c":     t_data.get("c"),
            "type":  "pit" if i == 0 else "food",
        })
    set_c = tf_to_c(state["set_temp"]) if state["set_temp"] else None
    device_online = (time.time() - state["last_data"]) < 60
    return jsonify({
        "connected":     state["connected"],
        "device_online": device_online,
        "probes":        probes,
        "set_temp_c":    set_c,
        "blower":        state["blower"],
        "ts":            state["ts"],
    })

@app.route("/api/history")
def api_history():
    return jsonify(list(history))

@app.route("/api/set_temp", methods=["POST"])
def api_set_temp():
    """Sett måltemperatur for pit. Body: {temp_c: 120}"""
    body  = request.json
    temp_c = float(body.get("temp_c", 0))
    tf     = c_to_tf(temp_c)
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_temp", "set_temp": tf}))
    return jsonify({"ok": True, "set_c": temp_c, "set_tf": tf})

@app.route("/api/set_blower", methods=["POST"])
def api_set_blower():
    """Manuell blåserstyring. Body: {blower: 50}  (0=auto, 1-100=manuell %)"""
    body = request.json
    pct  = int(body.get("blower", 0))
    pct  = max(0, min(100, pct))
    # Enheten bruker skala 0–10000
    raw  = pct * 100
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_blower", "blower": raw}))
    return jsonify({"ok": True, "blower": pct})

@app.route("/api/set_food_temp", methods=["POST"])
def api_set_food_temp():
    """Sett matalarm-temperatur. Body: {index: 1, temp_c: 75}"""
    body   = request.json
    idx    = int(body.get("index", 1))
    temp_c = float(body.get("temp_c", 0))
    tf     = c_to_tf(temp_c)
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "set_food", "food": idx, "set_temp": tf}))
    return jsonify({"ok": True, "index": idx, "set_c": temp_c})

@app.route("/api/set_label", methods=["POST"])
def api_set_label():
    """Sett probe-navn. Body: {index: 1, label: 'Brisket'}"""
    body  = request.json
    idx   = int(body.get("index", 0))
    label = body.get("label", "")[:16]
    labels = [state["labels"].get(str(i), "") for i in range(4)]
    labels[idx] = label
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "labels", "labels": labels}))
    return jsonify({"ok": True})

@app.route("/api/sync")
def api_sync():
    """Be enheten sende alle data på nytt."""
    mqttc.publish(TOPIC_RECV, json.dumps({"name": "sync"}))
    return jsonify({"ok": True})

@app.route("/api/raw")
def api_raw():
    """Vis siste rå MQTT-meldinger for debugging."""
    return jsonify(list(state["raw_msgs"]))

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8088))
    print(f"CyberQ Dashboard → http://localhost:{port}")
    print(f"Device: {DEVICE_ID} @ {MQTT_HOST}")
    app.run(host="0.0.0.0", port=port, debug=False)
