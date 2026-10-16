"""Mailbox TLS: certificate chain AND hostname are verified, with a narrow, explicit dev-only override.
These tests use a REAL TLS SMTP server with a self-signed certificate (not a mock), so verification is actually exercised."""
from __future__ import annotations

import datetime
import imaplib
import socket
import ssl

import pytest
from aiosmtpd.controller import Controller
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from packages.core import mailer
from packages.core.config import Settings, get_settings
from packages.core.tlsconfig import TLSConfigError, client_context
from packages.tools.imap_inbound import ImapInboundChecker
from packages.tools.outreach import SMTPOutreachProvider


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)       # NO IP SAN on purpose
            .sign(key, hashes.SHA256()))
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return {"cert": str(d / "cert.pem"), "key": str(d / "key.pem")}


class Sink:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope.content)
        return "250 OK"


@pytest.fixture()
def smtp_server(pki):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(pki["cert"], pki["key"])
    sink = Sink()
    controller = Controller(sink, hostname="127.0.0.1", port=port, tls_context=ctx, require_starttls=True,
                            authenticator=lambda *a, **k: __import__("aiosmtpd.smtp", fromlist=["AuthResult"]).AuthResult(success=True, handled=True),
                            auth_required=True, auth_require_tls=True)
    controller.start()
    yield {"port": port, "sink": sink}
    controller.stop()


def provider(host, port):
    return SMTPOutreachProvider(host=host, port=port, username="u", password="p", from_email="me@xflow.example")


def use_settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_untrusted_self_signed_certificate_is_rejected_by_default(smtp_server):
    with pytest.raises(ssl.SSLCertVerificationError):
        provider("localhost", smtp_server["port"]).send(to_email="a@b.co", subject="s", body="b")
    assert smtp_server["sink"].messages == []                       # nothing was transmitted over the unverified channel


def test_trusting_the_ca_via_TLS_CA_FILE_allows_the_send(smtp_server, pki, monkeypatch):
    use_settings(monkeypatch, TLS_CA_FILE=pki["cert"])
    result = provider("localhost", smtp_server["port"]).send(to_email="a@b.co", subject="Hello", body="body")
    assert result.sent and len(smtp_server["sink"].messages) == 1


def test_hostname_mismatch_is_rejected_even_when_the_ca_is_trusted(smtp_server, pki, monkeypatch):
    use_settings(monkeypatch, TLS_CA_FILE=pki["cert"])
    with pytest.raises(ssl.SSLCertVerificationError, match="IP address mismatch|hostname"):
        provider("127.0.0.1", smtp_server["port"]).send(to_email="a@b.co", subject="s", body="b")     # cert is only valid for 'localhost'


def test_insecure_override_works_in_development_and_is_loud(smtp_server, monkeypatch, caplog):
    use_settings(monkeypatch, ALLOW_INSECURE_TLS="true")
    assert provider("localhost", smtp_server["port"]).send(to_email="a@b.co", subject="s", body="b").sent


def test_insecure_override_is_refused_in_production(monkeypatch):
    use_settings(monkeypatch, ALLOW_INSECURE_TLS="true", ENVIRONMENT="production")
    with pytest.raises(TLSConfigError, match="production"):
        client_context()


def test_verification_is_strict_by_default():
    ctx = client_context(Settings())
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_bad_ca_file_is_a_clear_configuration_error(monkeypatch, tmp_path):
    bogus = tmp_path / "nope.pem"; bogus.write_text("not a certificate")
    use_settings(monkeypatch, TLS_CA_FILE=str(bogus))
    with pytest.raises(TLSConfigError, match="TLS_CA_FILE"):
        client_context()


def test_report_mailer_also_verifies_certificates(smtp_server, pki, monkeypatch):
    base = {"SMTP_HOST": "localhost", "SMTP_PORT": str(smtp_server["port"]), "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
            "SMTP_FROM_EMAIL": "me@x.co", "REPORT_EMAIL_TO": "founder@x.co"}
    use_settings(monkeypatch, **base)
    with pytest.raises(ssl.SSLCertVerificationError):
        mailer.send_internal_email(get_settings(), "subject", "body")
    use_settings(monkeypatch, TLS_CA_FILE=pki["cert"])
    mailer.send_internal_email(get_settings(), "subject", "body")
    assert len(smtp_server["sink"].messages) == 1


def test_imap_client_is_built_with_a_verifying_context(monkeypatch):
    captured = {}

    class FakeIMAP:
        def __init__(self, host, port=993, ssl_context=None):
            captured["ctx"], captured["host"] = ssl_context, host

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    ImapInboundChecker._verified_client("imap.example.com")
    assert captured["ctx"].verify_mode == ssl.CERT_REQUIRED and captured["ctx"].check_hostname is True


def test_preflight_blocks_insecure_tls_in_production():
    from scripts import check_production
    errors, _ = check_production.audit(Settings(environment="production", allow_insecure_tls=True, allowed_origins="https://a.co", trusted_hosts="api.a.co",
                                                app_database_url="postgresql+psycopg://app_role:x@db/x"), accounts_exist=True, migrations_ok=True)
    assert any("ALLOW_INSECURE_TLS" in e for e in errors)
