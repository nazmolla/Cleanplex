"""Front door for the stream proxy (#184): Plex's TLS certificate and the nginx config in front of Plex.

Every client reaches Plex through nginx, which a firewall redirect (pf, nftables) or a
Docker port mapping puts on Plex's public port. nginx terminates TLS with Plex's own
plex.direct certificate, streams video bytes straight from Plex, and hands only playback
decisions, playlists, subtitles and progress reports to the Python proxy.

The certificate is Plex's cert-v2.p12, whose password is SHA-512 of "plex" + the server's
ProcessedMachineIdentifier (verified 2026-11-05). Plex renews it every few months, so it is
re-extracted when the file changes and nginx is reloaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger

logger = get_logger(__name__)

FRONT_PORT = 32480         # where the firewall redirect sends Plex's public port
PLAIN_INTERNAL_PORT = 32481  # nginx-internal: plain HTTP after the TLS/plain split
TLS_INTERNAL_PORT = 32482    # nginx-internal: TLS terminated with Plex's certificate
PROXY_PORT = 7981          # the Python stream proxy (decisions, playlists, subtitles)
PLEX_UPSTREAM = "127.0.0.1:32400"
CERT_CHECK_S = 6 * 3600    # Plex renews months ahead; a few checks a day is plenty


@dataclass
class PlexPaths:
    """Where this machine's Plex keeps the certificate and its machine id."""

    p12: Path
    machine_id: str


def _macos_paths(home: Path) -> PlexPaths | None:
    prefs = home / "Library/Preferences/com.plexapp.plexmediaserver.plist"
    p12 = home / "Library/Caches/PlexMediaServer/cert-v2.p12"
    try:
        with open(prefs, "rb") as fh:
            mid = plistlib.load(fh).get("ProcessedMachineIdentifier", "")
    except (OSError, plistlib.InvalidFileException):
        return None
    return PlexPaths(p12, mid) if mid else None


def _linux_paths(base: Path) -> PlexPaths | None:
    data = base / "Library/Application Support/Plex Media Server"
    try:
        xml = (data / "Preferences.xml").read_text(errors="ignore")
    except OSError:
        return None
    m = re.search(r'ProcessedMachineIdentifier="([^"]+)"', xml)
    return PlexPaths(data / "Cache/cert-v2.p12", m.group(1)) if m else None


def _windows_paths() -> PlexPaths | None:
    try:
        import winreg  # type: ignore[import-not-found]  # only exists on Windows
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as key:
            mid, _ = winreg.QueryValueEx(key, "ProcessedMachineIdentifier")
    except OSError:
        return None
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Plex Media Server"
    return PlexPaths(local / "Cache/cert-v2.p12", mid)


def find_plex_paths() -> PlexPaths | None:
    """Locate Plex's certificate and machine id for the account Cleanplex runs as.

    CLEANPLEX_PLEX_DATA overrides the Plex data folder (Linux layout, e.g. a Docker volume);
    CLEANPLEX_PLEX_MACHINE_ID overrides the machine id.
    """
    override = os.environ.get("CLEANPLEX_PLEX_DATA")
    found: PlexPaths | None
    if override:
        found = _linux_paths(Path(override))
    elif platform.system() == "Darwin":
        found = _macos_paths(Path.home())
    elif platform.system() == "Windows":
        found = _windows_paths()
    else:
        found = _linux_paths(Path("/var/lib/plexmediaserver"))
    mid = os.environ.get("CLEANPLEX_PLEX_MACHINE_ID")
    if found and mid:
        found.machine_id = mid
    return found


def p12_password(machine_id: str) -> bytes:
    return hashlib.sha512(f"plex{machine_id}".encode()).hexdigest().encode()


def extract_certificate(paths: PlexPaths, out_dir: Path) -> tuple[Path, Path]:
    """Write Plex's certificate (with chain) and key as PEM files readable only by this account.

    Returns (cert_path, key_path). Raises ValueError if the password does not open the file.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    key, cert, chain = pkcs12.load_key_and_certificates(paths.p12.read_bytes(), p12_password(paths.machine_id))
    if key is None or cert is None:
        raise ValueError("Plex certificate has no key or certificate")
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    cert_path, key_path = out_dir / "plex-cert.pem", out_dir / "plex-key.pem"
    pem = cert.public_bytes(serialization.Encoding.PEM) + b"".join(
        c.public_bytes(serialization.Encoding.PEM) for c in (chain or [])
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    for path, data in ((cert_path, pem), (key_path, key_pem)):
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        tmp.replace(path)  # atomic: nginx never reads a half-written file
    return cert_path, key_path


# Private, loopback and link-local addresses: these viewers are on the LAN, and Plex treats a
# request with no X-Forwarded-For from 127.0.0.1 as local. A private address in the header is
# ignored by Plex and the viewer marked remote (Gate 2, 2026-11-05), so it is never sent.
_LAN_PATTERN = (
    r"~^(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|::1$|fe80:|fc|fd)"
)


def render_nginx_conf(cert_path: Path, key_path: Path, *, front_port: int = FRONT_PORT,
                      proxy_port: int = PROXY_PORT, plex_upstream: str = PLEX_UPSTREAM,
                      pid_path: str = "/tmp/cleanplex-nginx.pid", log_dir: str = "/tmp") -> str:
    """The nginx config for the front door. Self-contained: run with `nginx -c <file>`."""
    return f"""# Generated by Cleanplex (cleanplex/frontdoor.py). Do not edit; it is rewritten.
worker_processes auto;
pid {pid_path};
error_log {log_dir}/cleanplex-nginx-error.log warn;
events {{ worker_connections 4096; }}

# One public port carries TLS and plain HTTP, as Plex's own port does: peek at the first
# bytes and pass TLS and plain to separate internal servers, keeping the client address
# with the PROXY protocol.
stream {{
    map $ssl_preread_protocol $cleanplex_backend {{
        ""      127.0.0.1:{PLAIN_INTERNAL_PORT};
        default 127.0.0.1:{TLS_INTERNAL_PORT};
    }}
    server {{
        listen {front_port};
        listen [::]:{front_port};
        ssl_preread on;
        proxy_protocol on;
        proxy_pass $cleanplex_backend;
    }}
}}

http {{
    access_log off;
    map $remote_addr $cleanplex_xff {{
        {_LAN_PATTERN} "";
        default $remote_addr;
    }}
    map $http_upgrade $cleanplex_connection {{
        default upgrade;
        ""      "";
    }}
    upstream cleanplex_plex {{ server {plex_upstream}; keepalive 32; }}
    upstream cleanplex_proxy {{ server 127.0.0.1:{proxy_port}; keepalive 16; }}

    server {{
        listen 127.0.0.1:{PLAIN_INTERNAL_PORT} proxy_protocol;
        listen 127.0.0.1:{TLS_INTERNAL_PORT} ssl proxy_protocol;
        ssl_certificate {cert_path};
        ssl_certificate_key {key_path};
        set_real_ip_from 127.0.0.1;
        real_ip_header proxy_protocol;

        client_max_body_size 200m;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        # $http_host keeps the port; $host drops it, and Plex builds redirects from this header
        # (a client on :32400 was sent to port 80 and could not connect, 2026-11-05).
        proxy_set_header Host $http_host;
        # Replaces anything the client sent: a remote client must not claim a LAN address.
        proxy_set_header X-Forwarded-For $cleanplex_xff;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $cleanplex_connection;

        # Playback decisions, starts, playlists, subtitles and progress: the Python proxy.
        location ~ ^/video/:/transcode/universal/(decision|start|subtitles) {{ proxy_pass http://cleanplex_proxy; }}
        location ~ ^/video/:/transcode/universal/session/[^/]+/base/index\\.m3u8$ {{ proxy_pass http://cleanplex_proxy; }}
        location ~ ^/:/(timeline|progress) {{ proxy_pass http://cleanplex_proxy; }}

        # Video segments: bytes from Plex; Python only notes the request (played cuts).
        location ~ ^/video/:/transcode/universal/session/[^/]+/base/\\d+\\.ts$ {{
            auth_request /_cleanplex/segment;
            proxy_pass http://cleanplex_plex;
        }}
        # Original files (direct play, downloads): Python says yes or no, bytes from Plex.
        location ~ ^/library/parts/ {{
            auth_request /_cleanplex/authorize;
            proxy_pass http://cleanplex_plex;
        }}
        location = /_cleanplex/authorize {{
            internal;
            proxy_pass http://cleanplex_proxy/_proxy/authorize;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_set_header X-Original-URI $request_uri;
        }}
        location = /_cleanplex/segment {{
            internal;
            proxy_pass http://cleanplex_proxy/_proxy/segment;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_set_header X-Original-URI $request_uri;
        }}

        location / {{ proxy_pass http://cleanplex_plex; }}
    }}
}}
"""


async def _reload_nginx(conf: Path) -> None:
    nginx = os.environ.get("CLEANPLEX_NGINX", "nginx")
    proc = await asyncio.create_subprocess_exec(nginx, "-e", str(conf.parent / "cleanplex-nginx-error.log"),
                                                "-c", str(conf), "-s", "reload",
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("nginx reload failed: %s", err.decode(errors="ignore").strip())


async def frontdoor_cert_loop(data_dir: Path) -> None:
    """Keep the front door's certificate in step with Plex's and reload nginx when it changes.

    Runs only when the front door is installed (its nginx config exists in the data dir).
    """
    conf = data_dir / "frontdoor" / "nginx.conf"
    last_mtime = None
    while True:
        try:
            if conf.exists():
                paths = find_plex_paths()
                if paths and paths.p12.exists():
                    mtime = paths.p12.stat().st_mtime
                    if last_mtime is not None and mtime != last_mtime:
                        await asyncio.to_thread(extract_certificate, paths, data_dir / "tls")
                        await _reload_nginx(conf)
                        logger.info("Plex renewed its certificate; front door updated")
                    last_mtime = mtime
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Front door certificate check failed: %s", exc)
        await asyncio.sleep(CERT_CHECK_S)


def main() -> None:
    """`python -m cleanplex.frontdoor <data_dir>`: extract the certificate and write the nginx config.

    Used by the install scripts; prints the config path.
    """
    import sys

    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".cleanplex")
    paths = find_plex_paths()
    if not paths or not paths.p12.exists():
        raise SystemExit("Plex certificate not found; set CLEANPLEX_PLEX_DATA / CLEANPLEX_PLEX_MACHINE_ID")
    cert, key = extract_certificate(paths, data_dir / "tls")
    conf_dir = data_dir / "frontdoor"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf = conf_dir / "nginx.conf"
    conf.write_text(render_nginx_conf(cert, key, pid_path=str(conf_dir / "nginx.pid"), log_dir=str(conf_dir)))
    print(conf)


if __name__ == "__main__":
    main()
