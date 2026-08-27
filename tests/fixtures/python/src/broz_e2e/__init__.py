import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_ID = os.environ.get("MIM_SERVICE_ID", "local")
DEPLOYMENT_ID = os.environ.get("MIM_DEPLOYMENT_ID", "local")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        if self.path == "/":
            body = f"<!doctype html><title>Broz Python</title><h1>Broz Python is live</h1><p>{SERVICE_ID}</p><p>{DEPLOYMENT_ID}</p>".encode()
            self.send_response(200); self.send_header("content-type", "text/html; charset=utf-8")
        elif self.path in ("/healthz", "/api/status"):
            body = json.dumps({"ok": True, "service_id": SERVICE_ID, "deployment_id": DEPLOYMENT_ID, "runtime": "python"}).encode()
            self.send_response(200); self.send_header("content-type", "application/json")
        else:
            body = b"not found"; self.send_response(404)
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

def main():
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    server.serve_forever()
