"""
Real, $0-cost enrichment using open public data -- no API key, no account,
no paid provider. Two real capabilities composed into the EnrichmentTool
interface (packages/tools/enrichment.py) that ProspectingAgent already uses,
so ProspectingAgent's own code never changes:

1. OverpassEnrichmentTool: finds real businesses matching an ICP's
   industries + geography via OpenStreetMap's Overpass API (business
   locations, names, and often a website/phone) and Nominatim (geocoding a
   place name to coordinates). Both are free, public, no-signup services --
   used here within their documented usage policies (a real, identifying
   User-Agent; capped request rate; no bulk/heavy usage). OSM's own tagging
   coverage for a given trade varies by region -- this is real, honestly
   incomplete data, not fabricated completeness.

2. WebsiteContactScraperTool: given a business's own public website, looks
   for a publicly self-published contact email (e.g., "info@company.com"
   listed on their own contact page) -- never guesses, never invents,
   respects robots.txt before fetching anything.

FreeEnrichmentTool composes both into the full find_and_enrich() the
EnrichmentTool interface requires. Every CompanyLead this produces has
contact_email_verified=False (nothing here performs real verification, e.g.
an SMTP handshake) and a specific, truthful contact_email_source -- exactly
the same honesty discipline StubEnrichmentTool documents, just backed by
real data instead of fake data.

IMPORTANT: this sandbox's own network egress is restricted to a fixed
allowlist that does NOT include openstreetmap.org or arbitrary business
websites -- these calls have not been (and cannot be) exercised live from
within this build environment. Every class below is built around an
injectable HTTP client specifically so the logic can be fully tested
offline (tests/test_free_enrichment.py uses a fake client with canned
responses shaped like Nominatim/Overpass's real, documented response
formats). Treat the request-construction and response-parsing logic as
tested; treat live connectivity as unverified until you run it on a machine
with normal internet access -- a suggested first real-world smoke test is
in docs/ENVIRONMENT.md.
"""
from __future__ import annotations

import re
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import urljoin, urlparse

from packages.tools.enrichment import CompanyLead, EnrichmentTool

USER_AGENT = "SparseRESTCompanyOS/1.0"        # the stable product token (robots.txt rules match on this)


def request_user_agent() -> str:
    """The header actually sent: the stable token plus a real contact address when one is configured (Nominatim's
    usage policy requires an identifying contact, and it is simple courtesy to the sites we read)."""
    from packages.core.config import get_settings
    contact = get_settings().smtp_from_email
    return f"{USER_AGENT} (business research; contact: {contact})" if contact else f"{USER_AGENT} (business research)"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0  # Nominatim usage policy: max 1 request/second
DEFAULT_SEARCH_RADIUS_METERS = 50_000

# Best-effort ICP-industry -> OpenStreetMap tag mapping for home-service trades.
# OSM tag coverage is real but uneven across regions -- this is not a complete
# or authoritative mapping, and product_config.icp can supply a raw
# "osm_tags" list directly to bypass it entirely for anything not covered here.
INDUSTRY_TO_OSM_TAGS = {
    "hvac": [("craft", "hvac")],
    "plumbing": [("craft", "plumber")],
    "roofing": [("craft", "roofer")],
    "landscaping": [("craft", "gardener"), ("shop", "garden_centre")],
    "pest_control": [("office", "pest_control"), ("shop", "pest_control")],
    "electrician": [("craft", "electrician")],
    "painting": [("craft", "painter")],
}


class DiscoverySourceError(RuntimeError):
    """The free discovery source (Nominatim/Overpass) answered with something we cannot use. The message is what the
    founder sees in the incident, so it names the service and the HTTP status instead of a JSON parse error."""


def _json_or_raise(response, service: str):
    status = getattr(response, "status_code", 200)
    if status != 200:
        snippet = " ".join((getattr(response, "text", "") or "").split())[:80]
        hint = " (the request was blocked or rate-limited -- check network access/proxy and the usage policy)" if status in (403, 429) else ""
        raise DiscoverySourceError(f"{service} returned HTTP {status}{hint}" + (f": {snippet}" if snippet else ""))
    try:
        return response.json()
    except ValueError as exc:
        raise DiscoverySourceError(f"{service} returned a response that is not valid JSON") from exc


class HttpClient(Protocol):
    def get(self, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None, timeout: float = 15.0): ...


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    display_name: str


class NominatimGeocoder:
    """Free geocoding, no API key. Rate-limited to the policy's 1 req/sec.
    clock_fn/sleep_fn are injectable so tests can verify the rate-limit logic
    without a real sleep (same pattern as apps/api/streaming.py)."""

    def __init__(self, http_client: HttpClient, base_url: str = "https://nominatim.openstreetmap.org",
                 clock_fn=time.monotonic, sleep_fn=time.sleep):
        self._http = http_client
        self._base_url = base_url
        self._clock_fn = clock_fn
        self._sleep_fn = sleep_fn
        self._last_request_at: float = float("-inf")

    def geocode(self, place: str) -> Optional[GeocodeResult]:
        self._respect_rate_limit()
        response = self._http.get(
            f"{self._base_url}/search",
            params={"q": place, "format": "json", "limit": 1},
            headers={"User-Agent": request_user_agent()},
        )
        results = _json_or_raise(response, "Nominatim (geocoding)")
        if not results:
            return None
        top = results[0]
        return GeocodeResult(lat=float(top["lat"]), lon=float(top["lon"]), display_name=top.get("display_name", place))

    def _respect_rate_limit(self) -> None:
        elapsed = self._clock_fn() - self._last_request_at
        if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
            self._sleep_fn(NOMINATIM_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = self._clock_fn()


def _lead_key(name: str, website: Optional[str]) -> str:
    """Must stay identical to ProspectingAgent's dedup_key_for()."""
    if website:
        return website.lower().replace("https://", "").replace("http://", "").rstrip("/")
    return name.strip().lower()


class OverpassEnrichmentTool(EnrichmentTool):
    """Finds real businesses via OpenStreetMap's Overpass API. Never marks a
    contact verified (it doesn't even have a contact yet at this stage --
    that's WebsiteContactScraperTool's job)."""

    def __init__(self, http_client: HttpClient, geocoder: Optional[NominatimGeocoder] = None,
                 overpass_url: str = "https://overpass-api.de/api/interpreter",
                 search_radius_meters: int = DEFAULT_SEARCH_RADIUS_METERS):
        self._http = http_client
        self._geocoder = geocoder or NominatimGeocoder(http_client)
        self._overpass_url = overpass_url
        self._radius = search_radius_meters

    def find_and_enrich(self, *, icp: dict, limit: int) -> list[CompanyLead]:
        tags = self._resolve_tags(icp)
        if not tags:
            return []

        geography = icp.get("geography")
        if not geography:
            return []
        location = self._geocoder.geocode(geography)
        if location is None:
            return []

        skip = set(icp.get("_skip_keys") or [])
        fetch = min(limit + len(skip), 400) if skip else limit     # ask for enough that NEW ones appear after the known ones
        query = self._build_query(tags, location.lat, location.lon, fetch)
        response = self._http.get(self._overpass_url, params={"data": query}, headers={"User-Agent": request_user_agent()})
        elements = _json_or_raise(response, "Overpass (business search)").get("elements", [])

        leads = []
        for el in elements[:fetch]:
            if len(leads) >= limit:
                break
            tags_dict = el.get("tags", {})
            name = tags_dict.get("name")
            if not name:
                continue  # an unnamed OSM node is not a usable lead
            if skip and _lead_key(name, tags_dict.get("website") or tags_dict.get("contact:website")) in skip:
                continue  # already in our database: do not re-fetch/scrape it
            leads.append(CompanyLead(
                company_name=name,
                website=tags_dict.get("website") or tags_dict.get("contact:website"),
                contact_name=None,
                contact_title=None,
                contact_email=tags_dict.get("email") or tags_dict.get("contact:email"),
                contact_email_source="openstreetmap_tag" if tags_dict.get("email") else None,
                contact_email_confidence=0.4 if tags_dict.get("email") else None,
                contact_email_verified=False,
            ))
        return leads

    def _resolve_tags(self, icp: dict) -> list[tuple[str, str]]:
        if icp.get("osm_tags"):
            return [tuple(t) for t in icp["osm_tags"]]
        tags = []
        for industry in icp.get("industries", []):
            tags.extend(INDUSTRY_TO_OSM_TAGS.get(industry.lower().replace(" ", "_"), []))
        return tags

    def _build_query(self, tags: list[tuple[str, str]], lat: float, lon: float, limit: int) -> str:
        clauses = "\n".join(
            f'  node["{k}"="{v}"](around:{self._radius},{lat},{lon});\n  way["{k}"="{v}"](around:{self._radius},{lat},{lon});'
            for k, v in tags
        )
        return f"[out:json][timeout:25];\n(\n{clauses}\n);\nout center {limit};"


class RobotsChecker(Protocol):
    def is_allowed(self, url: str, user_agent: str) -> bool: ...


class UrllibRobotsChecker:
    """Real robots.txt compliance check -- fetches and parses the target
    site's own robots.txt before WebsiteContactScraperTool touches anything
    else on that site."""

    def is_allowed(self, url: str, user_agent: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
        except Exception:
            return True  # no readable robots.txt -- default to allowed, the conventional interpretation
        return parser.can_fetch(user_agent, url)


_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_JUNK_EMAIL_SUBSTRINGS = ("sentry.io", "wixpress.com", "example.com", ".png", ".jpg", ".gif", ".svg")


class WebsiteContactScraperTool:
    """Looks for a publicly self-published contact email on a business's own
    website. Not an EnrichmentTool itself -- it enriches ONE lead at a time,
    given a URL; FreeEnrichmentTool is what composes this with discovery."""

    def __init__(self, http_client: HttpClient, robots_checker: Optional[RobotsChecker] = None):
        self._http = http_client
        self._robots = robots_checker or UrllibRobotsChecker()

    def find_contact_email(self, website_url: str) -> Optional[str]:
        from packages.tools.netsafety import is_public_http_url
        if not is_public_http_url(website_url if "://" in website_url else f"https://{website_url}"):
            return None  # untrusted URL pointing at a private/loopback address: never fetch it
        if not self._robots.is_allowed(website_url, USER_AGENT):
            return None
        try:
            response = self._http.get(website_url, headers={"User-Agent": request_user_agent()}, timeout=10.0)
        except Exception:
            return None
        matches = [m for m in _EMAIL_RE.findall(response.text) if not any(j in m.lower() for j in _JUNK_EMAIL_SUBSTRINGS)]
        return matches[0] if matches else None


class FreeEnrichmentTool(EnrichmentTool):
    """The real, $0-cost default: OpenStreetMap discovery + public-website
    contact scraping, composed into the full find_and_enrich() interface."""

    def __init__(self, http_client: HttpClient):
        self._overpass = OverpassEnrichmentTool(http_client)
        self._scraper = WebsiteContactScraperTool(http_client)

    def find_and_enrich(self, *, icp: dict, limit: int) -> list[CompanyLead]:
        leads = self._overpass.find_and_enrich(icp=icp, limit=limit)
        enriched = []
        for lead in leads:
            if not lead.contact_email and lead.website:
                found_email = self._scraper.find_contact_email(lead.website)
                if found_email:
                    lead = CompanyLead(
                        company_name=lead.company_name, website=lead.website,
                        contact_name=lead.contact_name, contact_title=lead.contact_title,
                        contact_email=found_email, contact_email_source="company_website_public_listing",
                        contact_email_confidence=0.6, contact_email_verified=False,
                    )
            enriched.append(lead)
        return enriched
