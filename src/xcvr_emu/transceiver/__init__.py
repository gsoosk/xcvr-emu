from .transceiver import CMISTransceiver
from .sff8636 import SFF8636Transceiver


def make_transceiver(index: int, config: dict):
    """Build the transceiver class named by the config ``type`` (default cmis).

    ``type: sff8636`` -> SFF-8636 (QSFP28) module; anything else -> CMIS. Selection
    is by management interface family, mirroring how SONiC dispatches on the
    EEPROM identifier byte.
    """
    kind = str(config.get("type", "cmis")).lower()
    if kind in ("sff8636", "sff", "qsfp28", "qsfp+"):
        return SFF8636Transceiver(index, config)
    return CMISTransceiver(index, config)


__all__ = ["CMISTransceiver", "SFF8636Transceiver", "make_transceiver"]
