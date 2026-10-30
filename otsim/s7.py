"""Siemens S7comm PLC (L1) met een gesimuleerd mengproces in DB1.

DB1 layout (big-endian):
  0  INT  temperatuur (0.1 °C)     2  INT  setpoint temperatuur
  4  INT  roerder rpm              6  BOOL verwarming aan (byte 6, bit 0)
"""
import ctypes
import logging
import random
import struct
import time

import snap7

log = logging.getLogger("otsim.s7")


def run_plc(cfg):
    server = snap7.server.Server()
    db1 = (ctypes.c_uint8 * 64)()
    server.register_area(snap7.types.srvAreaDB, 1, db1)
    struct.pack_into(">hhh", db1, 0, 200, 650, 120)
    server.start(tcpport=102)
    log.info("S7 PLC op :102 (DB1)")
    temp = 20.0
    try:
        while True:
            _, setpoint, rpm = struct.unpack_from(">hhh", db1, 0)
            heating = temp * 10 < setpoint
            temp += (0.6 if heating else -0.3) + random.uniform(-0.05, 0.05)
            struct.pack_into(">h", db1, 0, int(temp * 10))
            db1[6] = 1 if heating else 0
            while True:
                event = server.pick_event()
                if not event:
                    break
                log.info("S7 event: %s", server.event_text(event))
            time.sleep(1)
    finally:
        server.stop()
        server.destroy()
