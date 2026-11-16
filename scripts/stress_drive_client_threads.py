"""Stress the real httplib2 + googleapiclient objects from many threads (#579).

No Google, no network: a local TLS stub on 127.0.0.1 with a throwaway
self-signed certificate (needs the ``openssl`` command).

    uv run python scripts/stress_drive_client_threads.py shared 3 20
    uv run python scripts/stress_drive_client_threads.py perthread 8 20

``shared``    one service used by every thread (the pattern before #579);
``perthread`` one ``GDriveClient``, each thread gets its own service.

Prints one JSON line: requests answered correctly, answers that belonged to
another thread's request (``wrong_body``) and the errors by kind. A native
crash shows as the exit code (faulthandler prints the stack).
"""

import faulthandler
import json
import os
import random
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

faulthandler.enable()
mode, n, secs = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
certs = tempfile.mkdtemp(prefix="stress579-")
subprocess.run(
    ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
     "-keyout", os.path.join(certs, "key.pem"), "-out", os.path.join(certs, "cert.pem"),
     "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
    check=True, capture_output=True,
)  # fmt: skip
os.environ["HTTPLIB2_CA_CERTS"] = os.path.join(certs, "cert.pem")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        tag = self.path.split("pageToken=")[-1].split("&")[0]
        size = random.choice([10, 2000, 70000, 300000])
        body = json.dumps({"files": [{"id": tag, "name": "x" * size}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    request_queue_size = 128  # every thread connects at once
    daemon_threads = True


srv = Server(("127.0.0.1", 0), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(os.path.join(certs, "cert.pem"), os.path.join(certs, "key.pem"))
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
endpoint = f"https://localhost:{srv.server_address[1]}"

from google.oauth2.credentials import Credentials  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

import oncofiles.google_service as gs  # noqa: E402
from oncofiles.gdrive_client import GDriveClient  # noqa: E402


def build_local(api, version, creds):
    return build(
        api,
        version,
        credentials=creds,
        cache_discovery=False,
        static_discovery=True,
        client_options={"api_endpoint": endpoint},
    )


gs.build_service = build_local
creds = Credentials(
    token="tok", refresh_token="r", token_uri=endpoint, client_id="c", client_secret="s"
)
client = GDriveClient.__new__(GDriveClient)
client._creds = creds
shared = build_local("drive", "v3", creds)

stats = {"ok": 0, "wrong_body": 0}
errors: dict[str, int] = {}
lock = threading.Lock()
stop = time.monotonic() + secs


def work(i):
    k = 0
    while time.monotonic() < stop:
        k += 1
        tag = f"t{i}x{k}"
        svc = shared if mode == "shared" else client._service
        try:
            resp = svc.files().list(pageToken=tag, pageSize=10).execute()
            good = resp["files"][0]["id"] == tag
            with lock:
                stats["ok" if good else "wrong_body"] += 1
        except Exception as e:
            with lock:
                key = f"{type(e).__name__}: {str(e)[:70]}"
                errors[key] = errors.get(key, 0) + 1


ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
[t.start() for t in ts]
[t.join() for t in ts]
print(json.dumps({"mode": mode, "threads": n, **stats, "errors": errors}))
