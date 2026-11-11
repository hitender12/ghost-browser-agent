"""Cancellation token: a job can be cancelled between steps."""

from __future__ import annotations

import threading


class CancelToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()
