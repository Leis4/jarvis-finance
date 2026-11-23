"""Inbox package for folder watching and auto-ingestion."""

from jarvis.finance.inbox.watcher import SberInboxHandler, start_watcher

__all__ = ["SberInboxHandler", "start_watcher"]
