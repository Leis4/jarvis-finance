from __future__ import annotations

from jarvis.invest.brokers.base import PaperBrokerAdapter
from jarvis.invest.brokers.sber_manual import SberManualAdapter
from jarvis.invest.brokers.sber_semi_auto import SberSemiAutoAdapter
from jarvis.invest.brokers.tbank import TBankMCPAdapter
from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.exceptions import UnsupportedModeError
from jarvis.invest.core.interfaces import BrokerAdapter


def get_broker_adapter(mode: str | None = None) -> BrokerAdapter:
    selected = mode or get_settings().broker_mode
    if selected == "paper":
        return PaperBrokerAdapter()
    if selected in {"tbank", "tbank_sandbox", "tbank_live", "tbank_mcp"}:
        return TBankMCPAdapter()
    if selected == "sber_manual":
        return SberManualAdapter()
    if selected == "sber_semi_auto":
        return SberSemiAutoAdapter()
    raise UnsupportedModeError(f"Unsupported broker mode: {selected}")
