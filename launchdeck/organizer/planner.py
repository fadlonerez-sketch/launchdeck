"""Organization plans: a reviewable, editable list of proposed operations.

Building and editing a plan never touches the filesystem except to *read* which names exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable

from launchdeck.organizer.categories import Classifier
from launchdeck.organizer.naming import suggest_filename, unique_name, validate_filename
from launchdeck.organizer.safety import SafetyError, check_source, Fingerprint, is_within, norm
from launchdeck.organizer.scanner import DuplicateReport, FileInfo

KEEP_IN_PLACE = "__keep__"

ACTION_MOVE = "move"
ACTION_RENAME = "rename"
ACTION_REVIEW = "review"
ACTION_NONE = "none"

STATUS_PLANNED = "planned"
STATUS_NO_CHANGE = "no change"
STATUS_INVALID = "invalid"
STATUS_BLOCKED = "blocked"  # failed revalidation
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_UNDONE = "undone"


@dataclass
class PlanConfig:
    source_root: str
    dest_root: str
    review_root: str
    classifier: Classifier

    def output_roots(self) -> list[str]:
        return [self.dest_root, self.review_root]


def validate_config(config: PlanConfig) -> list[str]:
    problems = []
    src = config.source_root
    if not src or not os.path.isabs(src):
        problems.append("Choose a source folder.")
    for label, root in (("destination", config.dest_root), ("duplicate review", config.review_root)):
        if not root or not os.path.isabs(root):
            problems.append(f"Choose a {label} folder.")
            continue
        if norm(root) == norm(src):
            problems.append(f"The {label} folder must be different from the source folder.")
        elif is_within(src, root):
            problems.append(f"The {label} folder cannot contain the source folder.")
    return problems


@dataclass
class PlanItem:
    id: int
    file: FileInfo
    category: str  # category id, or KEEP_IN_PLACE
    new_name: str  # name the user wants (suggestion, edit, or original)
    suggested_name: str | None = None
    review: bool = False  # duplicate copy marked for the review folder
    selected: bool = True
    dup_group: int | None = None
    dest_dir: str = ""
    dest_name: str = ""
    adjusted: bool = False  # dest_name differs from new_name to avoid a conflict
    status: str = STATUS_PLANNED
    message: str = ""
    result_path: str = ""

    @property
    def action(self) -> str:
        if self.review:
            return ACTION_REVIEW
        if self.category == KEEP_IN_PLACE:
            return ACTION_RENAME if self.new_name != self.file.name else ACTION_NONE
        return ACTION_MOVE

    @property
    def dest_path(self) -> str:
        return os.path.join(self.dest_dir, self.dest_name) if self.dest_dir else ""

    @property
    def renamed(self) -> bool:
        return bool(self.dest_name) and self.dest_name != self.file.name

    @property
    def actionable(self) -> bool:
        return self.status in (STATUS_PLANNED,) and self.action != ACTION_NONE

    @property
    def fingerprint(self) -> Fingerprint:
        return Fingerprint(self.file.size, self.file.mtime_ns)


@dataclass
class Plan:
    config: PlanConfig
    items: list[PlanItem] = field(default_factory=list)
    duplicates: DuplicateReport = field(default_factory=DuplicateReport)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    # -------------------------------------------------------------- selection helpers
    def selected(self) -> list[PlanItem]:
        return [i for i in self.items if i.selected and i.actionable]

    def select_all(self, value: bool = True) -> None:
        for item in self.items:
            item.selected = value and item.actionable

    def select_category(self, category: str, value: bool = True) -> None:
        for item in self.items:
            if item.category == category and not item.review:
                item.selected = value and item.actionable

    def summary(self) -> dict[str, int]:
        sel = self.selected()
        return {
            "files": len(self.items),
            "selected": len(sel),
            "moves": sum(1 for i in sel if i.action == ACTION_MOVE),
            "renames": sum(1 for i in sel if i.renamed),
            "rename_only": sum(1 for i in sel if i.action == ACTION_RENAME),
            "review": sum(1 for i in sel if i.action == ACTION_REVIEW),
            "duplicate_groups": len(self.duplicates.groups),
            "duplicate_files": sum(len(g.members) for g in self.duplicates.groups),
            "skipped": len(self.skipped),
            "invalid": sum(1 for i in self.items if i.status in (STATUS_INVALID, STATUS_BLOCKED)),
        }

    def mark_extra_copies_for_review(self) -> int:
        """Keep the oldest copy of each verified duplicate group; mark the others for review."""
        by_path = {norm(i.file.path): i for i in self.items}
        marked = 0
        for group in self.duplicates.groups:
            for member in group.members[1:]:  # members are sorted oldest first
                item = by_path.get(norm(member.path))
                if item and not item.review:
                    item.review = True
                    item.selected = True
                    marked += 1
        self.recompute()
        return marked

    def clear_review_marks(self) -> None:
        for item in self.items:
            item.review = False
        self.recompute()

    # -------------------------------------------------------------- destinations
    def recompute(self, exists: Callable[[str], bool] = os.path.lexists) -> None:
        compute_destinations(self.items, self.config, exists)


def compute_destinations(items: Iterable[PlanItem], config: PlanConfig,
                         exists: Callable[[str], bool] = os.path.lexists) -> None:
    """Fill in dest_dir/dest_name so no two selected items, and no existing file, collide."""
    reserved: set[str] = set()
    for item in items:
        if item.status in (STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED, STATUS_UNDONE):
            continue
        item.adjusted = False
        item.message = "" if item.status != STATUS_BLOCKED else item.message
        if item.status != STATUS_BLOCKED:
            item.status = STATUS_PLANNED
        problem = validate_filename(item.new_name)
        if problem:
            item.status = STATUS_INVALID
            item.message = problem
            item.dest_dir, item.dest_name = "", ""
            continue
        if item.review:
            item.dest_dir = config.review_root
        elif item.category == KEEP_IN_PLACE:
            item.dest_dir = item.file.folder
        else:
            item.dest_dir = os.path.join(config.dest_root, config.classifier.folder_for(item.category))
        if item.action == ACTION_NONE:
            item.dest_name = item.file.name
            item.status = STATUS_NO_CHANGE
            item.selected = False
            continue
        own = norm(item.file.path)

        def occupied(path: str, _own=own) -> bool:
            return norm(path) != _own and exists(path)

        name = unique_name(item.dest_dir, item.new_name, reserved, occupied)
        item.dest_name = name
        if name != item.new_name:
            item.adjusted = True
            item.message = f"'{item.new_name}' already exists there, so it will be saved as '{name}'."
        if item.selected and item.status == STATUS_PLANNED:
            reserved.add(norm(item.dest_path))


def build_plan(files: list[FileInfo], duplicates: DuplicateReport, config: PlanConfig,
               suggest_names: bool = True, skipped: list[tuple[str, str]] | None = None,
               exists: Callable[[str], bool] = os.path.lexists) -> Plan:
    groups = duplicates.group_of()
    items = []
    for index, f in enumerate(sorted(files, key=lambda x: (x.name.casefold(), x.path.casefold()))):
        suggestion = suggest_filename(f.name) if suggest_names else None
        items.append(
            PlanItem(
                id=index,
                file=f,
                category=f.category,
                new_name=suggestion or f.name,
                suggested_name=suggestion,
                dup_group=groups.get(norm(f.path)),
            )
        )
    plan = Plan(config, items, duplicates, list(skipped or []))
    plan.recompute(exists)
    return plan


def revalidate(plan: Plan) -> list[PlanItem]:
    """Re-check every selected item against the filesystem right before confirmation.

    Items whose source changed or disappeared become BLOCKED (and deselected); destinations
    are recomputed so that newly appeared files get unique names. Returns the items that changed.
    """
    changed = []
    for item in plan.selected():
        try:
            check_source(item.file.path, item.fingerprint, plan.config.source_root, plan.config.output_roots())
        except SafetyError as exc:
            item.status = STATUS_BLOCKED
            item.message = str(exc)
            item.selected = False
            changed.append(item)
    before = {i.id: i.dest_path for i in plan.items}
    plan.recompute()
    for item in plan.items:
        if item.selected and before.get(item.id) != item.dest_path and item not in changed:
            changed.append(item)
    return changed
