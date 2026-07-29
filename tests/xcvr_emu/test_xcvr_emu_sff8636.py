"""Unit tests for the SFF-8636 (QSFP28) emulated transceiver.

The emulator is otherwise CMIS-only; these lock the SFF-8636 module's byte image
(so sonic-platform-common's Sff8636Api decodes real identity/DOM/thresholds and
routes the port to xcvrd's SffManagerTask) and its stateless TX_DISABLE handling.
"""
import struct

import pytest
import pytest_asyncio

from xcvr_emu.proto.emulator_pb2 import ReadRequest, WriteRequest
from xcvr_emu.transceiver import CMISTransceiver, SFF8636Transceiver, make_transceiver
from xcvr_emu.transceiver.sff8636 import SFFModuleState

SFF_CONFIG = {
    "type": "sff8636",
    "present": True,
    "defaults": {"VendorName": "XCVR-EMU", "VendorPN": "EMU-QSFP28-LR4",
                 "VendorSN": "SN0123456789"},
}


@pytest_asyncio.fixture
async def sff():
    xcvr = SFF8636Transceiver(0, SFF_CONFIG)
    yield xcvr
    await xcvr.plugout()


def _rd(xcvr, page, offset, length, force=True):
    return xcvr.read(ReadRequest(index=0, bank=0, page=page, offset=offset,
                                 length=length, force=force))


def _u16(xcvr, page, off, scale, signed=False):
    raw = struct.unpack(">h" if signed else ">H", _rd(xcvr, page, off, 2))[0]
    return raw / scale


def test_factory_selects_by_type():
    assert isinstance(make_transceiver(0, {"type": "sff8636"}), SFF8636Transceiver)
    assert isinstance(make_transceiver(0, {"type": "sff"}), SFF8636Transceiver)
    assert isinstance(make_transceiver(0, {}), CMISTransceiver)
    assert isinstance(make_transceiver(0, {"type": "cmis"}), CMISTransceiver)


@pytest.mark.asyncio
async def test_identifier_is_qsfp28(sff):
    # byte 0 = 0x11 is the switch that routes SONiC to the SFF-8636 api/SffManagerTask.
    assert _rd(sff, 0, 0, 1) == bytes([0x11])


@pytest.mark.asyncio
async def test_routing_support_bytes(sff):
    # not flat memory (byte2 bit2 = 0) -> thresholds accessible
    assert (_rd(sff, 0, 2, 1)[0] >> 2) & 1 == 0
    # OPTIONS (193-196, little-endian): bit20 TX_DISABLE impl, bit19 TX_FAULT impl
    options = struct.unpack("<I", _rd(sff, 0, 193, 4))[0]
    assert options & (1 << 20), "TX_DISABLE support must be advertised"
    assert options & (1 << 19), "TX_FAULT support must be advertised"
    # 10/40G ethernet compliance must not be 40GBASE-CR4 (0x08) -> not copper
    assert _rd(sff, 0, 131, 1)[0] != 0x08


@pytest.mark.asyncio
async def test_identity_strings(sff):
    assert _rd(sff, 0, 148, 16).rstrip(b"\x00") == b"XCVR-EMU"
    assert _rd(sff, 0, 168, 16).rstrip(b"\x00") == b"EMU-QSFP28-LR4"
    assert _rd(sff, 0, 196, 16).rstrip(b"\x00") == b"SN0123456789"


@pytest.mark.asyncio
async def test_dom_values(sff):
    assert _u16(sff, 0, 22, 256, signed=True) == pytest.approx(45.0)
    assert _u16(sff, 0, 26, 10000) == pytest.approx(3.3)
    for ch in range(4):
        assert _u16(sff, 0, 34 + ch * 2, 10000) == pytest.approx(1.0)   # rx power mW
        assert _u16(sff, 0, 42 + ch * 2, 500) == pytest.approx(6.0)     # tx bias mA
        assert _u16(sff, 0, 50 + ch * 2, 10000) == pytest.approx(1.0)   # tx power mW


@pytest.mark.asyncio
async def test_dom_thresholds(sff):
    assert _u16(sff, 3, 128, 256, signed=True) == pytest.approx(75.0)   # temp high alarm
    assert _u16(sff, 3, 130, 256, signed=True) == pytest.approx(-5.0)   # temp low alarm
    assert _u16(sff, 3, 144, 10000) == pytest.approx(3.6)               # vcc high alarm
    assert _u16(sff, 3, 176, 10000) == pytest.approx(2.0)               # rx power high alarm
    assert _u16(sff, 3, 184, 500) == pytest.approx(13.0)               # tx bias high alarm
    assert _u16(sff, 3, 192, 10000) == pytest.approx(4.0)               # tx power high alarm


@pytest.mark.asyncio
async def test_tx_disable_write_persists(sff):
    # The daemon (SffManagerTask) drives TX_DISABLE at 00h:86; a write must persist
    # so it reads back its own effect. No datapath state machine is involved.
    assert _rd(sff, 0, 86, 1) == bytes([0x00])
    sff.write(WriteRequest(index=0, bank=0, page=0, offset=86, length=1, data=bytes([0x0F])))
    assert _rd(sff, 0, 86, 1) == bytes([0x0F])


@pytest.mark.asyncio
async def test_replug_resets_image(sff):
    sff.write(WriteRequest(index=0, bank=0, page=0, offset=86, length=1, data=bytes([0x0F])))
    assert _rd(sff, 0, 86, 1) == bytes([0x0F])
    await sff.plugout()
    assert not sff.present
    sff.plugin()
    assert sff.present
    assert _rd(sff, 0, 86, 1) == bytes([0x00]), "re-plug must restore a fresh image"
    assert _rd(sff, 0, 0, 1) == bytes([0x11])


@pytest.mark.asyncio
async def test_absent_reads_zero_without_force(sff):
    await sff.plugout()
    assert not sff.present
    # unforced read of an absent module returns zeros (mirrors CMISTransceiver)
    assert sff.read(ReadRequest(index=0, bank=0, page=0, offset=0, length=4, force=False)) == b"\x00\x00\x00\x00"
    # forced read still serves the image (diagnostic path)
    assert _rd(sff, 0, 0, 1, force=True) == bytes([0x11])


@pytest.mark.asyncio
async def test_getinfo_shim_fields(sff):
    # server.GetInfo reads xcvr._state.name (uncaught) and iterates xcvr._dpsms.
    assert isinstance(sff._state, SFFModuleState)
    assert sff._state.name == "READY"
    assert sff._dpsms == {}
    await sff.plugout()
    assert sff._state.name == "NOT_PRESENT"


def test_absent_config_not_present():
    xcvr = SFF8636Transceiver(7, {"type": "sff8636", "present": False})
    assert not xcvr.present
    assert xcvr._state == SFFModuleState.NOT_PRESENT
    # image is still built (forced/diagnostic reads work)
    assert xcvr.read(ReadRequest(index=7, bank=0, page=0, offset=0, length=1, force=True)) == bytes([0x11])
