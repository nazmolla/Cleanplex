"""Stand-in Plex server for the Docker smoke test: answers what Cleanplex's setup touches."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PREFS = {"RelayEnabled": "1", "DlnaEnabled": "1", "TranscodeCountLimit": "0"}
LOG = []


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json"):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/identity":
            self._send('<MediaContainer machineIdentifier="smoke-mid"/>', "application/xml")
        elif path == "/:/prefs":
            self._send(json.dumps({"MediaContainer": {"Setting": [{"id": k, "value": v} for k, v in PREFS.items()]}}))
        elif path == "/_stub/prefs-log":
            self._send(json.dumps(LOG))
        else:
            self._send("<MediaContainer/>", "application/xml")

    def do_PUT(self):
        url = urlparse(self.path)
        if url.path == "/:/prefs":
            changes = {k: v[0] for k, v in parse_qs(url.query).items() if k != "X-Plex-Token"}
            PREFS.update(changes)
            LOG.append(changes)
        self._send("{}")

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 32400), H).serve_forever()
