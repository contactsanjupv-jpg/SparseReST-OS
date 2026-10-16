"""
Every test here uses FakeHttpClient -- no real network call to
openstreetmap.org or any business website happens anywhere in this suite.
Response shapes are modeled on Nominatim's and Overpass's real, documented
JSON formats. This proves request construction and response parsing; it
does NOT prove live connectivity, which this sandbox's network egress
allowlist doesn't permit testing anyway (see free_enrichment.py's module
docstring).
"""
from __future__ import annotations

from packages.tools.free_enrichment import (
    FreeEnrichmentTool,
    NominatimGeocoder,
    OverpassEnrichmentTool,
    WebsiteContactScraperTool,
)


class FakeResponse:
    def __init__(self, json_data=None, text=""):
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


class FakeHttpClient:
    """Routes by substring match on the URL -- lets one fake client serve
    Nominatim, Overpass, and a business website in the same test."""

    def __init__(self):
        self.calls = []
        self.responses = {}  # url_substring -> FakeResponse

    def register(self, url_substring: str, response: FakeResponse):
        self.responses[url_substring] = response

    def get(self, url, *, params=None, headers=None, timeout=15.0):
        self.calls.append({"url": url, "params": params, "headers": headers})
        for substring, response in self.responses.items():
            if substring in url:
                return response
        raise AssertionError(f"No fake response registered for URL: {url}")


class AlwaysAllowRobots:
    def is_allowed(self, url, user_agent):
        return True


class AlwaysDenyRobots:
    def is_allowed(self, url, user_agent):
        return False


def test_geocoder_parses_real_nominatim_response_shape():
    client = FakeHttpClient()
    client.register("nominatim.openstreetmap.org", FakeResponse(json_data=[
        {"lat": "39.7684", "lon": "-86.1581", "display_name": "Indianapolis, Marion County, Indiana, USA"}
    ]))
    geocoder = NominatimGeocoder(client, sleep_fn=lambda s: None)
    result = geocoder.geocode("Indianapolis, IN")

    assert result.lat == 39.7684
    assert result.lon == -86.1581
    assert "User-Agent" in client.calls[0]["headers"]


def test_geocoder_returns_none_for_no_results():
    client = FakeHttpClient()
    client.register("nominatim", FakeResponse(json_data=[]))
    geocoder = NominatimGeocoder(client, sleep_fn=lambda s: None)
    assert geocoder.geocode("Nowhere Land") is None


def test_geocoder_respects_rate_limit_between_calls():
    client = FakeHttpClient()
    client.register("nominatim", FakeResponse(json_data=[{"lat": "1.0", "lon": "2.0", "display_name": "x"}]))

    fake_time = {"t": 0.0}
    sleeps = []
    geocoder = NominatimGeocoder(client, clock_fn=lambda: fake_time["t"], sleep_fn=lambda s: sleeps.append(s))

    geocoder.geocode("Place A")
    geocoder.geocode("Place B")

    assert len(sleeps) == 1
    assert sleeps[0] > 0.9


def test_overpass_tool_finds_real_shaped_business_results():
    client = FakeHttpClient()
    client.register("nominatim", FakeResponse(json_data=[{"lat": "39.7684", "lon": "-86.1581", "display_name": "Indianapolis"}]))
    client.register("overpass-api.de", FakeResponse(json_data={
        "elements": [
            {"type": "node", "tags": {"name": "Reliable Plumbing Co", "craft": "plumber", "website": "https://reliableplumbing.example"}},
            {"type": "node", "tags": {"craft": "plumber"}},
        ]
    }))

    tool = OverpassEnrichmentTool(client, geocoder=NominatimGeocoder(client, sleep_fn=lambda s: None))
    leads = tool.find_and_enrich(icp={"industries": ["plumbing"], "geography": "Indianapolis, IN"}, limit=10)

    assert len(leads) == 1
    assert leads[0].company_name == "Reliable Plumbing Co"
    assert leads[0].website == "https://reliableplumbing.example"
    assert leads[0].contact_email_verified is False


def test_overpass_tool_returns_empty_without_geography_or_recognized_industry():
    client = FakeHttpClient()
    tool = OverpassEnrichmentTool(client)
    assert tool.find_and_enrich(icp={"industries": ["plumbing"]}, limit=10) == []
    assert tool.find_and_enrich(icp={"geography": "Indianapolis"}, limit=10) == []


def test_overpass_tool_supports_raw_osm_tags_override():
    client = FakeHttpClient()
    client.register("nominatim", FakeResponse(json_data=[{"lat": "1.0", "lon": "2.0", "display_name": "x"}]))
    client.register("overpass-api.de", FakeResponse(json_data={"elements": [{"tags": {"name": "Custom Trade Co"}}]}))

    tool = OverpassEnrichmentTool(client, geocoder=NominatimGeocoder(client, sleep_fn=lambda s: None))
    leads = tool.find_and_enrich(icp={"osm_tags": [["shop", "hardware"]], "geography": "Anywhere"}, limit=5)
    assert leads[0].company_name == "Custom Trade Co"


def test_website_scraper_finds_a_real_looking_contact_email():
    client = FakeHttpClient()
    client.register("reliableplumbing.example", FakeResponse(text="<html>Call us or email info@reliableplumbing.example today!</html>"))
    scraper = WebsiteContactScraperTool(client, robots_checker=AlwaysAllowRobots())
    assert scraper.find_contact_email("https://reliableplumbing.example") == "info@reliableplumbing.example"


def test_website_scraper_filters_junk_emails():
    client = FakeHttpClient()
    client.register("example.com", FakeResponse(text="<html><img src='tracker@sentry.io'/> no real contact here</html>"))
    scraper = WebsiteContactScraperTool(client, robots_checker=AlwaysAllowRobots())
    assert scraper.find_contact_email("https://example.com") is None


def test_website_scraper_respects_robots_txt_disallow():
    client = FakeHttpClient()
    client.register("example.com", FakeResponse(text="<html>email@example.com</html>"))
    scraper = WebsiteContactScraperTool(client, robots_checker=AlwaysDenyRobots())
    assert scraper.find_contact_email("https://example.com") is None
    assert client.calls == []


def test_free_enrichment_tool_composes_discovery_and_contact_scraping():
    client = FakeHttpClient()
    client.register("nominatim", FakeResponse(json_data=[{"lat": "39.7684", "lon": "-86.1581", "display_name": "Indianapolis"}]))
    client.register("overpass-api.de", FakeResponse(json_data={"elements": [
        {"tags": {"name": "Reliable Plumbing Co", "website": "https://reliableplumbing.example"}}
    ]}))
    client.register("reliableplumbing.example", FakeResponse(text="Contact us: owner@reliableplumbing.example"))

    tool = FreeEnrichmentTool(client)
    tool._scraper = WebsiteContactScraperTool(client, robots_checker=AlwaysAllowRobots())
    leads = tool.find_and_enrich(icp={"industries": ["plumbing"], "geography": "Indianapolis, IN"}, limit=5)

    assert len(leads) == 1
    assert leads[0].contact_email == "owner@reliableplumbing.example"
    assert leads[0].contact_email_source == "company_website_public_listing"
    assert leads[0].contact_email_verified is False
