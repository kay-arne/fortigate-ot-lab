"""Web-gebaseerde rollen: SCADA-HMI (L2), historian (L3) en generieke webservices."""
import json
import logging
import sqlite3
import threading
import time
import urllib.request
from html import escape

from flask import Flask, redirect, request

log = logging.getLogger("otsim.web")

PAGE = """<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=5>
<title>{title}</title><style>body{{font-family:sans-serif;background:#1b222a;color:#e6e6e6;margin:30px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #445;padding:5px 10px}}h1{{color:#f0a030}}</style>
</head><body><h1>{title}</h1><p>{sub}</p>{body}</body></html>"""


# --------------------------------------------------------------------------
# SCADA HMI
# --------------------------------------------------------------------------
def run_hmi(cfg):
    from pymodbus.client import ModbusTcpClient
    import snap7

    args = cfg["args"]
    values: dict = {}

    def poll():
        clients = {ip: ModbusTcpClient(ip, port=502, timeout=2, retries=0) for ip in args.get("plcs", [])}
        s7 = snap7.client.Client() if args.get("s7") else None
        tick = 0
        while True:
            for ip, c in clients.items():
                try:
                    rr = c.read_holding_registers(0, count=4, slave=1)
                    values[ip] = {"regs": rr.registers, "ok": True}
                    # Periodiek de device-identiteit uitlezen (Modbus FC43), zodat de
                    # FortiGate het device over de L2->L1-conduit als Modicon M580
                    # fingerprint en zijn kwetsbaarheden kan correleren.
                    if tick % 15 == 0:
                        try:
                            c.read_device_information(read_code=1, slave=1)
                        except Exception:  # noqa: BLE001 - identificatie is best-effort
                            pass
                except Exception as e:  # noqa: BLE001
                    values[ip] = {"regs": [], "ok": False, "err": str(e)}
                    c.close()
            if s7:
                try:
                    if not s7.get_connected():
                        s7.connect(args["s7"], 0, 1)
                    data = s7.db_read(1, 0, 8)
                    values[args["s7"]] = {"regs": [int.from_bytes(data[i:i + 2], "big", signed=True) for i in (0, 2, 4)]
                                          + [data[6]], "ok": True}
                except Exception as e:  # noqa: BLE001
                    values[args["s7"]] = {"regs": [], "ok": False, "err": str(e)}
                    s7.disconnect()
            tick += 1
            time.sleep(2)

    threading.Thread(target=poll, daemon=True).start()
    app = Flask(__name__)

    def status(ip, v):
        if not v["ok"]:
            return "COMM FAULT"
        # tank-PLC: HR0 niveau (%), HR1 setpoint; boven 95% loopt de tank over
        if ip == args.get("tank") and v["regs"] and v["regs"][0] >= 95:
            return "<b style='color:#ff5a4f'>ALARM: TANK LOOPT OVER</b>"
        return "OK"

    @app.get("/")
    def index():
        rows = "".join(
            f"<tr><td>{ip}</td><td>{status(ip, v)}</td><td>{v['regs']}</td>"
            f"<td><form method=post action=/setpoint><input type=hidden name=ip value={ip}>"
            f"<input name=value size=5><button>Setpoint (HR1)</button></form></td></tr>"
            for ip, v in values.items())
        return PAGE.format(title="SCADA HMI", sub="Live procesdata (L1 via FortiGate)",
                           body=f"<table><tr><th>PLC</th><th>Status</th><th>Waarden</th><th></th></tr>{rows}</table>")

    @app.post("/setpoint")
    def setpoint():
        ip, value = request.form["ip"], int(request.form["value"])
        if ip in args.get("plcs", []):
            c = ModbusTcpClient(ip, port=502, timeout=3, retries=0)
            c.write_register(1, value, slave=1)
            c.close()
        return redirect("/")

    app.run(host="0.0.0.0", port=80)


# --------------------------------------------------------------------------
# Historian
# --------------------------------------------------------------------------
def run_historian(cfg):
    from asyncua.sync import Client as UaClient
    from pymodbus.client import ModbusTcpClient

    args = cfg["args"]
    db = sqlite3.connect("/var/lib/historian.db", check_same_thread=False)
    db.execute("create table if not exists samples (ts real, source text, tag text, value real)")
    lock = threading.Lock()

    def store(source, tag, value):
        with lock:
            db.execute("insert into samples values (?,?,?,?)", (time.time(), source, tag, value))
            db.commit()

    def collect():
        ua = None
        while True:
            # OPC UA (L3 <- L2)
            try:
                if ua is None:
                    ua = UaClient(args["opcua"], timeout=4)
                    ua.connect()
                plant = ua.nodes.objects.get_child(["2:Plant"])
                for plc in plant.get_children():
                    for var in plc.get_children():
                        store("opcua", f"{plc.read_browse_name().Name}.{var.read_browse_name().Name}",
                              float(var.read_value()))
            except Exception as e:  # noqa: BLE001
                log.info("OPC UA niet bereikbaar: %s", e)
                try:
                    ua and ua.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                ua = None
            # Modbus read-only (L3 -> L1, via OT-readonly profiel)
            for ip in args.get("modbus", []):
                c = ModbusTcpClient(ip, port=502, timeout=2, retries=0)
                try:
                    rr = c.read_holding_registers(0, count=2, slave=1)
                    store("modbus", f"{ip}.HR0", rr.registers[0])
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    c.close()
            # replicatie naar de DMZ-mirror (L3 -> L3.5, push)
            try:
                with lock:
                    rows = db.execute("select * from samples order by ts desc limit 20").fetchall()
                req = urllib.request.Request("http://10.35.0.30/ingest", data=json.dumps(rows).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)

    threading.Thread(target=collect, daemon=True).start()
    app = Flask(__name__)

    @app.get("/")
    def index():
        with lock:
            rows = db.execute("select * from samples order by ts desc limit 40").fetchall()
        body = "<table><tr><th>tijd</th><th>bron</th><th>tag</th><th>waarde</th></tr>" + "".join(
            f"<tr><td>{time.strftime('%H:%M:%S', time.localtime(r[0]))}</td><td>{r[1]}</td>"
            f"<td>{escape(r[2])}</td><td>{r[3]}</td></tr>" for r in rows) + "</table>"
        return PAGE.format(title="Process Historian", sub="L3 - verzamelt via OPC UA en Modbus (read-only)", body=body)

    app.run(host="0.0.0.0", port=8080)


# --------------------------------------------------------------------------
# Generieke webservice (MES, patch server, DMZ-mirror)
# --------------------------------------------------------------------------
def run_webservice(cfg):
    title = cfg["args"].get("title", cfg["name"])
    received: list = []
    app = Flask(__name__)

    @app.get("/")
    def index():
        body = f"<p>Host {cfg['name']} ({cfg['ip']})</p>"
        if received:
            body += "<h3>Laatste ontvangen data</h3><pre>" + escape(json.dumps(received[-1])[:2000]) + "</pre>"
        return PAGE.format(title=escape(title), sub=f"segment {cfg['segment']}", body=body)

    @app.post("/ingest")
    def ingest():
        received.append(request.get_json(silent=True))
        del received[:-5]
        return {"ok": True}

    app.run(host="0.0.0.0", port=int(cfg["args"].get("port", 80)))
