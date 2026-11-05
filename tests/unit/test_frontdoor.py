"""Front door: Plex certificate extraction and the generated nginx config (#184)."""

import datetime
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from cleanplex import frontdoor


def _make_p12(path: Path, machine_id: str) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.abc.plex.direct")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    path.write_bytes(pkcs12.serialize_key_and_certificates(
        b"plex", key, cert, None,
        serialization.BestAvailableEncryption(frontdoor.p12_password(machine_id))))


def test_extracts_plex_certificate_with_the_machine_id_password(tmp_path):
    p12 = tmp_path / "cert-v2.p12"
    _make_p12(p12, "MID123")
    cert, key = frontdoor.extract_certificate(frontdoor.PlexPaths(p12, "MID123"), tmp_path / "tls")
    assert b"BEGIN CERTIFICATE" in cert.read_bytes() and b"PRIVATE KEY" in key.read_bytes()
    assert oct(os.stat(key).st_mode & 0o777) == "0o600"


def test_a_wrong_machine_id_cannot_open_the_certificate(tmp_path):
    p12 = tmp_path / "cert-v2.p12"
    _make_p12(p12, "MID123")
    try:
        frontdoor.extract_certificate(frontdoor.PlexPaths(p12, "WRONG"), tmp_path / "tls")
    except ValueError:
        return
    raise AssertionError("wrong password accepted")


def test_linux_and_docker_layout_is_found_from_preferences_xml(tmp_path, monkeypatch):
    data = tmp_path / "Library/Application Support/Plex Media Server"
    data.mkdir(parents=True)
    (data / "Preferences.xml").write_text('<Preferences ProcessedMachineIdentifier="abc123" />')
    monkeypatch.setenv("CLEANPLEX_PLEX_DATA", str(tmp_path))
    found = frontdoor.find_plex_paths()
    assert found.machine_id == "abc123" and found.p12 == data / "Cache/cert-v2.p12"


def test_nginx_config_keeps_the_safety_rules():
    conf = frontdoor.render_nginx_conf(Path("/c.pem"), Path("/k.pem"))
    # Bytes never go through Python: segments and original files are proxied to Plex.
    assert "auth_request /_cleanplex/segment;\n            proxy_pass http://cleanplex_plex;" in conf
    assert "auth_request /_cleanplex/authorize;\n            proxy_pass http://cleanplex_plex;" in conf
    # A client cannot smuggle its own X-Forwarded-For, and LAN viewers send none.
    assert "proxy_set_header X-Forwarded-For $cleanplex_xff;" in conf
    assert '192\\.168\\.' in conf and '"";' in conf
    # One port for TLS and plain, client address kept.
    assert "ssl_preread on;" in conf and "real_ip_header proxy_protocol;" in conf
    assert "listen [::]:32480;" in conf
    # The port must survive in Host: Plex builds redirect URLs from it.
    assert "proxy_set_header Host $http_host;" in conf and "Host $host;" not in conf
