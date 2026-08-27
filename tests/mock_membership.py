#!/usr/bin/env python3
import argparse, hashlib, json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

state = {"services": {}, "deployments": 0}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def body(self):
        return self.rfile.read(int(self.headers.get("content-length", "0")))
    def send_json(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        if self.path == "/v1/guest-sessions":
            body = json.loads(self.body()); self.send_json(201, {"project_id": body["project_id"], "user_id": "guest", "plan": "guest-preview"}); return
        if self.path.startswith("/v1/artifacts"):
            data = self.body(); digest = hashlib.sha256(data).hexdigest(); self.send_json(201, {"id": "art_"+digest[:8], "digest": "sha256:"+digest, "kind": "mock"}); return
        if self.path == "/v1/services":
            body = json.loads(self.body()); service = {"id": "svc_test", "name": body["name"], "status": "stopped", "artifact_id": body["artifact_id"], "active_deployment_id": "", "hostname": "127.0.0.1:%d" % self.server.server_port}
            state["services"][service["id"]] = service; self.send_json(201, service); return
        if self.path.endswith("/deploy"):
            self.body(); state["deployments"] += 1; deployment = "dep_%d" % state["deployments"]
            service = state["services"]["svc_test"]; service.update(status="running", active_deployment_id=deployment)
            self.send_json(200, {"id": deployment, "service_id": "svc_test", "status": "running"}); return
        if self.path.endswith("/stop"):
            self.body(); state["services"]["svc_test"]["status"] = "stopped"; self.send_json(200, {"status": "stopped"}); return
        self.send_json(404, {"error": "not_found"})
    def do_GET(self):
        if self.path == "/v1/services": self.send_json(200, {"items": list(state["services"].values())}); return
        if self.path == "/":
            dep = state["services"].get("svc_test", {}).get("active_deployment_id", "")
            data = b"<!doctype html><title>Broz mock</title>"
            self.send_response(200); self.send_header("X-Mim-Deployment", dep); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_json(404, {"error": "not_found"})
    def do_DELETE(self):
        if self.path == "/v1/services/svc_test": state["services"].pop("svc_test", None); self.send_response(204); self.end_headers(); return
        self.send_json(404, {"error": "not_found"})

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--port-file", required=True); args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(args.port_file, "w") as f: f.write(str(server.server_port))
    server.serve_forever()
