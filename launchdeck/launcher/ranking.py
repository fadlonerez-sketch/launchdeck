"""Deterministic local search ranking.

Tiers, strongest first: exact > prefix > whole-word > word-prefix > acronym > substring > all-tokens >
fuzzy (typo-tolerant, via a word vocabulary) > subsequence (apps/commands only).
A ``SearchIndex`` is immutable once built, so a worker thread can search it while a new one is
being built elsewhere.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import OrderedDict
from typing import Iterable, Mapping

from launchdeck.launcher.models import (
    KIND_ACTION, KIND_APP, KIND_COMMAND, KIND_FILE, KIND_FOLDER, KIND_ORDER, KIND_SHORTCUT,
    KIND_WEBSITE, Item, Result,
)

SCORE_EXACT = 1000.0
SCORE_PREFIX = 850.0
SCORE_WHOLE_WORD = 830.0  # every query token is a complete word of the name
SCORE_WORD = 700.0
SCORE_ACRONYM = 640.0
SCORE_SUBSTRING = 520.0
SCORE_TOKENS = 420.0
SCORE_FUZZY = 300.0
SCORE_SUBSEQUENCE = 180.0

KIND_BONUS = {
    KIND_ACTION: 40.0, KIND_COMMAND: 40.0, KIND_APP: 35.0, KIND_WEBSITE: 30.0,
    KIND_SHORTCUT: 25.0, KIND_FOLDER: 10.0, KIND_FILE: 0.0,
}
_SMALL_KINDS = {KIND_ACTION, KIND_COMMAND, KIND_APP, KIND_WEBSITE, KIND_SHORTCUT}
_STEM_KINDS = {KIND_FILE}

_WORD_RE = re.compile(r"[^\W_]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Case-fold, strip accents, and reduce punctuation/whitespace runs to single spaces."""
    return " ".join(_WORD_RE.findall(_strip_accents(text).casefold()))


def _split_words(text: str) -> tuple[tuple[str, ...], str]:
    """Return (words, acronym). Words include camelCase parts: 'PowerShell' -> power, shell."""
    words: list[str] = []
    initials: list[str] = []
    for chunk in _WORD_RE.findall(_strip_accents(text)):
        parts = _CAMEL_RE.findall(chunk) if chunk.isascii() else []
        if not parts or "".join(parts) != chunk:
            parts = [chunk]
        low_chunk = chunk.casefold()
        if low_chunk not in words:
            words.append(low_chunk)
        for part in parts:
            low = part.casefold()
            if low not in words:
                words.append(low)
            initials.append(low[0])
    return tuple(words), "".join(initials)


def bounded_distance(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment distance, or ``limit + 1`` once it is known to exceed ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                value = min(value, prev2[j - 2] + 1)
            cur[j] = value
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1] if prev[-1] <= limit else limit + 1


def _typo_limit(token: str) -> int:
    return 1 if len(token) <= 5 else 2


class _Entry:
    __slots__ = ("item", "norm", "stem", "words", "acronym", "keywords", "bonus", "small", "name_len", "sort_name")

    def __init__(self, item: Item):
        self.item = item
        self.norm = normalize(item.name)
        stem = item.name
        if item.kind in _STEM_KINDS and "." in item.name.strip("."):
            stem = item.name.rsplit(".", 1)[0]
        self.stem = normalize(stem)
        words, acronym = _split_words(item.name)
        self.words = words
        self.acronym = acronym
        self.keywords = tuple(k for k in (normalize(k) for k in item.keywords) if k)
        self.bonus = KIND_BONUS.get(item.kind, 0.0)
        self.small = item.kind in _SMALL_KINDS
        self.name_len = len(item.name)
        self.sort_name = item.name.casefold()


class SearchIndex:
    def __init__(self, items: Iterable[Item], usage: Mapping[str, int] | None = None):
        self._entries: list[_Entry] = []
        seen_keys: set[str] = set()
        seen_app_names: set[str] = set()
        for item in items:
            if item.key in seen_keys:
                continue
            if item.kind == KIND_APP:
                # The same program usually appears in both the per-user and all-users Start Menu.
                app_name = " ".join(item.name.casefold().split())
                if app_name in seen_app_names:
                    continue
                seen_app_names.add(app_name)
            seen_keys.add(item.key)
            self._entries.append(_Entry(item))
        self._small_ids = [idx for idx, e in enumerate(self._entries) if e.small]
        self._usage = dict(usage or {})
        self._vocab: dict[str, list[int]] = {}
        for idx, entry in enumerate(self._entries):
            for word in entry.words:
                if len(word) >= 2:
                    self._vocab.setdefault(word, []).append(idx)
        self._buckets: dict[str, list[str]] = {}
        for word in self._vocab:
            self._buckets.setdefault(word[0], []).append(word)
        self._fuzzy_cache: OrderedDict[str, dict[int, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def items(self) -> list[Item]:
        return [e.item for e in self._entries]

    def with_usage(self, usage: Mapping[str, int]) -> "SearchIndex":
        clone = object.__new__(SearchIndex)
        clone.__dict__.update(self.__dict__)
        clone._usage = dict(usage)
        clone._fuzzy_cache = OrderedDict()
        return clone

    # ----------------------------------------------------------------------------- scoring
    def _usage_bonus(self, key: str) -> float:
        count = self._usage.get(key, 0)
        return min(60.0, 15.0 * math.log2(1 + count)) if count else 0.0

    @staticmethod
    def _direct_score(e: _Entry, q: str, tokens: list[str], compact: str) -> tuple[float, str] | None:
        norm = e.norm
        if q == norm or q == e.stem or q in e.keywords:
            # "notepad" matches both Notepad and Notepad++ after normalisation; the literal wins.
            literal = q == e.sort_name or (e.stem == q and e.sort_name.startswith(q + "."))
            return SCORE_EXACT + (20.0 if literal else 0.0), "exact"
        if norm.startswith(q) or e.stem.startswith(q):
            return SCORE_PREFIX - min(len(norm) - len(q), 80) * 0.5, "prefix"
        for kw in e.keywords:
            if kw.startswith(q):
                return SCORE_PREFIX - 5 - min(len(kw) - len(q), 40) * 0.5, "prefix"
        words = e.words
        if all(t in words for t in tokens):
            return SCORE_WHOLE_WORD - min(len(norm) - len(q), 80) * 0.3, "word"
        if all(any(w.startswith(t) for w in words) for t in tokens):
            return SCORE_WORD - min(len(norm) - len(q), 80) * 0.3, "word"
        if len(compact) >= 2 and e.acronym.startswith(compact):
            return SCORE_ACRONYM - (len(e.acronym) - len(compact)) * 3, "acronym"
        pos = norm.find(q)
        if pos >= 0:
            return SCORE_SUBSTRING - min(pos, 40) - min(len(norm), 80) * 0.2, "substring"
        if len(tokens) > 1 and all(t in norm for t in tokens):
            return SCORE_TOKENS - min(len(norm), 80) * 0.2, "tokens"
        for kw in e.keywords:
            if q in kw:
                return SCORE_SUBSTRING - 10, "substring"
        return None

    def _fuzzy_word_matches(self, token: str) -> dict[int, int]:
        """Entries containing a word within typo distance of ``token``: {entry index: distance}."""
        cached = self._fuzzy_cache.get(token)
        if cached is not None:
            self._fuzzy_cache.move_to_end(token)
            return cached
        limit = _typo_limit(token)
        found: dict[int, int] = {}
        candidates = list(self._buckets.get(token[0], ()))
        if len(token) > 1 and token[1] != token[0]:
            candidates += self._buckets.get(token[1], ())  # first two letters swapped
        n = len(token)
        for word in candidates:
            if len(word) + limit < n:
                continue
            best = bounded_distance(token, word, limit)
            if best and len(word) > n:  # also allow the token to be a mistyped prefix of the word
                for cut in (n - 1, n, n + 1):
                    if 0 < cut < len(word):
                        best = min(best, bounded_distance(token, word[:cut], limit))
            if best <= limit:
                for idx in self._vocab[word]:
                    if best < found.get(idx, limit + 1):
                        found[idx] = best
        self._fuzzy_cache[token] = found
        if len(self._fuzzy_cache) > 64:
            self._fuzzy_cache.popitem(last=False)
        return found

    @staticmethod
    def _subsequence_score(e: _Entry, compact: str) -> float | None:
        text = e.norm.replace(" ", "")
        i = -1
        pos = 0
        for word in e.norm.split(" "):  # the match must begin at the start of a word
            if word and word[0] == compact[0]:
                i = pos
                break
            pos += len(word)
        if i < 0:
            return None
        segments = 1
        last = i
        for ch in compact[1:]:
            j = text.find(ch, last + 1)
            if j < 0:
                return None
            if j != last + 1:
                segments += 1
            last = j
        if segments > max(2, math.ceil(len(compact) / 2)):
            return None
        return SCORE_SUBSEQUENCE - 15 * (segments - 1) - min(len(text) - len(compact), 40) * 0.5

    # ----------------------------------------------------------------------------- search
    def search(self, query: str, limit: int = 20) -> list[Result]:
        q = normalize(query)
        if not q:
            return []
        tokens = q.split(" ")
        compact = "".join(tokens)
        scored: dict[int, tuple[float, str]] = {}

        single = len(tokens) == 1
        word_start = " " + q
        for idx, e in enumerate(self._entries):
            norm = e.norm
            if len(q) == 1:  # one character: only word starts, otherwise nearly everything matches
                hit = norm.startswith(q) or word_start in norm
            elif single:
                hit = q in norm
            else:
                hit = all(t in norm for t in tokens)
            if not hit and not (e.keywords or (len(compact) >= 2 and e.acronym.startswith(compact))):
                continue
            result = self._direct_score(e, q, tokens, compact)
            if result:
                scored[idx] = result

        # Typo tolerance only when the strong tiers did not already fill the list.
        if len(scored) < limit and len(compact) >= 3:
            per_token: list[dict[int, int] | None] = []
            for t in tokens:
                per_token.append(self._fuzzy_word_matches(t) if len(t) >= 3 else None)
            fuzzy_tokens = [m for m in per_token if m is not None]
            if fuzzy_tokens:
                candidate_ids = set(min(fuzzy_tokens, key=len))
                for idx in candidate_ids:
                    if idx in scored:
                        continue
                    e = self._entries[idx]
                    total = 0
                    ok = True
                    for t, matches in zip(tokens, per_token):
                        if t in e.norm:
                            continue
                        if matches is None or idx not in matches:
                            ok = False
                            break
                        total += matches[idx]
                    if ok and total > 0:
                        scored[idx] = (SCORE_FUZZY - 40 * total - min(len(e.norm), 80) * 0.2, "fuzzy")

        if len(scored) < limit and len(compact) >= 2:
            for idx in self._small_ids:
                if idx in scored:
                    continue
                e = self._entries[idx]
                s = self._subsequence_score(e, compact)
                if s is not None:
                    scored[idx] = (s, "subsequence")

        results = []
        for idx, (score, match) in scored.items():
            e = self._entries[idx]
            results.append((score + e.bonus + self._usage_bonus(e.item.key), match, e))
        results.sort(key=lambda r: (-r[0], KIND_ORDER.get(r[2].item.kind, 99), r[2].name_len, r[2].sort_name, r[2].item.key))
        return [Result(e.item, round(score, 3), match) for score, match, e in results[:limit]]
