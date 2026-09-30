"""Normalize search destinations and parse saved or live SERP HTML."""
from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


def normalize_destination(url: str) -> str | None:
    """Unwrap known redirects; reject opaque wrappers and engine navigation."""
    for _ in range(3):
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
                return None
            _ = parsed.port  # Reject malformed ports.
        except ValueError:
            return None
        google = host == "google.com" or host.endswith(".google.com")
        ddg = host == "duckduckgo.com" or host.endswith(".duckduckgo.com")
        if google or ddg:
            query = parse_qs(parsed.query)
            target = None
            if google and parsed.path == "/url":
                target = query.get("q", query.get("url", [None]))[0]
            elif ddg and parsed.path.rstrip("/") == "/l":
                target = query.get("uddg", [None])[0]
            if target and target != url:
                url = target
                continue
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    return None


def normalize_results(results: list[dict], count: int) -> list[dict]:
    seen: set[str] = set()
    cleaned = []
    for result in results:
        url = normalize_destination(result.get("url") or "")
        if not url or url in seen or not (result.get("title") or "").strip():
            continue
        seen.add(url)
        cleaned.append({**result, "url": url})
        if len(cleaned) >= count:
            break
    return cleaned


def parse_serp(html: str, engine: str, count: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('form[action*="recaptcha"], .g-recaptcha, #captcha-form, form#challenge-form'):
        return []
    results = []
    if engine == "google":
        for heading in soup.select("a h3"):
            anchor = heading.find_parent("a")
            if anchor is None:
                continue
            block = heading.find_parent(class_="g") or heading.find_parent(attrs={"data-hveid": True})
            snippet = block.select_one("div.VwiC3b, span.aCOpRe") if block else None
            results.append({"title": heading.get_text(" ", strip=True),
                            "url": urljoin("https://www.google.com", str(anchor.get("href") or "")),
                            "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
    elif engine == "duckduckgo":
        for item in soup.select('.result, .web-result, [data-testid="result"]'):
            anchor = item.select_one("h2 a, .result__title a, .result__a")
            snippet = item.select_one(".result__snippet, .snippet")
            if anchor:
                results.append({"title": anchor.get_text(" ", strip=True),
                                "url": urljoin("https://duckduckgo.com", str(anchor.get("href") or "")),
                                "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
    elif engine == "duckduckgo lite":
        for anchor in soup.select("a.result-link"):
            row = anchor.find_parent("tr")
            following = row.find_next_sibling("tr") if row else None
            snippet = following.select_one(".result-snippet") if following else None
            results.append({"title": anchor.get_text(" ", strip=True),
                            "url": urljoin("https://lite.duckduckgo.com", str(anchor.get("href") or "")),
                            "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
    return normalize_results(results, count)
