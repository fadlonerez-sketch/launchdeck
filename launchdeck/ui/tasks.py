"""Background task runner for organizer work (scan, hash, apply, undo)."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)


class TaskThread(QThread):
    """Runs ``fn(progress, cancelled)`` off the GUI thread.

    ``progress(done, total, text)`` is throttled to keep the event queue small. Cancellation is
    cooperative: functions check ``cancelled()`` between files, never in the middle of a move.
    """

    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, name: str, fn, parent: QObject | None = None):
        super().__init__(parent)
        self.setObjectName(name)
        self._fn = fn
        self._cancel = threading.Event()
        self._last = (-1, -1)
        self.completed = False  # set just before the result is emitted; the thread is then ending

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _progress(self, done: int, total: int, text: str) -> None:
        if (done, total) != self._last:
            self._last = (done, total)
            self.progress.emit(done, total, text)

    def run(self) -> None:
        try:
            result = self._fn(self._progress, self.cancelled)
        except Exception as exc:
            log.exception("Background task %s failed", self.objectName())
            self.completed = True
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.completed = True
            self.succeeded.emit(result)


class FileOperationGate(QObject):
    """Allows only one filesystem-changing or scanning organizer task at a time, app-wide."""

    changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._owner: str | None = None
        self._thread: TaskThread | None = None

    @property
    def busy(self) -> bool:
        return self._owner is not None

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def mutating(self) -> bool:
        return self._owner in ("apply", "undo")

    def start(self, owner: str, thread: TaskThread) -> bool:
        if self._thread is not None and self._thread.completed:
            # A follow-up task started from the previous task's result slot: that thread has
            # delivered its result and is only finishing, so wait for it and hand over.
            self._thread.wait(5000)
            self._release(self._thread)
        if self._owner is not None:
            return False
        self._owner = owner
        self._thread = thread
        thread.finished.connect(lambda t=thread: self._release(t))
        self.changed.emit(True)
        thread.start()
        return True

    def _release(self, thread: TaskThread) -> None:
        if thread is not self._thread:
            return  # already released
        self._thread = None
        self._owner = None
        thread.deleteLater()
        self.changed.emit(False)

    def cancel(self) -> None:
        if self._thread is not None:
            self._thread.cancel()

    def wait(self, timeout_ms: int | None = None) -> bool:
        if self._thread is None:
            return True
        return self._thread.wait() if timeout_ms is None else self._thread.wait(timeout_ms)
