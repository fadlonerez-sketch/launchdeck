"""Strict URL recognition and web-search URL construction."""

from __future__ import annotations

import re
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
# Characters left untouched when re-quoting the path/query of a URL the user typed.
_SAFE_PATH = "/%:@!$&'()*+,;=-._~"
_SAFE_QUERY = _SAFE_PATH + "?"


def _valid_host(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    if host == "localhost":
        return True
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return all(0 <= int(part) <= 255 for part in host.split("."))
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_host.rstrip(".").split(".")
    return len(labels) >= 2 and all(_LABEL_RE.match(label) for label in labels) and not labels[-1].isdigit()


def parse_explicit_url(text: str) -> str | None:
    """Return a normalised http(s) URL if ``text`` is clearly one, otherwise None.

    Accepted: ``http://…`` / ``https://…`` with a valid host, and ``www.example.com/…``
    (treated as https). Anything with whitespace, control characters, credentials or another
    scheme (file:, javascript:, ms-settings:, …) is rejected.
    """
    text = (text or "").strip()
    if not text or _CONTROL_RE.search(text) or len(text) > 2048:
        return None
    lower = text.lower()
    if lower.startswith("www."):
        text = "https://" + text
        lower = text.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port  # raises ValueError for an invalid port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    if not _valid_host(host):
        return None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit(
        (
            parts.scheme.lower(),
            netloc,
            quote(parts.path, safe=_SAFE_PATH),
            quote(parts.query, safe=_SAFE_QUERY),
            quote(parts.fragment, safe=_SAFE_QUERY),
        )
    )


def is_valid_website_url(text: str) -> bool:
    return parse_explicit_url(text) is not None


def validate_search_template(template: str) -> str | None:
    """Return an error message, or None if the template is a usable search URL."""
    if "{query}" not in template:
        return "The search address must contain {query} where the search text goes."
    if parse_explicit_url(template.replace("{query}", "test")) is None:
        return "The search address must be a valid http:// or https:// URL."
    return None


def build_search_url(template: str, query: str) -> str:
    """Insert the URL-encoded query into a validated search template."""
    error = validate_search_template(template)
    if error:
        raise ValueError(error)
    return template.replace("{query}", quote_plus(query.strip()))


def display_domain(url: str) -> str:
    try:
        host = urlsplit(url).hostname or url
    except ValueError:
        return url
    return host[4:] if host.startswith("www.") else host
