"""Start de simulator-rol uit /etc/otsim.json."""
import argparse
import json
import logging
import time

ROLES = {
    "modbus_plc": ("otsim.modbus", "run_plc"),
    "io_device": ("otsim.modbus", "run_io"),
    "s7_plc": ("otsim.s7", "run_plc"),
    "opcua_gw": ("otsim.opcua", "run_gateway"),
    "hmi": ("otsim.web", "run_hmi"),
    "historian": ("otsim.web", "run_historian"),
    "webservice": ("otsim.web", "run_webservice"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/otsim.json")
    cfg = json.load(open(ap.parse_args().config))
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {cfg['name']} %(levelname)s %(message)s")
    role = cfg["role"]
    if role not in ROLES:
        # jumphost / ews / probe: geen service, alleen tooling (ssh, otprobe)
        logging.info("rol %s heeft geen simulator; idle", role)
        while True:
            time.sleep(3600)
    module, func = ROLES[role]
    getattr(__import__(module, fromlist=[func]), func)(cfg)


if __name__ == "__main__":
    main()
