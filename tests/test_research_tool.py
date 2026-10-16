"""Website research: grounded extraction, robots, SSRF safety, bounded fetching."""
from __future__ import annotations

from types import SimpleNamespace

from packages.tools.netsafety import is_public_http_url
from packages.tools.research import StubResearchTool, WebsiteResearchTool

HOME = """<html><head><title>Acme Plumbing</title>
<meta name="description" content="Family-owned plumbing company serving Testville since 1998."></head>
<body><h1>Plumbing you can trust</h1><ul><li>Drain cleaning</li><li>Water heaters</li></ul>
<p>We are a family run plumbing business with licensed plumbers on call every day of the week.</p>
<a href="/about-us">About us</a><a href="https://other.com/x">elsewhere</a><script>var secret='x'</script></body></html>"""
ABOUT = "<html><body><h2>Our story</h2><p>Started by two brothers in a garage, we now serve the whole county with fast plumbing repairs.</p></body></html>"


class Http:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        body = self.pages.get(url)
        if body is None:
            return SimpleNamespace(status_code=404, text="", headers={})
        if isinstance(body, tuple):
            return SimpleNamespace(status_code=body[0], text="", headers={"location": body[1]})
        return SimpleNamespace(status_code=200, text=body, headers={"content-type": "text/html"})


class Robots:
    def __init__(self, blocked=()):
        self.blocked = blocked

    def is_allowed(self, url, ua):
        return not any(b in url for b in self.blocked)


def tool(pages, blocked=()):
    http = Http(pages)
    return WebsiteResearchTool(http, Robots(blocked), resolver=lambda host: ["93.184.216.34"]), http


def test_extracts_only_what_is_on_the_page():
    t, http = tool({"https://acme.com": HOME, "https://acme.com/about-us": ABOUT})
    r = t.research("acme.com")
    assert r["status"] == "ok" and r["title"] == "Acme Plumbing"
    assert r["hook"].startswith("Family-owned plumbing company") and r["hook_source"] == "meta_description"
    assert r["hook"] in r["hook_evidence"] and "Drain cleaning" in r["services"]
    assert "secret" not in r["text"] and "brothers in a garage" in r["text"]        # script ignored, about page read
    assert "https://other.com/x" not in http.urls                                    # never leaves the prospect's own site


def test_hook_falls_back_to_page_text_then_heading():
    t, _ = tool({"https://a.com": "<html><body><p>We provide emergency plumbing repair across the whole metro area, day or night.</p></body></html>"})
    assert t.research("a.com")["hook_source"] == "page_text"
    t, _ = tool({"https://a.com": "<html><body><h1>Metro Plumbing Co</h1></body></html>"})
    assert t.research("a.com")["hook_source"] == "heading"
    t, _ = tool({"https://a.com": "<html><body></body></html>"})
    assert t.research("a.com")["hook"] is None


def test_robots_disallow_is_respected_and_nothing_is_fetched():
    t, http = tool({"https://acme.com": HOME}, blocked=("acme.com",))
    assert t.research("acme.com")["status"] == "blocked_by_robots" and http.urls == []


def test_unreachable_and_no_website():
    t, _ = tool({})
    assert t.research("down.com")["status"] == "unreachable"
    assert t.research(None)["status"] == "no_website"


def test_redirects_are_followed_but_each_hop_is_revalidated():
    t, http = tool({"https://a.com": (301, "https://www.a.com/"), "https://www.a.com/": HOME})
    assert t.research("a.com")["status"] == "ok"
    t, http = tool({"https://evil.com": (302, "http://169.254.169.254/latest/meta-data/")})
    assert t.research("evil.com")["status"] == "unreachable" and not any("169.254" in u for u in http.urls)


def test_non_html_content_is_ignored():
    http = Http({})
    http.get = lambda url, headers=None, timeout=None: SimpleNamespace(status_code=200, text="%PDF", headers={"content-type": "application/pdf"})
    assert WebsiteResearchTool(http, Robots(), resolver=lambda h: ["93.184.216.34"]).research("a.com")["status"] == "unreachable"


def test_private_urls_are_refused_without_any_request():
    http = Http({"http://localhost/": HOME})
    t = WebsiteResearchTool(http, Robots(), resolver=lambda h: ["93.184.216.34"])
    for url in ("http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/", "http://169.254.169.254/", "http://intranet.local/"):
        assert t.research(url)["status"] == "unsafe_url"
    assert http.urls == []


def test_netsafety_rules():
    resolver_private = lambda h: ["10.1.2.3"]            # noqa: E731
    assert not is_public_http_url("ftp://a.com") and not is_public_http_url("http:///x") and not is_public_http_url("javascript:alert(1)")
    assert not is_public_http_url("http://[::1]/") and not is_public_http_url("http://192.168.1.1")
    assert not is_public_http_url("https://sneaky.com", resolver_private)               # DNS rebinding style: public name -> private IP
    assert is_public_http_url("https://93.184.216.34") and is_public_http_url("https://acme.com", lambda h: ["93.184.216.34"])


def test_stub_research_finds_nothing_so_nothing_gets_contacted():
    assert StubResearchTool().research("acme.com")["status"] == "unreachable"


def test_user_agent_header_identifies_us_and_carries_a_contact_when_configured(monkeypatch):
    from packages.core.config import get_settings
    from packages.tools.free_enrichment import USER_AGENT, request_user_agent
    assert request_user_agent().startswith(USER_AGENT) and "contact:" not in request_user_agent()
    monkeypatch.setenv("SMTP_FROM_EMAIL", "sam@xflow.example"); get_settings.cache_clear()
    try:
        assert "contact: sam@xflow.example" in request_user_agent()
    finally:
        monkeypatch.delenv("SMTP_FROM_EMAIL"); get_settings.cache_clear()
