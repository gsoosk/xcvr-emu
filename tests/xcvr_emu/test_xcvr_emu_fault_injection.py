"""Unit tests for emulator fault injection (reserved control page 0xFE).

These faults let the black-box xcvrd tests exercise error/retry paths (EEPROM
read-retry, CMIS FAILED, DOM gating) without a proto change.
"""
import asyncio
import logging

import pytest
import pytest_asyncio
import importlib.resources
import yaml

from cmis import MemMap
from xcvr_emu.proto.emulator_pb2 import ReadRequest, WriteRequest
from xcvr_emu.transceiver import CMISTransceiver
from xcvr_emu.transceiver.transceiver import FAULT_PAGE, FAULT_READ, FAULT_DP_STALL

logger = logging.getLogger(__name__)


@pytest_asyncio.fixture
async def xcvr():
    with importlib.resources.open_text("xcvr_emu", "config.yaml") as f:
        config = yaml.safe_load(f)["transceivers"][0]
    xcvr = CMISTransceiver(0, config)
    yield xcvr
    await xcvr.plugout()


def _set_fault(xcvr, bits):
    xcvr.write(WriteRequest(index=0, bank=0, page=FAULT_PAGE, offset=0,
                            length=1, data=bytes([bits])))


def _read(xcvr, page, offset, length, force=False):
    return xcvr.read(ReadRequest(index=0, bank=0, page=page, offset=offset,
                                 length=length, force=force))


@pytest.mark.asyncio
async def test_fault_bitmap_write_readback(xcvr):
    assert _read(xcvr, FAULT_PAGE, 0, 1) == bytes([0x00])
    _set_fault(xcvr, FAULT_READ | FAULT_DP_STALL)
    assert _read(xcvr, FAULT_PAGE, 0, 1) == bytes([FAULT_READ | FAULT_DP_STALL])
    # the fault bitmap is NOT stored in the EEPROM (page 0xFE is reserved)
    assert 0xFE not in {k[1] for k in xcvr.mem_map.remote.higher_pages}
    _set_fault(xcvr, 0)
    assert _read(xcvr, FAULT_PAGE, 0, 1) == bytes([0x00])


@pytest.mark.asyncio
async def test_read_fault_fails_identity_page(xcvr):
    # baseline: identity byte 0 readable
    assert _read(xcvr, 0, 0, 1) == bytes([0x18])
    _set_fault(xcvr, FAULT_READ)
    # non-forced page-0 read now fails (as if the EEPROM were unreadable)
    with pytest.raises(RuntimeError):
        _read(xcvr, 0, 0, 1)
    # a forced (diagnostic) read still works
    assert _read(xcvr, 0, 0, 1, force=True) == bytes([0x18])
    # upper pages are unaffected by the identity-read fault
    _read(xcvr, 0x10, 128, 1)
    _set_fault(xcvr, 0)
    assert _read(xcvr, 0, 0, 1) == bytes([0x18])


@pytest.mark.asyncio
async def test_dp_stall_keeps_module_not_ready(xcvr):
    m = MemMap(remote=_Accessor(xcvr))
    # arm the datapath stall, then clear low power (would normally -> ModuleReady)
    _set_fault(xcvr, FAULT_DP_STALL)
    m.LowPwrRequestSW.value = m.LowPwrRequestSW.NO_REQUEST
    await asyncio.sleep(0.1)
    assert m.ModuleState.value == m.ModuleState.MODULE_LOW_PWR  # stalled, not ready

    # clearing the fault + toggling low power lets it reach ModuleReady
    _set_fault(xcvr, 0)
    m.LowPwrRequestSW.value = m.LowPwrRequestSW.LOW_POWER_MODE
    await asyncio.sleep(0.1)
    m.LowPwrRequestSW.value = m.LowPwrRequestSW.NO_REQUEST
    await asyncio.sleep(0.1)
    assert m.ModuleState.value == m.ModuleState.MODULE_READY


class _Accessor:
    def __init__(self, xcvr):
        self.xcvr = xcvr

    def read(self, bank, page, offset, length):
        return self.xcvr.read(ReadRequest(index=0, bank=bank, offset=offset,
                                          page=page, length=length, force=True))

    def write(self, bank, page, offset, length, data):
        self.xcvr.write(WriteRequest(index=0, bank=bank, offset=offset, page=page,
                                     length=length, data=data))
