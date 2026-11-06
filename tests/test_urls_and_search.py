from urllib.parse import parse_qs, urlsplit

import pytest

from launchdeck.commands.model import CommandDef
from launchdeck.launcher.models import KIND_COMMAND, KIND_URL, KIND_WEBSEARCH, KIND_WEBSITE, Item
from launchdeck.launcher.ranking import SearchIndex
from launchdeck.launcher.search import SearchContext, library_items, run_search
from launchdeck.launcher.urls import (
    build_search_url, display_domain, parse_explicit_url, validate_search_template,
)
from launchdeck.storage.data_store import Website


@pytest.mark.parametrize("text, expected", [
    ("https://example.com", "https://example.com"),
    ("HTTP://Example.COM/Path?q=1#frag", "http://example.com/Path?q=1#frag"),
    ("www.github.com/user", "https://www.github.com/user"),
    ("https://example.com:8080/a b".replace(" ", "%20"), "https://example.com:8080/a%20b"),
    ("https://xn--mnchen-3ya.de", "https://xn--mnchen-3ya.de"),
    ("http://localhost:3000", "http://localhost:3000"),
])
def test_valid_urls(text, expected):
    assert parse_explicit_url(text) == expected


@pytest.mark.parametrize("text", [
    "example.com",  # bare domains are not treated as URLs (could be a file name)
    "notes.txt",
    "javascript:alert(1)",
    "file:///C:/Windows/System32/cmd.exe",
    "ms-settings:display",
    "https://",
    "https://exa mple.com",
    "https://user:pass@example.com",
    "https://example.com\n.evil",
    "https://-bad-.com",
    "https://example.com:99999",
    "calc.exe",
    "",
])
def test_rejected_urls(text):
    assert parse_explicit_url(text) is None


def test_search_url_encoding():
    url = build_search_url("https://www.google.com/search?q={query}", "  C++ & rock'n'roll / ü  ")
    assert url.startswith("https://www.google.com/search?q=")
    assert "&" not in url.split("q=", 1)[1]
    assert parse_qs(urlsplit(url).query)["q"] == ["C++ & rock'n'roll / ü"]


def test_search_template_validation():
    assert validate_search_template("https://duckduckgo.com/?q={query}") is None
    assert validate_search_template("https://duckduckgo.com/?q=") is not None
    assert validate_search_template("javascript:{query}") is not None
    with pytest.raises(ValueError):
        build_search_url("ftp://x/{query}", "a")


def test_display_domain():
    assert display_domain("https://www.youtube.com/watch?v=1") == "youtube.com"


def _ctx(**kw):
    cmd = CommandDef("Notepad", ["np"], "notepad.exe", ["{query}"], id="c1")
    disabled = CommandDef("Old tool", ["old"], "notepad.exe", ["{query}"], enabled=False, id="c2")
    lib = library_items([Website("YouTube", "https://www.youtube.com", ["yt"])], [cmd, disabled], [])
    defaults = dict(
        files=SearchIndex([]),
        library=SearchIndex(lib),
        commands_by_alias={"np": cmd, "old": disabled},
        limit=8,
    )
    defaults.update(kw)
    return SearchContext(**defaults)


def test_web_search_fallback_for_unmatched_query():
    res = run_search(_ctx(), "quantum chromodynamics")
    assert len(res) == 1
    assert res[0].item.kind == KIND_WEBSEARCH
    assert "Google" in res[0].item.name
    assert "quantum+chromodynamics" in res[0].item.target


def test_web_fallback_can_be_disabled():
    assert run_search(_ctx(web_fallback=False), "quantum chromodynamics") == []


def test_url_result_first():
    res = run_search(_ctx(), "https://example.com/x")
    assert res[0].item.kind == KIND_URL
    assert res[0].item.target == "https://example.com/x"


def test_command_alias_with_argument_and_disabled_commands():
    res = run_search(_ctx(), "np my notes.txt")
    assert res[0].item.kind == KIND_COMMAND
    assert res[0].argument == "my notes.txt"
    # Disabled commands are neither listed nor runnable via their alias.
    assert all(r.item.target != "c2" for r in run_search(_ctx(), "old something"))
    assert all(r.item.target != "c2" for r in run_search(_ctx(), "old tool"))


def test_websites_found_by_keyword():
    res = run_search(_ctx(), "yt")
    assert res[0].item.kind == KIND_WEBSITE
    assert res[0].item.detail == "youtube.com"


def test_merged_results_respect_limit_and_dedupe():
    files = SearchIndex([Item("file", f"report {i}.txt", f"C:/r/{i}.txt") for i in range(30)])
    res = run_search(_ctx(files=files, limit=5), "report")
    assert len([r for r in res if r.item.kind != KIND_WEBSEARCH]) == 5
    assert len({r.item.key for r in res}) == len(res)
