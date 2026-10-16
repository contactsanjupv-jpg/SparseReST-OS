"""One provider outage = one actionable incident (not a stream of critical pages), a clear cause, and backoff."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core import autopilot
from packages.core.incidents import IncidentService
from packages.db.models import AutopilotState, Incident, Task
from packages.db.session import session_scope
from packages.tools.enrichment import EnrichmentTool
from packages.tools.free_enrichment import DiscoverySourceError, NominatimGeocoder, OverpassEnrichmentTool, _json_or_raise
from tests.acq_helpers import Clock, FakeEnrichment, acquisition_product, drain, lead, make_registry


class Resp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data

    def json(self):
        if self._data is None:
            raise ValueError("Expecting value")
        return self._data


class Down(EnrichmentTool):
    def __init__(self, error="Nominatim (geocoding) returned HTTP 403 (the request was blocked)"):
        self.error, self.calls = error, 0

    def find_and_enrich(self, *, icp, limit):
        self.calls += 1
        raise DiscoverySourceError(self.error)


# ---- clear errors -------------------------------------------------------------------------------------------
def test_http_errors_name_the_service_and_status_instead_of_a_json_parse_error():
    with pytest.raises(DiscoverySourceError, match=r"Nominatim.*HTTP 403.*blocked or rate-limited"):
        _json_or_raise(Resp(403, "<html>Forbidden</html>"), "Nominatim (geocoding)")
    with pytest.raises(DiscoverySourceError, match="HTTP 503"):
        _json_or_raise(Resp(503, ""), "Overpass (business search)")
    with pytest.raises(DiscoverySourceError, match="not valid JSON"):
        _json_or_raise(Resp(200, "oops"), "Overpass (business search)")
    assert _json_or_raise(Resp(200, data={"elements": []}), "x") == {"elements": []}
    assert _json_or_raise(SimpleNamespace(json=lambda: [1]), "x") == [1]             # responses without status_code still work


def test_real_geocoder_and_overpass_surface_the_clear_error():
    class Http:
        def __init__(self, resp): self.resp = resp
        def get(self, url, **kw): return self.resp
    geo = NominatimGeocoder(Http(Resp(403, "Forbidden")), sleep_fn=lambda s: None)
    with pytest.raises(DiscoverySourceError, match="HTTP 403"):
        geo.geocode("Austin, Texas")
    tool = OverpassEnrichmentTool(Http(Resp(429, "slow down")), SimpleNamespace(geocode=lambda p: SimpleNamespace(lat=1.0, lon=2.0)))
    with pytest.raises(DiscoverySourceError, match="Overpass.*HTTP 429"):
        tool.find_and_enrich(icp={"industries": ["plumbing"], "geography": "Austin"}, limit=5)


# ---- one incident per outage ---------------------------------------------------------------------------------------
def open_incidents(pid, key=None):
    """Plain tuples (severity, cause, dedup_key): ORM rows must not be read after their session closes."""
    with session_scope() as s:
        q = s.query(Incident).filter_by(product_id=pid, status="open")
        rows = q.filter_by(dedup_key=key).all() if key else q.all()
        return [(i.severity, i.cause or "", i.dedup_key) for i in rows]


def fail_one_discovery_cycle(reg, ids):
    """One planning pass, then run the discovery task through ALL of its retries to terminal failure."""
    autopilot.tick(ids["company_id"], ids["product_id"])
    for _ in range(5):
        drain(reg, ids["product_id"])
        with session_scope() as s:
            s.query(Task).filter(Task.next_attempt_at.isnot(None)).update({"next_attempt_at": datetime.now(timezone.utc)})


def test_repeated_discovery_failures_raise_exactly_one_non_critical_incident_with_the_real_cause():
    with acquisition_product(config={"discovery_cooldown_hours": 0.0}) as ids:
        reg = make_registry(Down())
        for _ in range(6):                                           # six planning cycles, each with a failing discovery task
            fail_one_discovery_cycle(reg, ids)
        failed = open_incidents(ids["product_id"], "discovery_failing")
        assert len(failed) == 1
        severity, cause, _ = failed[0]
        assert severity == "warning" and "HTTP 403" in cause and "Nominatim" in cause
        assert open_incidents(ids["product_id"], "agent_failed:prospecting:discover") == []
        assert not [i for i in open_incidents(ids["product_id"]) if i[0] == "critical"]               # nothing pages the founder as critical


def test_discovery_backs_off_exponentially_after_failures_instead_of_retrying_every_cycle():
    with acquisition_product(config={"discovery_cooldown_hours": 1.0}) as ids:
        clock = Clock()
        down = Down(); reg = make_registry(down)
        fail_one_discovery_cycle(reg, ids)
        first_calls = down.calls
        autopilot.tick(ids["company_id"], ids["product_id"])        # absorbs the failure into the cursor
        with session_scope() as s:
            cursor = s.get(AutopilotState, ids["product_id"]).cursor
            assert cursor["discovery_failures"] == 1 and "HTTP 403" in cursor["last_discovery_error"]
        clock.advance(hours=1.5)                                    # past the BASE cooldown but inside the doubled one (2h)
        autopilot.tick(ids["company_id"], ids["product_id"]); drain(reg, ids["product_id"])
        assert down.calls == first_calls                            # backed off: no new attempt yet
        clock.advance(hours=1.0)
        autopilot.tick(ids["company_id"], ids["product_id"]); drain(reg, ids["product_id"])
        assert down.calls > first_calls                             # now it retries


def test_recovery_resets_the_backoff_and_resolves_the_incident():
    with acquisition_product(config={"discovery_cooldown_hours": 0.0}) as ids:
        down = Down(); reg = make_registry(down)
        for _ in range(3):
            fail_one_discovery_cycle(reg, ids)
        assert len(open_incidents(ids["product_id"], "discovery_failing")) == 1
        reg = make_registry(FakeEnrichment([lead(1)]))               # the source comes back
        for _ in range(4):
            autopilot.tick(ids["company_id"], ids["product_id"]); drain(reg, ids["product_id"])
        assert open_incidents(ids["product_id"], "discovery_failing") == []
        with session_scope() as s:
            assert s.get(AutopilotState, ids["product_id"]).cursor.get("discovery_failures") == 0


def test_generic_agent_failures_are_deduplicated_while_open_but_distinct_failures_are_separate():
    with acquisition_product(enable=False) as ids:
        svc = IncidentService()
        a = svc.report(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=None, task_id=None, severity="critical", title="t", dedup_key="k1")
        b = svc.report(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=None, task_id=None, severity="critical", title="t", dedup_key="k1")
        c = svc.report(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=None, task_id=None, severity="critical", title="t", dedup_key="k2")
        assert a == b and a != c
        IncidentService.resolve(a)
        d = svc.report(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=None, task_id=None, severity="critical", title="t", dedup_key="k1")
        assert d != a                                                   # once resolved, a recurrence is a NEW incident
