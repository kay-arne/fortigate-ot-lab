"""Modbus/TCP PLC's (L1) en field I/O devices (L0) met een eenvoudig procesmodel.

Registermap PLC 'tank':
  HR0 level (%)   HR1 setpoint (%)   HR2 pomp (0/1)   HR9 I/O-communicatiefout
Registermap PLC 'conveyor':
  HR0 snelheid (rpm)   HR1 setpoint (rpm)   HR2 teller   HR3 run (0/1)   HR9 I/O-fout
Field I/O 'level_sensor':  HR0 level, coil0 inlaatpomp
Field I/O 'motor_drive':   HR0 snelheid, HR1 setpoint, coil0 run
"""
import asyncio
import logging
import random
import time

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.datastore import (ModbusSequentialDataBlock, ModbusServerContext,
                                ModbusSlaveContext)
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.server import StartAsyncTcpServer

log = logging.getLogger("otsim.modbus")


def _plc_identity():
    """De PLC presenteert zich via Modbus Read Device Identification (FC43) als een
    Schneider Modicon M580 met firmware 2.70 -- een versie met bekende CVE's (o.a.
    CVE-2018-7846, UMAS DoS). Zo kan de FortiGate het device fingerprinten en zijn
    kwetsbaarheden correleren; dat is de voorwaarde waaronder virtual patching gaat
    ingrijpen."""
    ident = ModbusDeviceIdentification()
    ident.VendorName = "Schneider Electric"
    ident.ProductCode = "BMEP582040"
    ident.VendorUrl = "https://www.se.com"
    ident.ProductName = "Modicon M580"
    ident.ModelName = "BMEP582040"
    ident.MajorMinorRevision = "2.70"
    return ident

# Force Listen Only Mode (Modbus FC08, sub-functie 0x0004): een device dat dit
# commando krijgt stopt met reageren op het netwerk tot het herstart wordt. Het
# staat zo in de Modbus-spec en is een klassieke OT-DoS tegen ongepatchte PLC's.
# Deze devices zijn er kwetsbaar voor; het procesmodel loopt intern door, maar de
# HMI en historian zien de PLC niet meer. De FortiGuard virtual-patch-signature
# Modbus.TCP.Force.Listen.Only (ID 10003675) blokkeert het pakket vóór de PLC.
LISTEN_ONLY_SECONDS = 20


def _listen_only_hooks(name):
    """pymodbus-hooks die Force Listen Only Mode nabootsen: het frame herkennen
    (request_tracer) en daarna LISTEN_ONLY_SECONDS lang elk antwoord onderdrukken
    (response_manipulator)."""
    state = {"until": 0.0}

    def tracer(request, *addr):
        if getattr(request, "function_code", None) == 8 and getattr(request, "sub_function_code", None) == 4:
            state["until"] = time.monotonic() + LISTEN_ONLY_SECONDS
            log.warning("%s: Force Listen Only ontvangen -- negeert Modbus %d s", name, LISTEN_ONLY_SECONDS)

    def manip(response):
        if time.monotonic() < state["until"]:
            return b"", True  # listen-only: geen antwoord sturen
        return response, False

    return tracer, manip


def _context():
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 100),
        co=ModbusSequentialDataBlock(0, [0] * 100),
        hr=ModbusSequentialDataBlock(0, [0] * 100),
        ir=ModbusSequentialDataBlock(0, [0] * 100),
        zero_mode=True,
    )
    return store, ModbusServerContext(slaves=store, single=True)


def hr(store, addr):
    return store.getValues(3, addr, 1)[0]


def set_hr(store, addr, value):
    store.setValues(3, addr, [int(max(0, min(65535, value)))])


def coil(store, addr):
    return bool(store.getValues(1, addr, 1)[0])


# --------------------------------------------------------------------------
# L0 field I/O
# --------------------------------------------------------------------------
async def _io_loop(store, kind):
    level, speed = 40.0, 0.0
    while True:
        if kind == "level_sensor":
            inflow = 2.5 if coil(store, 0) else 0.0
            level = max(0.0, min(100.0, level + inflow - 1.2 + random.uniform(-0.2, 0.2)))
            set_hr(store, 0, round(level))
        else:  # motor_drive
            target = hr(store, 1) if coil(store, 0) else 0
            speed += (target - speed) * 0.3
            set_hr(store, 0, round(speed + random.uniform(-2, 2)) if speed > 1 else 0)
        await asyncio.sleep(1)


def run_io(cfg):
    store, ctx = _context()
    kind = cfg["args"].get("kind", "level_sensor")

    tracer, manip = _listen_only_hooks(cfg["name"])

    async def main():
        asyncio.create_task(_io_loop(store, kind))
        log.info("field I/O %s op :502", kind)
        await StartAsyncTcpServer(context=ctx, address=("0.0.0.0", 502),
                                  request_tracer=tracer, response_manipulator=manip)
    asyncio.run(main())


# --------------------------------------------------------------------------
# L1 PLC
# --------------------------------------------------------------------------
async def _plc_loop(store, process, io_ip):
    client = AsyncModbusTcpClient(io_ip, port=502, timeout=2, retries=0) if io_ip else None
    if process == "tank":
        set_hr(store, 1, 50)
    else:
        set_hr(store, 1, 1200)
        set_hr(store, 3, 1)
    while True:
        try:
            if client and not client.connected:
                await client.connect()
            if process == "tank":
                rr = await client.read_holding_registers(0, count=1, slave=1)
                level = rr.registers[0]
                set_hr(store, 0, level)
                sp = hr(store, 1)
                pump = hr(store, 2)
                if level < sp - 5:
                    pump = 1
                elif level > sp + 5:
                    pump = 0
                set_hr(store, 2, pump)
                await client.write_coil(0, bool(pump), slave=1)
            else:
                await client.write_register(1, hr(store, 1), slave=1)
                await client.write_coil(0, bool(hr(store, 3)), slave=1)
                rr = await client.read_holding_registers(0, count=1, slave=1)
                set_hr(store, 0, rr.registers[0])
                if rr.registers[0] > 100:
                    set_hr(store, 2, (hr(store, 2) + 1) % 65535)
            set_hr(store, 9, 0)
        except Exception as e:  # noqa: BLE001 - I/O-fout = bit zetten en doorgaan
            set_hr(store, 9, 1)
            log.debug("I/O-fout: %s", e)
            if client:
                client.close()
        await asyncio.sleep(1)


def run_plc(cfg):
    store, ctx = _context()
    args = cfg["args"]

    tracer, manip = _listen_only_hooks(cfg["name"])

    async def main():
        asyncio.create_task(_plc_loop(store, args.get("process", "tank"), args.get("io")))
        log.info("Modbus PLC (%s) op :502, device-id Modicon M580", args.get("process"))
        await StartAsyncTcpServer(context=ctx, address=("0.0.0.0", 502), identity=_plc_identity(),
                                  request_tracer=tracer, response_manipulator=manip)
    asyncio.run(main())
