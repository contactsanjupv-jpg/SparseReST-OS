"""
TLS client configuration for every mailbox connection (SMTP outreach, SMTP report mail, IMAP).

The stock `smtplib.starttls()` and `imaplib.IMAP4_SSL()` build an UNVERIFIED context: they accept any certificate, so a
network attacker could present their own and capture the mailbox password. Everything here uses
`ssl.create_default_context()`: certificate chain AND hostname are verified against the system trust store.

Two explicit, narrow escape hatches:
  * TLS_CA_FILE            -- trust an additional CA bundle (a private mail server, or a local test server).
  * ALLOW_INSECURE_TLS     -- turn verification OFF. Development only: refused outright when ENVIRONMENT=production,
                              logged at WARNING on every use, and flagged by scripts/check_production.py.
"""
from __future__ import annotations

import logging
import ssl
from typing import Optional

from packages.core.config import Settings, get_settings
from packages.core.log import get_logger, log

_log = get_logger("tls")


class TLSConfigError(Exception):
    pass


def client_context(settings: Optional[Settings] = None) -> ssl.SSLContext:
    settings = settings or get_settings()
    if settings.allow_insecure_tls:
        if settings.is_production:
            raise TLSConfigError("ALLOW_INSECURE_TLS is not permitted when ENVIRONMENT=production")
        log(_log, logging.WARNING, "TLS certificate verification is DISABLED (ALLOW_INSECURE_TLS) -- development only")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        return ssl.create_default_context(cafile=settings.tls_ca_file or None)
    except (OSError, ssl.SSLError) as exc:
        raise TLSConfigError(f"TLS_CA_FILE '{settings.tls_ca_file}' could not be loaded: {exc}") from exc
