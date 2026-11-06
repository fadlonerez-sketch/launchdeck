"""Combines the file index, the small library index and dynamic results into one ranked list."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Signal

from launchdeck.commands.model import CommandDef
from launchdeck.launcher.models import (
    KIND_COMMAND, KIND_ORDER, KIND_URL, KIND_WEBSEARCH, KIND_WEBSITE, Item, Result,
)
from launchdeck.launcher.ranking import SearchIndex
from launchdeck.launcher.urls import build_search_url, display_domain, parse_explicit_url
from launchdeck.storage.data_store import Website

log = logging.getLogger(__name__)


def library_items(websites: list[Website], commands: list[CommandDef], actions: list[Item]) -> list[Item]:
    items = list(actions)
    for site in websites:
        items.append(Item(KIND_WEBSITE, site.name, site.url, display_domain(site.url), tuple(site.keywords)))
    for cmd in commands:
        if cmd.enabled:  # disabled commands are not offered at all
            detail = cmd.description or f"Alias: {', '.join(cmd.aliases)}"
            items.append(Item(KIND_COMMAND, cmd.name, cmd.id, detail, tuple(cmd.aliases)))
    return items


@dataclass(frozen=True)
class SearchContext:
    files: SearchIndex
    library: SearchIndex
    commands_by_alias: dict[str, CommandDef] = field(default_factory=dict)
    search_template: str = "https://www.google.com/search?q={query}"
    engine_name: str = "Google"
    web_fallback: bool = True
    limit: int = 8


def _sort_key(r: Result):
    return (-r.score, KIND_ORDER.get(r.item.kind, 99), len(r.item.name), r.item.name.casefold(), r.item.key)


def run_search(ctx: SearchContext, text: str) -> list[Result]:
    query = text.strip()
    if not query:
        return []
    out: list[Result] = []

    url = parse_explicit_url(query)
    if url:
        out.append(Result(Item(KIND_URL, f"Open {display_domain(url)}", url, url), 5000.0, "dynamic"))

    head, _, tail = query.partition(" ")
    cmd = ctx.commands_by_alias.get(head.casefold())
    if cmd is not None and cmd.enabled and cmd.uses_query and tail.strip():
        argument = tail.strip()
        item = Item(KIND_COMMAND, f"{cmd.name}  ›  {argument}", cmd.id,
                    cmd.description or f"Run {cmd.name} with this text", key=f"command:{cmd.id}")
        out.append(Result(item, 4000.0, "dynamic", argument=argument))

    ranked = ctx.library.search(query, ctx.limit) + ctx.files.search(query, ctx.limit)
    ranked.sort(key=_sort_key)
    seen = {r.item.key for r in out}
    for r in ranked:
        if r.item.key not in seen and len(out) < ctx.limit:
            seen.add(r.item.key)
            out.append(r)

    if ctx.web_fallback:
        try:
            search_url = build_search_url(ctx.search_template, query)
            label = f"Search {ctx.engine_name} for “{query}”"
            out.append(Result(Item(KIND_WEBSEARCH, label, search_url, display_domain(search_url)), -1.0, "dynamic"))
        except ValueError as exc:
            log.warning("Web search template unusable: %s", exc)
    return out


class SearchWorker(QThread):
    """Runs searches off the GUI thread; only the newest pending request is processed."""

    results_ready = Signal(int, str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchWorker")
        self._cond = threading.Condition()
        self._pending: tuple[int, str] | None = None
        self._ctx: SearchContext | None = None
        self._stopping = False

    def set_context(self, ctx: SearchContext) -> None:
        with self._cond:
            self._ctx = ctx

    def submit(self, request_id: int, text: str) -> None:
        with self._cond:
            self._pending = (request_id, text)
            self._cond.notify()

    def stop(self, timeout_ms: int = 3000) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()
        self.wait(timeout_ms)

    def run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                request_id, text = self._pending
                self._pending = None
                ctx = self._ctx
            if ctx is None:
                results: list[Result] = []
            else:
                try:
                    results = run_search(ctx, text)
                except Exception:
                    log.exception("Search failed for a query of length %d", len(text))
                    results = []
            with self._cond:
                stale = self._pending is not None
            if not stale:
                self.results_ready.emit(request_id, text, results)
