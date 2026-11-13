"""Descubrimiento ONVIF por WS-Discovery (multicast UDP 239.255.255.250:3702).

Es un atajo: los equipos ONVIF se anuncian solos y no hay que barrer nada.
Pero el multicast no cruza subredes, y dentro de un contenedor con red bridge
normalmente no llega. Por eso aquí es un complemento del barrido TCP, nunca
el único camino: si no contesta nadie, no es un error.
"""
from __future__ import annotations

import asyncio
import re
import socket
import uuid
from dataclasses import dataclass

MULTICAST_ADDR = "239.255.255.250"
MULTICAST_PORT = 3702

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{msg_id}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


@dataclass
class OnvifDevice:
    address: str            # IP desde la que contestó
    xaddrs: list[str]       # URLs del servicio ONVIF
    scopes: list[str]

    @property
    def name(self) -> str:
        for scope in self.scopes:
            if "/name/" in scope:
                return scope.rsplit("/name/", 1)[1].replace("%20", " ")
        return ""

    @property
    def hardware(self) -> str:
        for scope in self.scopes:
            if "/hardware/" in scope:
                return scope.rsplit("/hardware/", 1)[1].replace("%20", " ")
        return ""


class _ProbeProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.responses: dict[str, OnvifDevice] = {}

    def datagram_received(self, data: bytes, addr):
        text = data.decode("utf-8", "replace")
        if "ProbeMatch" not in text:
            return
        xaddrs = re.findall(r"<[^>]*XAddrs[^>]*>([^<]+)<", text)
        scopes = re.findall(r"<[^>]*Scopes[^>]*>([^<]+)<", text)
        self.responses[addr[0]] = OnvifDevice(
            address=addr[0],
            xaddrs=" ".join(xaddrs).split(),
            scopes=" ".join(scopes).split(),
        )


async def discover(timeout: float = 4.0, bind_addr: str = "0.0.0.0") -> list[OnvifDevice]:
    """Lanza un Probe y recoge lo que conteste. Lista vacía no es un fallo."""
    loop = asyncio.get_running_loop()
    message = _PROBE.format(msg_id=uuid.uuid4()).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.bind((bind_addr, 0))
    except OSError:
        sock.close()
        return []
    sock.setblocking(False)

    transport = None
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _ProbeProtocol, sock=sock
        )
        # Se manda varias veces: UDP multicast se pierde con facilidad.
        for _ in range(3):
            transport.sendto(message, (MULTICAST_ADDR, MULTICAST_PORT))
            await asyncio.sleep(0.4)
        await asyncio.sleep(timeout)
        return list(protocol.responses.values())
    except OSError:
        return []
    finally:
        if transport is not None:
            transport.close()
