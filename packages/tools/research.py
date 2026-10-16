"""
Prospect research: read the prospect's OWN public website and extract only what is actually there.

Nothing here invents facts. The result is a dict stored on `prospects.research`; the optional `hook` is a
short phrase lifted from the site's text (with `hook_evidence`, the exact source sentence) so personalization
can only ever reference something verifiable. Respects robots.txt, blocks private-network URLs (SSRF), caps
pages/bytes/time, follows at most 3 redirects (each re-validated), and uses an identifying User-Agent.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

from packages.tools.free_enrichment import USER_AGENT, HttpClient, RobotsChecker, UrllibRobotsChecker, request_user_agent
from packages.tools.netsafety import is_public_http_url

MAX_PAGES = 3
MAX_BYTES = 300_000
MAX_TEXT_CHARS = 6000
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
_ABOUT_HINTS = ("about", "service", "what-we-do", "our-work", "who-we-are", "company")
_WS = re.compile(r"\s+")


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self.items: list[str] = []
        self.paragraphs: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._href: Optional[str] = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP_TAGS and tag != "head":
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description") and a.get("content") and not self.description:
                self.description = _WS.sub(" ", a["content"]).strip()
        if tag in ("h1", "h2", "h3", "li", "p"):
            self._stack.append(tag)
            self._buf = []
        if tag == "a" and a.get("href"):
            self._href = a["href"]
            self._anchor_buf: list[str] = []

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and tag != "head" and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href is not None:
            self.links.append((self._href, _WS.sub(" ", "".join(self._anchor_buf)).strip()))
            self._href = None
        if self._stack and tag == self._stack[-1]:
            text = _WS.sub(" ", "".join(self._buf)).strip()
            kind = self._stack.pop()
            if text:
                {"h1": self.headings, "h2": self.headings, "h3": self.headings, "li": self.items, "p": self.paragraphs}[kind].append(text)
            self._buf = []

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._stack:
            self._buf.append(data)
        if self._href is not None:
            self._anchor_buf.append(data)


class ResearchTool(ABC):
    @abstractmethod
    def research(self, website: Optional[str]) -> dict:
        """Returns a research dict; `status` is one of ok | no_website | unsafe_url | blocked_by_robots | unreachable."""


class StubResearchTool(ResearchTool):
    """Offline default: finds nothing, honestly. With the stub, qualification's personalization gate keeps
    every prospect from being contacted -- by design, no outreach without real research."""

    def research(self, website: Optional[str]) -> dict:
        return {"status": "unreachable", "note": "no research tool configured (ENRICHMENT_MODE=stub)", "fetched_at": _now()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(url: str) -> str:
    return url if "://" in url else f"https://{url}"


class WebsiteResearchTool(ResearchTool):
    def __init__(self, http_client: HttpClient, robots_checker: Optional[RobotsChecker] = None, resolver=None):
        self._http = http_client
        self._robots = robots_checker or UrllibRobotsChecker()
        self._resolver = resolver

    def _fetch(self, url: str) -> Optional[str]:
        for _ in range(4):
            if not is_public_http_url(url, self._resolver):
                return None
            response = self._http.get(url, headers={"User-Agent": request_user_agent()}, timeout=10.0)
            status = getattr(response, "status_code", 200)
            if status in (301, 302, 303, 307, 308):
                location = (getattr(response, "headers", {}) or {}).get("location")
                if not location:
                    return None
                url = urljoin(url, location)
                continue
            if status != 200:
                return None
            ctype = (getattr(response, "headers", {}) or {}).get("content-type", "text/html")
            if "html" not in ctype.lower():
                return None
            return (response.text or "")[:MAX_BYTES]
        return None

    def research(self, website: Optional[str]) -> dict:
        if not website:
            return {"status": "no_website", "fetched_at": _now()}
        base = _clean(website)
        if not is_public_http_url(base, self._resolver):
            return {"status": "unsafe_url", "note": "URL points at a non-public address", "fetched_at": _now()}
        if not self._robots.is_allowed(base, USER_AGENT):
            return {"status": "blocked_by_robots", "fetched_at": _now()}
        try:
            html = self._fetch(base)
        except Exception as exc:
            return {"status": "unreachable", "note": exc.__class__.__name__, "fetched_at": _now()}
        if not html:
            return {"status": "unreachable", "note": "no readable HTML response", "fetched_at": _now()}

        home = _Extractor()
        home.feed(html)
        pages, extractors = [base], [home]
        host = urlparse(base).netloc
        for href, label in home.links:
            if len(pages) >= MAX_PAGES:
                break
            target = urljoin(base, href)
            if urlparse(target).netloc != host or target in pages or not any(h in (href + " " + label).lower() for h in _ABOUT_HINTS):
                continue
            if not self._robots.is_allowed(target, USER_AGENT):
                continue
            try:
                sub = self._fetch(target)
            except Exception:
                sub = None
            if sub:
                ex = _Extractor()
                ex.feed(sub)
                pages.append(target)
                extractors.append(ex)

        title = _WS.sub(" ", home.title).strip()
        description = home.description
        headings = [h for ex in extractors for h in ex.headings if 3 <= len(h) <= 120][:12]
        services = []
        for ex in extractors:
            for item in ex.items:
                if 3 <= len(item) <= 60 and item not in services:
                    services.append(item)
        services = services[:8]
        paragraphs = [p for ex in extractors for p in ex.paragraphs if len(p) >= 40]
        text = " ".join([title, description, *headings, *paragraphs])[:MAX_TEXT_CHARS]

        hook, source, evidence = None, None, None
        if 30 <= len(description) <= 220:
            hook, source, evidence = description, "meta_description", description
        elif paragraphs:
            first = paragraphs[0]
            hook, source, evidence = (first[:200].rsplit(" ", 1)[0] if len(first) > 200 else first), "page_text", first
        elif headings:
            hook, source, evidence = headings[0], "heading", headings[0]
        return {
            "status": "ok", "pages": pages, "title": title, "description": description, "headings": headings,
            "services": services, "text": text, "hook": hook, "hook_source": source, "hook_evidence": evidence,
            "fetched_at": _now(), "llm_used": False,
        }
