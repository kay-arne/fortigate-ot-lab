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
from pymodbus.pdu import ModbusRequest, ModbusResponse
from pymodbus.server import StartAsyncTcpServer

log = logging.getLogger("otsim.modbus")

# Eén PLC per proces: gedeelde 'stil'-toestand voor de hooks en de UMAS-handler.
_DOWN = {"until": 0.0, "name": "plc"}


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
    ident.MajorMinorRevision = "SV2.70"  # Schneider-firmwarenotatie, vóór de patch van CVE-2018-78xx
    ident.UserApplicationName = "Modicon M580"
    return ident

# Force Listen Only Mode (Modbus FC08, sub-functie 0x0004): een device dat dit
# commando krijgt stopt met reageren op het netwerk tot het herstart wordt. Het
# staat zo in de Modbus-spec en is een klassieke OT-DoS tegen ongepatchte PLC's.
# Deze devices zijn er kwetsbaar voor; het procesmodel loopt intern door, maar de
# HMI en historian zien de PLC niet meer. De FortiGuard virtual-patch-signature
# Modbus.TCP.Force.Listen.Only (ID 10003675) blokkeert het pakket vóór de PLC.
LISTEN_ONLY_SECONDS = 20
UMAS_FC = 0x5A       # Modbus-functiecode die UMAS (Schneider) draagt
UMAS_DOS = 0x11      # RELEASE_PLC_RESERVATION: de M580 UMAS-DoS (CVE-2018-7846)


def _go_down(reason):
    _DOWN["until"] = time.monotonic() + LISTEN_ONLY_SECONDS
    log.warning("%s: %s -- PLC valt %d s stil", _DOWN["name"], reason, LISTEN_ONLY_SECONDS)


class _UmasResponse(ModbusResponse):
    function_code = UMAS_FC

    def __init__(self, payload=b"\xfe\x00", **kwargs):
        super().__init__(**kwargs)
        self.payload = payload

    def encode(self):
        return self.payload

    def decode(self, data):
        self.payload = data


class _UmasRequest(ModbusRequest):
    """Laat de PLC UMAS (Modbus FC 0x5A) 'spreken', zodat de FortiGate de sessie als
    UMAS herkent. Alleen de DoS-functie (0x11) laat de PLC uitvallen; de handshake
    (INIT_COMM 0x01, TAKE_PLC_RESERVATION 0x10) wordt beantwoord zodat de aanvaller
    een echte UMAS-sessie kan opzetten."""
    function_code = UMAS_FC
    _rtu_frame_size = 0  # TCP: lengte komt uit de MBAP-header

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pairing, self.umas_fc, self.umas_data = 0, None, b""

    def decode(self, data):
        self.pairing = data[0] if len(data) > 0 else 0
        self.umas_fc = data[1] if len(data) > 1 else None
        self.umas_data = data[2:]

    def encode(self):
        return bytes([self.pairing, self.umas_fc or 0]) + self.umas_data

    def execute(self, context):
        if self.umas_fc == UMAS_DOS:
            _go_down("UMAS RELEASE_PLC_RESERVATION (CVE-2018-7846 DoS)")
            return _UmasResponse(b"\xfe\x00")
        if self.umas_fc == 0x10:  # reservation -> geef een session key terug
            return _UmasResponse(b"\xfe\x01")
        return _UmasResponse(b"\xfe\x00")  # INIT_COMM e.a.: OK


def _dos_hooks(name):
    """Force Listen Only Mode (FC08, sub 0x0004) nabootsen: herken het frame en laat
    de PLC stilvallen. UMAS (FC 0x5A) gaat via _UmasRequest. De response_manipulator
    onderdrukt alle antwoorden zolang de PLC 'stil' is."""
    _DOWN["name"] = name

    def tracer(request, *addr):
        if getattr(request, "function_code", None) == 8 and getattr(request, "sub_function_code", None) == 4:
            _go_down("Force Listen Only ontvangen")

    def manip(response):
        if time.monotonic() < _DOWN["until"]:
            return b"", True  # stil: geen antwoord sturen
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

    tracer, manip = _dos_hooks(cfg["name"])

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

    tracer, manip = _dos_hooks(cfg["name"])

    async def main():
        asyncio.create_task(_plc_loop(store, args.get("process", "tank"), args.get("io")))
        log.info("Modbus PLC (%s) op :502, device-id Modicon M580", args.get("process"))
        await StartAsyncTcpServer(context=ctx, address=("0.0.0.0", 502), identity=_plc_identity(),
                                  custom_functions=[_UmasRequest],
                                  request_tracer=tracer, response_manipulator=manip)
    asyncio.run(main())
