"""SFF-8636 (QSFP+/QSFP28) transceiver emulation.

The CMIS emulator (``CMISTransceiver``) models a paged CMIS module with a full
field ``MemMap`` and a datapath state machine. SFF-8636 modules are far simpler:
a flat-ish paged EEPROM (lower page 00h + upper pages 00h/03h), no CMIS datapath
state machine, and management that is limited to a handful of control bytes
(TX_DISABLE at 00h:86, power control at 00h:93). SONiC selects the SFF path purely
from EEPROM byte 0 (SFF8024Identifier); once it reads 0x11 (QSFP28) it drives the
port through xcvrd's ``SffManagerTask`` instead of ``CmisManagerTask``.

So rather than a field ``MemMap``, this class backs the module with a raw EEPROM
byte image built to satisfy sonic-platform-common's ``Sff8636Api`` decoder
(identity, DOM monitors + thresholds, and the TX_DISABLE support/mirror the
daemon actually drives). The gRPC layer is byte-oriented, so read/write/Monitor
work unchanged; only ``GetInfo``'s msm/dpsms fields need shimming (SFF has no CMIS
state machine).
"""
import asyncio
import logging
import struct
from enum import Enum

from cmis import EEPROM

from ..proto.emulator_pb2 import ReadRequest, WriteRequest

logger = logging.getLogger(__name__)

# SFF-8024 identifier for QSFP28 ("QSFP28 or later").
QSFP28_IDENTIFIER = 0x11


class SFFModuleState(Enum):
    """Minimal state so server.GetInfo's msm shim (xcvr._state.name) works.

    SFF-8636 has no CMIS module state machine; this only distinguishes present
    from absent for the informational msm field the harness may read."""
    NOT_PRESENT = 0
    READY = 1


def _s16(value_c, scale=256):
    return struct.pack(">h", int(round(value_c * scale)))


def _u16(value, scale):
    return struct.pack(">H", int(round(value * scale)))


def _ascii(text, size):
    raw = text.encode("ascii")[:size]
    return raw + b"\x00" * (size - len(raw))


def build_sff8636_image(eeprom: EEPROM, defaults: dict) -> None:
    """Populate a QSFP28 (SFF-8636) EEPROM image the sonic Sff8636Api can decode.

    Offsets/scales mirror sonic-platform-common's Sff8636MemMap so xcvrd reads
    real identity, DOM and threshold values. Identity strings are overridable via
    the module ``defaults`` (VendorName / VendorPN / VendorSN / Identifier).
    """
    vendor = defaults.get("VendorName", "XCVR-EMU")
    part = defaults.get("VendorPN", "EMU-QSFP28-LR4")
    serial = defaults.get("VendorSN", "SN0123456789")
    identifier = int(defaults.get("Identifier", QSFP28_IDENTIFIER))

    def w(page, offset, data):
        eeprom.write(0, page, offset, len(data), bytes(data))

    # --- lower page 00h: status + live DOM monitors ------------------------
    w(0, 0, bytes([identifier]))     # SFF8024 identifier (QSFP28)
    w(0, 1, bytes([0x00]))           # status/rev-compliance: rev != 2.8 -> temp/volt support default True
    w(0, 2, bytes([0x00]))           # status indicators: bit2 FLAT_MEM=0 -> paged (thresholds accessible)
    w(0, 3, bytes([0x00]))           # RX_LOS: none
    w(0, 4, bytes([0x00]))           # TX_FAULT: none
    w(0, 22, _s16(45.0))             # temperature 45.0 C (S16, 1/256)
    w(0, 26, _u16(3.3, 10000))       # supply voltage 3.3 V (U16, 100 uV)
    for ch in range(4):              # rx power 1.0 mW/lane (U16, 0.1 uW)
        w(0, 34 + ch * 2, _u16(1.0, 10000))
    for ch in range(4):              # tx bias 6.0 mA/lane (U16, 2 uA)
        w(0, 42 + ch * 2, _u16(6.0, 500))
    for ch in range(4):              # tx power 1.0 mW/lane (U16, 0.1 uW)
        w(0, 50 + ch * 2, _u16(1.0, 10000))
    w(0, 86, bytes([0x00]))          # TX_DISABLE: all enabled (daemon-driven)
    w(0, 93, bytes([0x00]))          # power control

    # --- upper page 00h: serial ID -----------------------------------------
    w(0, 129, bytes([0xC0]))         # ext id: power class (bits7:6=11 -> Power Class 4)
    w(0, 130, bytes([0x0C]))         # connector: MPO 1x12
    w(0, 131, bytes([0x02]))         # 10/40G compliance: 40GBASE-LR4 (fiber, not CR -> not copper)
    w(0, 139, bytes([0x05]))         # encoding: 64B/66B
    w(0, 140, bytes([0xFF]))         # nominal BR: >25.4G (see ext BR)
    w(0, 141, bytes([0x00]))         # ext rate select compliance
    w(0, 142, bytes([0x0A]))         # length SMF: 10 km -> cable_type Length(km)
    w(0, 148, _ascii(vendor, 16))    # vendor name
    w(0, 165, bytes([0x01, 0x02, 0x03]))  # vendor OUI
    w(0, 168, _ascii(part, 16))      # vendor part number
    w(0, 184, _ascii("01", 2))       # vendor rev
    w(0, 192, bytes([0x00]))         # ext spec compliance: Unspecified (no CR/ACC/Copper)
    w(0, 195, bytes([0x18]))         # options byte3: bit3 TX_FAULT impl + bit4 TX_DISABLE impl
    w(0, 196, _ascii(serial, 16))    # vendor serial number
    w(0, 212, _ascii("241214AA", 8))  # date code YYMMDD + lot
    w(0, 220, bytes([0x34]))         # diag mon type: temp(bit5)+volt(bit4)+txpower(bit2)

    # --- upper page 03h: DOM thresholds ------------------------------------
    w(3, 128, _s16(75.0)); w(3, 130, _s16(-5.0))
    w(3, 132, _s16(70.0)); w(3, 134, _s16(-10.0))
    w(3, 144, _u16(3.6, 10000)); w(3, 146, _u16(3.0, 10000))
    w(3, 148, _u16(3.5, 10000)); w(3, 150, _u16(3.1, 10000))
    w(3, 176, _u16(2.0, 10000)); w(3, 178, _u16(0.5, 10000))
    w(3, 180, _u16(1.8, 10000)); w(3, 182, _u16(0.6, 10000))
    w(3, 184, _u16(13.0, 500)); w(3, 186, _u16(6.0, 500))
    w(3, 188, _u16(12.0, 500)); w(3, 190, _u16(7.0, 500))
    w(3, 192, _u16(4.0, 10000)); w(3, 194, _u16(1.0, 10000))
    w(3, 196, _u16(3.5, 10000)); w(3, 198, _u16(1.2, 10000))


class SFF8636Transceiver:
    """Emulated SFF-8636 (QSFP28) module: byte-image EEPROM, no CMIS datapath SM."""

    def __init__(self, index: int, config: dict):
        self._index = index
        self._config = config
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._present = False
        # server.GetInfo builds dpsms from this (empty -> no datapath SMs) and
        # reads _state.name for the msm field.
        self._dpsms: dict = {}
        self._state = SFFModuleState.NOT_PRESENT
        self.eeprom = EEPROM()

        if config.get("present"):
            self.plugin()
        else:
            self._init()

    def _init(self) -> None:
        self.eeprom = EEPROM()
        build_sff8636_image(self.eeprom, self._config.get("defaults", {}))
        self._state = SFFModuleState.NOT_PRESENT

    @property
    def present(self) -> bool:
        return self._present

    def read(self, req: ReadRequest) -> bytes:
        if not req.force and not self._present:
            return b"\x00" * req.length
        return self.eeprom.read(req.bank, req.page, req.offset, req.length)

    def write(self, req: WriteRequest) -> None:
        self.eeprom.write(req.bank, req.page, req.offset, req.length, req.data)
        self._queue.put_nowait(req)

    async def plugout(self) -> None:
        self._present = False
        self._state = SFFModuleState.NOT_PRESENT
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                self._task = None
            logger.debug(f"SFF8636 Transceiver({self._index}) task cancelled")

    def plugin(self) -> None:
        if self._task:
            logger.warning(f"SFF8636 Transceiver({self._index}) already running")
            return
        self._init()
        self._present = True
        self._state = SFFModuleState.READY
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        logger.info(f"SFF8636 Transceiver({self._index}) started")
        # Drain the write queue. SFF-8636 management is stateless from the
        # module's side (TX_DISABLE etc. simply persist in EEPROM and are read
        # back by the daemon), so there is no datapath state machine to advance;
        # we consume events to keep the queue bounded and to leave a hook for
        # future reactive behaviour.
        while True:
            req: WriteRequest = await self._queue.get()
            logger.debug(
                f"SFF8636({self._index}) write page={req.page:02X}h "
                f"offset={req.offset} len={req.length}")
