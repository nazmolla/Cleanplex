"""Write a fake Plex config folder (machine id, token, certificate) for the Docker smoke test."""
import datetime
import hashlib
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

MID, TOKEN = "smokemachineid", "smoke-token"
root = Path(sys.argv[1])
data = root / "Library/Application Support/Plex Media Server"
(data / "Cache").mkdir(parents=True, exist_ok=True)
(data / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{MID}" PlexOnlineToken="{TOKEN}"/>')
key = ec.generate_private_key(ec.SECP256R1())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.smoke.plex.direct")])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256()))
password = hashlib.sha512(f"plex{MID}".encode()).hexdigest().encode()
(data / "Cache/cert-v2.p12").write_bytes(pkcs12.serialize_key_and_certificates(
    b"plex", key, cert, None, serialization.BestAvailableEncryption(password)))
