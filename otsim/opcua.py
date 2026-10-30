"""OPC UA gateway (L2): publiceert PLC-waarden (gelezen via Modbus) als OPC UA-nodes."""
import asyncio
import logging

from asyncua import Server
from pymodbus.client import AsyncModbusTcpClient

log = logging.getLogger("otsim.opcua")


async def _main(cfg):
    server = Server()
    await server.init()
    server.set_endpoint("opc.tcp://0.0.0.0:4840/")
    server.set_server_name(f"otlab {cfg['name']}")
    ns = await server.register_namespace("urn:otlab:plant")
    plant = await server.nodes.objects.add_object(ns, "Plant")

    targets = []
    for ip in cfg["args"].get("plcs", []):
        obj = await plant.add_object(ns, f"PLC_{ip.replace('.', '_')}")
        regs = [await obj.add_variable(ns, f"HR{i}", 0) for i in range(4)]
        fault = await obj.add_variable(ns, "CommFault", False)
        targets.append((AsyncModbusTcpClient(ip, port=502, timeout=2, retries=0), regs, fault))

    async with server:
        log.info("OPC UA server op :4840 met %d PLC's", len(targets))
        while True:
            for client, regs, fault in targets:
                try:
                    if not client.connected:
                        await client.connect()
                    rr = await client.read_holding_registers(0, count=4, slave=1)
                    for var, val in zip(regs, rr.registers):
                        await var.write_value(val)
                    await fault.write_value(False)
                except Exception:  # noqa: BLE001
                    await fault.write_value(True)
                    client.close()
            await asyncio.sleep(2)


def run_gateway(cfg):
    asyncio.run(_main(cfg))
