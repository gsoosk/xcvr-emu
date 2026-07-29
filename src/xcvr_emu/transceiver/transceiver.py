import asyncio
import logging

from cmis import (
    Address,
    LowPwrRequestSW,
    MemMap,
    ModuleState,
    DPStateHostLane,
    BanksSupportedEnum,
    LanesEnum,
)

from ..dpsm import DataPathStateMachine
from ..proto.emulator_pb2 import ReadRequest, WriteRequest

logger = logging.getLogger(__name__)


# --- Fault injection --------------------------------------------------------
# A reserved control page the harness writes to inject module faults, so the
# black-box tests can exercise xcvrd's error/retry paths without a proto change.
# Writes to (FAULT_PAGE, 0) set the fault bitmap; it is NOT stored in the EEPROM
# and never surfaces to the CMIS decode. Faults persist across plug/unplug until
# explicitly cleared (write 0), so a test can arm a fault then insert the module.
FAULT_PAGE = 0xFE
FAULT_READ = 0x01       # identity-page (page 0) reads fail -> xcvrd EEPROM read-retry
FAULT_DP_STALL = 0x02   # module never reaches ModuleReady -> xcvrd CMIS retry -> FAILED


class CMISTransceiver:
    def __init__(self, index: int, config: dict, mem_map: MemMap | None = None):
        super().__init__()
        self._index = index
        self._config = config
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._present = False
        # Injected-fault bitmap. Set only here (not in _init) so an armed fault
        # survives the _init_eeprom that runs on every plugin().
        self._faults = 0

        self.mem_map = MemMap() if mem_map is None else mem_map

        self._dpsms: dict[tuple[int, int], DataPathStateMachine] = {}

        if config.get("present"):
            self.plugin()
        else:
            self._init()

    def _iterate_banks(self, f, *args):
        num_banks = 1
        original_bank = self.mem_map.bank
        match self.mem_map.BanksSupported.value:
            case BanksSupportedEnum.BANKS_0_1_SUPPORTED:
                num_banks = 2
            case BanksSupportedEnum.BANKS_0_3_SUPPORTED:
                num_banks = 4

        try:
            for bank in range(num_banks):
                self.mem_map.bank = bank
                f(*args)
        finally:
            self.mem_map.bank = original_bank

    def _init(self):
        self._state = ModuleState.MODULE_LOW_PWR
        self._init_eeprom()

    def _init_dpsms(self):
        dpsms = {}

        def __init_dpsms(dpsms):
            bank = self.mem_map.bank
            for i, acs in enumerate(self.mem_map.ACS_DPConfigLane):
                appsel = acs.AppSelCode.value
                # CMIS v5.2 6.2.3.2
                # The special AppSel code value 0000b in the Data Path Configuration register of a host lane indicates that the
                # lane (together with its associated resources) is unused and not part of a Data Path. The DataPathID and
                # ExplicitControl fields of unused host lanes are irrelevant and may be ignored by the module.
                if appsel == 0:
                    continue

                dpid = acs.DataPathID.value
                if (bank, dpid) not in dpsms:
                    dpsms[(bank, dpid)] = DataPathStateMachine(self.mem_map, bank, dpid)

                explicit_control = acs.ExplicitControl.value
                dpsm = dpsms[(bank, dpid)]
                dpsm.add_lane(i, appsel, explicit_control)

            for (b, _), dpsm in dpsms.items():
                if b != bank:
                    continue

                if not dpsm.update_state():
                    logger.warning(f"DPSM invalid config: {dpsm}")

        self._iterate_banks(__init_dpsms, dpsms)
        self._dpsms = dpsms

    def _apply_dpinit(self):
        for i, scs in enumerate(self.mem_map.SCS0_DPConfigLane):
            value = scs.value
            logger.info(
                f"Applying DPInit({i}): {value:b} AppSelCode: {scs.AppSelCode.value}"
            )
            self.mem_map.ACS_DPConfigLane[i].value = value
            if scs.AppSelCode.value == 0:
                continue

            # TODO validate the config and set appropriate status
            self.mem_map.DPInitPendingLane[
                i
            ].value = self.mem_map.DPInitPendingLane.PENDING
            self.mem_map.ConfigStatusLane[
                i
            ].value = self.mem_map.ConfigStatusLane.SUCCESS

        return True

    def _init_eeprom(self):
        def set_value(field, value):
            if type(value) is list:
                for i, val in enumerate(value):
                    set_value(field[i], val)
            elif type(value) is dict:
                for k, val in value.items():
                    set_value(getattr(field, k), val)
            else:
                field.set_value_from_str(str(value))

        for k, v in self._config.get("defaults", {}).items():
            try:
                field = getattr(self.mem_map, k)
            except AttributeError:
                logger.error(f"Invalid attribute: {k}")
                continue

            set_value(field, v)

        # use ApplicationDescriptor1 for the default application
        default_app = self.mem_map.ApplicationDescriptor[0]

        count = 0
        match default_app.HostLaneCount.value:
            case LanesEnum.ONE_LANE:
                count = 1
            case LanesEnum.TWO_LANES:
                count = 2
            case LanesEnum.FOUR_LANES:
                count = 4
            case LanesEnum.EIGHT_LANES:
                count = 8

        assign = default_app.HostLaneAssignmentOptions.value

        # default staged control set 0, data path configuration
        # and default active control set
        def provision_acs_scs():
            dpid = 0
            current_count = 0
            for i, acs in enumerate(self.mem_map.ACS_DPConfigLane):
                if (assign >> i) & 0x1 == 1:
                    dpid += 1
                    current_count = 0
                if current_count < count:
                    acs.AppSelCode.value = 1
                    acs.DataPathID.value = dpid
                    current_count += 1
                else:
                    acs.AppSelCode.value = 0
                    acs.DataPathID.value = 0

            for i, scs in enumerate(self.mem_map.SCS0_DPConfigLane):
                scs.value = self.mem_map.ACS_DPConfigLane[i].value

            for v in self.mem_map.DPStateHostLane:
                v.value = DPStateHostLane.DPDEACTIVATED

        self._iterate_banks(provision_acs_scs)

        self.mem_map.ModuleState.value = ModuleState.MODULE_LOW_PWR
        self.mem_map.LowPwrRequestSW.value = LowPwrRequestSW.LOW_POWER_MODE

    @property
    def present(self) -> bool:
        return self._present

    def _page_bank_emulation(self, req: ReadRequest | WriteRequest) -> None:
        if req.offset >= 128:  # upper page
            self.mem_map.PageSelect.value = req.page
            if req.page >= 0x10:  # banked page
                max_bank = 1
                match self.mem_map.BanksSupported.value:
                    case BanksSupportedEnum.BANKS_0_1_SUPPORTED:
                        max_bank = 2
                    case BanksSupportedEnum.BANKS_0_3_SUPPORTED:
                        max_bank = 4

                if req.bank < max_bank:
                    self.mem_map.BankSelect.value = req.bank

    def read(self, req: ReadRequest) -> bytes:
        # Fault-control page: read back the current fault bitmap (never EEPROM).
        if req.page == FAULT_PAGE:
            return bytes([self._faults]) + b"\x00" * max(0, req.length - 1)
        # Injected read fault: identity-page (page 0) reads fail as if the EEPROM
        # were unreadable, so xcvrd's insertion identity read fails and it enters
        # its retry-eeprom loop. force reads (diagnostic) bypass the fault.
        if (self._faults & FAULT_READ) and req.page == 0 and not req.force:
            raise RuntimeError(
                f"Transceiver({self._index}) injected read fault on page 0")
        if not req.force and not self.present:
            return b"\x00" * req.length
        self._page_bank_emulation(req)
        return self.mem_map.read(req.bank, req.page, req.offset, req.length)

    def write(self, req: WriteRequest) -> None:
        # Fault-control page: set the fault bitmap; do not store in EEPROM/queue.
        if req.page == FAULT_PAGE:
            self._faults = req.data[0] if req.data else 0
            logger.info(
                f"Transceiver({self._index}) fault bitmap set to {self._faults:#04x}")
            return
        self._page_bank_emulation(req)
        self.mem_map.write(req.bank, req.page, req.offset, req.length, req.data)
        if req.length == 1:
            address = Address(req.page, req.offset)
        else:
            address = Address(req.page, (req.offset, req.offset + req.length - 1))
        self._queue.put_nowait((req, address))

    async def plugout(self) -> None:
        self._present = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                self._task = None
            logger.debug(f"Transceiver({self._index}) task cancelled")

    def plugin(self) -> None:
        if self._task:
            logger.warning(f"Transceiver({self._index}) already running")
            return

        self._init()
        self._present = True
        self._task = asyncio.create_task(self._run())
        control = self.mem_map.ModuleGlobalControls
        self.write(
            WriteRequest(
                page=control.address.page,
                offset=control.address.offset,
                length=control.address.byte_size,
                data=bytes([control.value_as_int]),
            )
        )

    async def _run(self) -> None:
        logger.info(f"Transceiver({self._index}) started")

        while True:
            ev: tuple[WriteRequest, Address] = await self._queue.get()
            address: Address = ev[1]
            bank: int = ev[0].bank

            self.mem_map.bank = bank

            logger.debug(f"Handling address: {address}, bank: {bank}")

            if address.includes(self.mem_map.ModuleGlobalControls.address):
                prev_state = self._state
                software_reset = self.mem_map.SoftwareReset
                if software_reset.value == software_reset.RESET:
                    logger.info("Software reset")
                    self._init()
                    # SoftwareReset (00h:26.3) is Write-Only / Self-Clearing per
                    # the CMIS spec: once the module has acted on the trigger the
                    # bit must read back 0. Clear it here so a subsequent
                    # read-modify-write of ModuleGlobalControls (e.g. the host
                    # clearing LowPwrRequestSW) does not re-trigger a reset.
                    self.mem_map.SoftwareReset.value = (
                        self.mem_map.SoftwareReset.NO_ACTION
                    )

                low_pwr = self.mem_map.LowPwrRequestSW
                if low_pwr.value == low_pwr.LOW_POWER_MODE:
                    state = ModuleState.MODULE_LOW_PWR
                elif self._faults & FAULT_DP_STALL:
                    # Injected datapath stall: the module never leaves low power,
                    # so it never reaches ModuleReady. xcvrd's CmisManagerTask
                    # keeps timing out waiting for ModuleReady and retries, and
                    # after CMIS_MAX_RETRIES drives cmis_state to FAILED. While it
                    # retries the state stays non-terminal (DOM is gated).
                    state = ModuleState.MODULE_LOW_PWR
                else:
                    state = ModuleState.MODULE_READY
                    self._init_dpsms()

                if state != prev_state:
                    logger.info(f"Updating module state: {prev_state} -> {state}")
                    self.mem_map.ModuleState.value = state
                    self._state = state

            match self._state:
                case ModuleState.MODULE_LOW_PWR:
                    pass
                case ModuleState.MODULE_READY:
                    logger.info(f"ready: {address}, bank: {bank}")
                    dp_state_fields = [
                        self.mem_map.DPDeinitLane,
                        self.mem_map.OutputDisableTx,
                    ]
                    if any(address == f.address for f in dp_state_fields):
                        for (b, _), dpsm in self._dpsms.items():
                            if b != bank:
                                continue

                            if not dpsm.update_state():
                                logger.warning(f"DPSM invalid config: {dpsm}")
                    elif address.includes(
                        self.mem_map.SCS0_ApplyTriggers.ApplyDPInitLane.address
                    ):
                        if self._apply_dpinit():
                            self._init_dpsms()
