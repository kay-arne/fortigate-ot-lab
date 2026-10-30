"""otprobe - testgereedschap voor het testcenter.

Elk commando print als laatste regel één JSON-object: {"ok": bool, "detail": ...}.
'ok' betekent dat de actie end-to-end slaagde (dus níet tegengehouden is).

  scan <target> <ports>             TCP connect-scan (nmap)
  tcp <ip> <port>                   TCP-verbinding opzetten
  http <url>                        HTTP GET
  modbus-read <ip> <addr> <count>   holding registers lezen
  modbus-write <ip> <addr> <value>  holding register schrijven
  s7-read <ip>                      CPU-status + DB1 lezen
  s7-stop <ip> / s7-start <ip>      CPU naar STOP / RUN
  opcua-browse <url>                OPC UA-objecten opvragen
"""
import json
import socket
import subprocess
import sys
import urllib.request


def out(ok, detail):
    print(json.dumps({"ok": bool(ok), "detail": detail}))
    sys.exit(0)


def cmd_scan(target, ports):
    p = subprocess.run(["nmap", "-Pn", "-n", "-sT", "--max-retries", "1", "--host-timeout", "60s",
                        "-p", ports, "-oG", "-", target], capture_output=True, text=True, timeout=600)
    found = []
    for line in p.stdout.splitlines():
        if "Ports:" in line:
            host = line.split()[1]
            for entry in line.split("Ports:")[1].split(","):
                parts = entry.strip().split("/")
                if len(parts) > 1 and parts[1] == "open":
                    found.append(f"{host}:{parts[0]}")
    out(found, {"open": found} if found else "geen open poorten gevonden")


def cmd_tcp(ip, port):
    try:
        with socket.create_connection((ip, int(port)), timeout=5):
            out(True, f"verbonden met {ip}:{port}")
    except OSError as e:
        out(False, f"{ip}:{port} - {e}")


def cmd_http(url):
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            out(True, f"HTTP {r.status}, {len(r.read())} bytes")
    except Exception as e:  # noqa: BLE001
        out(False, str(e))


def _modbus(ip):
    from pymodbus.client import ModbusTcpClient
    c = ModbusTcpClient(ip, port=502, timeout=4, retries=0)
    if not c.connect():
        out(False, f"geen TCP-verbinding met {ip}:502")
    return c


def cmd_modbus_read(ip, addr, count):
    c = _modbus(ip)
    try:
        rr = c.read_holding_registers(int(addr), count=int(count), slave=1)
        if rr.isError():
            out(False, f"Modbus exception: {rr}")
        out(True, {"registers": rr.registers})
    except Exception as e:  # noqa: BLE001
        out(False, f"geen Modbus-antwoord (sessie onderbroken?): {e}")
    finally:
        c.close()


def cmd_modbus_write(ip, addr, value):
    c = _modbus(ip)
    try:
        rr = c.write_register(int(addr), int(value), slave=1)
        if rr.isError():
            out(False, f"Modbus exception: {rr}")
        out(True, f"HR{addr} := {value}")
    except Exception as e:  # noqa: BLE001
        out(False, f"geen Modbus-antwoord (sessie onderbroken?): {e}")
    finally:
        c.close()


def _s7(ip):
    import snap7
    c = snap7.client.Client()
    try:
        c.connect(ip, 0, 1)
    except Exception as e:  # noqa: BLE001
        out(False, f"geen S7-verbinding met {ip}:102 - {e}")
    return c


def cmd_s7_read(ip):
    c = _s7(ip)
    try:
        state = c.get_cpu_state()
        data = c.db_read(1, 0, 8)
        out(True, {"cpu": state, "db1": list(data)})
    except Exception as e:  # noqa: BLE001
        out(False, str(e))


def cmd_s7_stop(ip):
    c = _s7(ip)
    try:
        c.plc_stop()
        out(True, "CPU STOP verstuurd en bevestigd")
    except Exception as e:  # noqa: BLE001
        out(False, str(e))


def cmd_s7_start(ip):
    c = _s7(ip)
    try:
        c.plc_hot_start()
        out(True, "CPU RUN verstuurd")
    except Exception as e:  # noqa: BLE001
        out(False, str(e))


def cmd_opcua_browse(url):
    from asyncua.sync import Client
    try:
        c = Client(url, timeout=5)
        c.connect()
        try:
            names = [n.read_browse_name().Name for n in c.nodes.objects.get_children()]
        finally:
            c.disconnect()
        out(True, {"objects": names})
    except Exception as e:  # noqa: BLE001
        out(False, str(e))


COMMANDS = {
    "scan": cmd_scan, "tcp": cmd_tcp, "http": cmd_http,
    "modbus-read": cmd_modbus_read, "modbus-write": cmd_modbus_write,
    "s7-read": cmd_s7_read, "s7-stop": cmd_s7_stop, "s7-start": cmd_s7_start,
    "opcua-browse": cmd_opcua_browse,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        out(False, "onbekend commando")
    try:
        COMMANDS[argv[0]](*argv[1:])
    except TypeError:
        out(False, f"verkeerde argumenten voor {argv[0]}")


if __name__ == "__main__":
    main(sys.argv[1:])
