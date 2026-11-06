"""Applies reviewed plan operations and undoes recorded ones.

Each operation is written to the history as *pending* before it runs and updated right after,
so the log is accurate even if LaunchDeck is closed or crashes mid-batch. Operations are
independent: one failure is recorded and the batch continues. Batches are not atomic.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from launchdeck.organizer.naming import unique_name
from launchdeck.organizer.planner import ACTION_MOVE, ACTION_RENAME, ACTION_REVIEW, Plan
from launchdeck.organizer.safety import (
    Fingerprint, SafetyError, check_destination, check_inside, check_source, ensure_directory,
    is_reparse_point, move_no_overwrite,
)
from launchdeck.storage.data_store import (
    OP_MOVE, OP_RENAME, OP_REVIEW, OP_UNDO, ST_CANCELLED, ST_FAILED, ST_SKIPPED, ST_SUCCESS,
    HistoryStore, OperationRecord,
)

log = logging.getLogger(__name__)

Progress = Callable[[int, int, str], None]
Cancelled = Callable[[], bool]

_ACTION_TO_OP = {ACTION_MOVE: OP_MOVE, ACTION_RENAME: OP_RENAME, ACTION_REVIEW: OP_REVIEW}


def describe_os_error(exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror in (32, 33):
        return "The file is open in another program. Close it and try again."
    if isinstance(exc, FileExistsError) or winerror in (80, 183):
        return "A file with that name already exists; nothing was overwritten."
    if isinstance(exc, PermissionError) or winerror == 5:
        return "Access denied."
    if isinstance(exc, FileNotFoundError) or winerror in (2, 3):
        return "The file or folder no longer exists."
    if winerror in (21, 53, 67, 1231):
        return "The drive or network location is not available."
    if winerror == 206 or getattr(exc, "errno", None) == 36:
        return "The path is too long."
    if winerror == 112:
        return "There is not enough space on the disk."
    return exc.strerror or str(exc)


@dataclass(frozen=True)
class ApplyOp:
    item_id: int
    op_type: str
    src: str
    dst: str
    fingerprint: Fingerprint
    src_scope: str
    dst_scope: str
    excluded_roots: tuple[str, ...] = ()


@dataclass
class OpOutcome:
    item_id: int
    status: str
    message: str
    final_path: str = ""


@dataclass
class BatchOutcome:
    batch_id: int | None
    outcomes: list[OpOutcome] = field(default_factory=list)
    error: str = ""

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


def ops_from_plan(plan: Plan) -> list[ApplyOp]:
    cfg = plan.config
    ops = []
    for item in plan.selected():
        action = item.action
        dst_scope = {ACTION_MOVE: cfg.dest_root, ACTION_REVIEW: cfg.review_root,
                     ACTION_RENAME: cfg.source_root}[action]
        # Renames stay in the source tree, but must never land inside an output folder.
        ops.append(ApplyOp(item.id, _ACTION_TO_OP[action], item.file.path, item.dest_path, item.fingerprint,
                           cfg.source_root, dst_scope, tuple(cfg.output_roots())))
    return ops


def _run_one(op: ApplyOp) -> tuple[str, str]:
    try:
        check_source(op.src, op.fingerprint, op.src_scope, list(op.excluded_roots))
        check_destination(op.dst, op.dst_scope, op.src)
        if op.op_type == OP_RENAME and os.path.dirname(os.path.abspath(op.dst)) != os.path.dirname(os.path.abspath(op.src)):
            raise SafetyError("A rename must stay in the same folder.")
        ensure_directory(os.path.dirname(op.dst), op.dst_scope)
        move_no_overwrite(op.src, op.dst)
    except SafetyError as exc:
        return ST_SKIPPED, str(exc)
    except OSError as exc:
        return ST_FAILED, describe_os_error(exc)
    return ST_SUCCESS, ""


def apply_operations(ops: list[ApplyOp], history: HistoryStore, source_root: str, dest_root: str,
                     cancelled: Cancelled = lambda: False, progress: Progress | None = None) -> BatchOutcome:
    if not ops:
        return BatchOutcome(None)
    records = [
        {"op_type": op.op_type, "src": op.src, "dst": op.dst, "size": op.fingerprint.size,
         "mtime_ns": op.fingerprint.mtime_ns, "src_scope": op.src_scope, "dst_scope": op.dst_scope}
        for op in ops
    ]
    try:
        batch_id, op_ids = history.start_batch("organize", source_root, dest_root, records)
    except Exception as exc:
        log.exception("Could not start history batch")
        return BatchOutcome(None, error=f"Nothing was changed: the activity history could not be written ({exc}).")
    result = BatchOutcome(batch_id)
    status = "completed"
    for index, (op, op_id) in enumerate(zip(ops, op_ids)):
        if cancelled():
            status = "cancelled"
            break
        if progress:
            progress(index, len(ops), os.path.basename(op.src))
        op_status, message = _run_one(op)
        result.outcomes.append(OpOutcome(op.item_id, op_status, message, op.dst if op_status == ST_SUCCESS else ""))
        try:
            history.finish_operation(op_id, op_status, message)
        except Exception as exc:
            log.critical("History update failed after operation %s: %s", op_id, exc)
            result.error = ("The activity history could not be updated, so the remaining operations were not run. "
                            f"The last file was {'moved' if op_status == ST_SUCCESS else 'not moved'}.")
            status = "interrupted"
            break
    try:
        history.finish_batch(batch_id, status)
    except Exception as exc:
        log.error("Could not close history batch %s: %s", batch_id, exc)
    done_ids = {o.item_id for o in result.outcomes}
    for op in ops:
        if op.item_id not in done_ids:
            result.outcomes.append(OpOutcome(op.item_id, ST_CANCELLED, "Not run (stopped before this file)."))
    if progress:
        progress(len(ops), len(ops), "")
    return result


# ------------------------------------------------------------------------------------------ undo
UNDO_READY = "ready"
UNDO_CONFLICT = "conflict"  # the original location is occupied
UNDO_MODIFIED = "modified"  # file changed since it was moved
UNDO_MISSING = "missing"  # file is no longer where LaunchDeck put it
UNDO_UNSAFE = "unsafe"
UNDO_NOT_UNDOABLE = "not undoable"


@dataclass
class UndoCheck:
    op: OperationRecord
    state: str
    message: str
    current: Fingerprint | None = None
    alternative: str = ""  # free name in the original folder when the original is occupied

    @property
    def can_restore(self) -> bool:
        return self.state in (UNDO_READY, UNDO_CONFLICT, UNDO_MODIFIED)


def check_undo(op: OperationRecord) -> UndoCheck:
    if not op.undoable:
        reason = "Already undone." if op.undone else "Only completed moves and renames can be undone."
        return UndoCheck(op, UNDO_NOT_UNDOABLE, reason)
    if not op.src_scope or not op.dst_scope:
        return UndoCheck(op, UNDO_UNSAFE, "This record does not contain enough information to undo safely.")
    if not os.path.lexists(op.dst):
        if os.path.lexists(op.src):
            return UndoCheck(op, UNDO_MISSING, "The file is not at its new location; it appears to be back at "
                                               "its original location already.")
        return UndoCheck(op, UNDO_MISSING, "The file is no longer at the location LaunchDeck moved it to "
                                           "(it was moved, renamed or deleted since).")
    try:
        check_inside(op.dst, op.dst_scope, "file")
        check_inside(os.path.dirname(op.src), op.src_scope, "original folder")
    except SafetyError as exc:
        return UndoCheck(op, UNDO_UNSAFE, str(exc))
    if is_reparse_point(op.dst) or not os.path.isfile(op.dst):
        return UndoCheck(op, UNDO_UNSAFE, "The item at the new location is no longer a regular file.")
    st = os.lstat(op.dst)
    current = Fingerprint(st.st_size, st.st_mtime_ns)
    original_dir = os.path.dirname(op.src)
    if not os.path.isdir(op.src_scope):
        return UndoCheck(op, UNDO_UNSAFE, f"The original folder '{op.src_scope}' is not available.", current)
    if os.path.lexists(op.src):
        alt = unique_name(original_dir, os.path.basename(op.src)) if os.path.isdir(original_dir) else ""
        return UndoCheck(op, UNDO_CONFLICT,
                         f"Another file now exists at the original location '{op.src}'. It will not be replaced.",
                         current, alt)
    if current.size != op.size or current.mtime_ns != op.mtime_ns:
        return UndoCheck(op, UNDO_MODIFIED, "The file was modified after it was moved.", current)
    return UndoCheck(op, UNDO_READY, "", current)


@dataclass(frozen=True)
class UndoRequest:
    check: UndoCheck
    target: str  # original path, or an alternative chosen by the user
    allow_modified: bool = False


def perform_undo(requests: list[UndoRequest], history: HistoryStore,
                 cancelled: Cancelled = lambda: False, progress: Progress | None = None) -> BatchOutcome:
    runnable = [r for r in requests if r.check.can_restore and r.check.current is not None]
    if not runnable:
        return BatchOutcome(None)
    records = [
        {"op_type": OP_UNDO, "src": r.check.op.dst, "dst": r.target, "size": r.check.current.size,
         "mtime_ns": r.check.current.mtime_ns, "undo_of": r.check.op.id,
         "src_scope": r.check.op.dst_scope, "dst_scope": r.check.op.src_scope}
        for r in runnable
    ]
    try:
        batch_id, op_ids = history.start_batch("undo", runnable[0].check.op.src_scope,
                                               runnable[0].check.op.dst_scope, records)
    except Exception as exc:
        return BatchOutcome(None, error=f"Nothing was changed: the activity history could not be written ({exc}).")
    result = BatchOutcome(batch_id)
    status = "completed"
    for index, (req, op_id) in enumerate(zip(runnable, op_ids)):
        if cancelled():
            status = "cancelled"
            break
        op = req.check.op
        if progress:
            progress(index, len(runnable), os.path.basename(op.dst))
        try:
            expected = req.check.current if req.allow_modified else Fingerprint(op.size, op.mtime_ns)
            if req.check.state == UNDO_MODIFIED and not req.allow_modified:
                raise SafetyError("The file was modified after it was moved; restore it explicitly to proceed.")
            check_source(op.dst, expected, op.dst_scope)
            if not req.target or os.path.dirname(os.path.abspath(req.target)) != os.path.dirname(os.path.abspath(op.src)):
                raise SafetyError("Files can only be restored into their original folder.")
            check_destination(req.target, op.src_scope)
            ensure_directory(os.path.dirname(req.target), op.src_scope)
            move_no_overwrite(op.dst, req.target)
            op_status, message = ST_SUCCESS, ("" if req.target == op.src else f"Restored as '{os.path.basename(req.target)}'.")
        except SafetyError as exc:
            op_status, message = ST_SKIPPED, str(exc)
        except OSError as exc:
            op_status, message = ST_FAILED, describe_os_error(exc)
        result.outcomes.append(OpOutcome(op.id, op_status, message, req.target if op_status == ST_SUCCESS else ""))
        try:
            history.finish_operation(op_id, op_status, message)
        except Exception as exc:
            log.critical("History update failed after undo %s: %s", op_id, exc)
            result.error = "The activity history could not be updated, so the remaining files were not restored."
            status = "interrupted"
            break
    try:
        history.finish_batch(batch_id, status)
    except Exception as exc:
        log.error("Could not close undo batch %s: %s", batch_id, exc)
    if progress:
        progress(len(runnable), len(runnable), "")
    return result
