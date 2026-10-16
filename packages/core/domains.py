"""
DNS-based domain checks used by the qualification gate and the sender-readiness check.
Free (plain DNS via dnspython), no accounts. Injectable so tests never touch the network.

Result semantics matter for safety: 'invalid' means DNS DEFINITIVELY says the domain cannot receive
mail (NXDOMAIN / no MX and no A record); 'unknown' means the lookup itself failed (timeout, no
network). The gate treats 'unknown' as NOT qualified rather than guessing -- never send on an
unverified assumption.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from packages.core.config import get_settings


class DomainChecker(ABC):
    @abstractmethod
    def mx(self, domain: str) -> tuple[str, str]:
        """('valid' | 'invalid' | 'unknown', human-readable detail)"""

    @abstractmethod
    def txt(self, name: str) -> Optional[list[str]]:
        """TXT records for `name`, [] if none exist, None if the lookup failed."""


class DnsDomainChecker(DomainChecker):
    def __init__(self, lifetime: float = 5.0):
        self._lifetime = lifetime
        self._cache: dict[str, tuple[str, str]] = {}

    def _resolver(self):
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.lifetime = resolver.timeout = self._lifetime
        return resolver

    def mx(self, domain: str) -> tuple[str, str]:
        domain = domain.strip().lower().rstrip(".")
        if domain in self._cache:
            return self._cache[domain]
        try:
            import dns.exception
            import dns.resolver
        except ImportError:
            return "unknown", "dnspython is not installed"
        resolver = self._resolver()
        try:
            answers = resolver.resolve(domain, "MX")
            result = ("valid", f"{len(answers)} MX record(s)") if len(answers) else ("invalid", "empty MX set")
        except dns.resolver.NXDOMAIN:
            result = ("invalid", "domain does not exist")
        except dns.resolver.NoAnswer:
            # RFC 5321: no MX falls back to the A/AAAA record. Accept that, reject if there is none.
            try:
                resolver.resolve(domain, "A")
                result = ("valid", "no MX but has an A record (implicit MX)")
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                result = ("invalid", "no MX and no A record")
            except Exception as exc:
                return "unknown", f"A lookup failed: {exc.__class__.__name__}"
        except dns.resolver.NoNameservers:
            return "unknown", "no nameserver answered"
        except dns.exception.Timeout:
            return "unknown", "DNS lookup timed out"
        except Exception as exc:
            return "unknown", f"DNS lookup failed: {exc.__class__.__name__}"
        self._cache[domain] = result
        return result

    def txt(self, name: str) -> Optional[list[str]]:
        try:
            import dns.resolver
            answers = self._resolver().resolve(name, "TXT")
            return ["".join(part.decode() for part in r.strings) for r in answers]
        except Exception as exc:
            if exc.__class__.__name__ in ("NXDOMAIN", "NoAnswer"):
                return []
            return None


class PermissiveDomainChecker(DomainChecker):
    """Used only when DNS_VALIDATION=false (offline development, the test suite)."""

    def mx(self, domain: str) -> tuple[str, str]:
        return "valid", "DNS validation disabled"

    def txt(self, name: str) -> Optional[list[str]]:
        return None


class StaticDomainChecker(DomainChecker):
    """Deterministic checker for tests: {'domain': 'valid'|'invalid'|'unknown'}; unlisted => valid."""

    def __init__(self, mx: Optional[dict] = None, txt: Optional[dict] = None):
        self._mx, self._txt = mx or {}, txt or {}

    def mx(self, domain: str) -> tuple[str, str]:
        status = self._mx.get(domain.lower(), "valid")
        return status, f"static:{status}"

    def txt(self, name: str) -> Optional[list[str]]:
        return self._txt.get(name.lower(), [])


def get_domain_checker() -> DomainChecker:
    return DnsDomainChecker() if get_settings().dns_validation else PermissiveDomainChecker()


def check_sender_authentication(from_email: str, checker: DomainChecker) -> dict:
    """SPF / DMARC presence for the sending domain. Missing authentication is the single most common reason
    cold mail lands in spam, so the autopilot refuses to start without SPF. (DKIM selectors are provider-specific
    and cannot be discovered by DNS alone -- the runbook has the one-line check.)"""
    domain = from_email.split("@")[-1].lower()
    spf = checker.txt(domain)
    dmarc = checker.txt(f"_dmarc.{domain}")
    return {
        "domain": domain,
        "spf": None if spf is None else any(r.lower().startswith("v=spf1") for r in spf),
        "dmarc": None if dmarc is None else any(r.lower().startswith("v=dmarc1") for r in dmarc),
    }
