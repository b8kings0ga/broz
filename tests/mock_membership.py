#!/usr/bin/env python3
import argparse, base64, hashlib, io, json, os, tarfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

state = {"services": {}, "deployments": 0, "revisions": 0, "blobs": {}, "activation_mismatch_once": True}

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
            data = self.body()
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    names = [os.path.normpath(item.name) for item in archive.getmembers()]
                if "." in names or any(name.startswith("../") or name.startswith("/") for name in names): raise ValueError("unsafe archive")
            except (tarfile.TarError, ValueError): self.send_json(400, {"error": "invalid_artifact"}); return
            digest = hashlib.sha256(data).hexdigest(); self.send_json(201, {"id": "art_"+digest[:8], "digest": "sha256:"+digest, "kind": "mock"}); return
        if self.path == "/v1/services":
            body = json.loads(self.body()); service = {"id": "svc_test", "name": body["name"], "status": "stopped", "artifact_id": body["artifact_id"], "active_deployment_id": "", "hostname": "127.0.0.1:%d" % self.server.server_port}
            state["services"][service["id"]] = service; self.send_json(201, service); return
        if self.path.endswith("/revisions"):
            body = json.loads(self.body()); state["revisions"] += 1; revision = "rev_%d" % state["revisions"]
            missing = [{"transport_sha256": blob["transport_sha256"], "upload_url": self.path + "/" + revision + "/blobs/" + blob["transport_sha256"].removeprefix("sha256:")} for blob in body["blobs"]]
            self.send_json(201, {"revision_id": revision, "state": "uploading", "missing_blobs": missing}); return
        if self.path.endswith("/prepare"):
            self.body(); revision = self.path.split("/")[-2]; self.send_json(200, {"revision_id": revision, "state": "prepared", "prepared_receipt": "receipt_"+revision, "prepared_at": "2026-08-30T00:00:00Z", "metrics_ms": {"cas_materialize": 1}}); return
        if self.path.endswith("/activate"):
            self.body()
            if state["activation_mismatch_once"]:
                state["activation_mismatch_once"] = False
                self.send_json(409, {"error": "prepared_receipt_mismatch"}); return
            revision = self.path.split("/")[-2]
            request_id = self.headers.get("Idempotency-Key", "")
            identity = "\0".join(("mock-user", "svc_test", revision, request_id)).encode()
            deployment = "dep_" + base64.urlsafe_b64encode(hashlib.sha256(identity).digest()[:18]).decode().rstrip("=")
            state["deployments"] += 1
            service = state["services"]["svc_test"]; service.update(status="running", active_deployment_id=deployment)
            self.send_json(200, {"status": "accessible", "mode": "hot", "service_id": "svc_test", "revision_id": revision, "deployment_id": deployment, "public_url": "http://"+service["hostname"], "metrics_ms": {"child_spawn": 2}}); return
        if self.path.endswith("/deploy"):
            self.body(); state["deployments"] += 1; deployment = "dep_%d" % state["deployments"]
            service = state["services"]["svc_test"]; service.update(status="running", active_deployment_id=deployment)
            self.send_json(200, {"id": deployment, "service_id": "svc_test", "status": "running"}); return
        if self.path.endswith("/stop"):
            self.body(); state["services"]["svc_test"]["status"] = "stopped"; self.send_json(200, {"status": "stopped"}); return
        self.send_json(404, {"error": "not_found"})
    def do_GET(self):
        if self.path == "/v1/services": self.send_json(200, {"items": list(state["services"].values())}); return
        if self.path == "/v1/services/svc_test" and "svc_test" in state["services"]: self.send_json(200, state["services"]["svc_test"]); return
        if urlsplit(self.path).path == "/":
            dep = state["services"].get("svc_test", {}).get("active_deployment_id", "")
            data = b"<!doctype html><title>Broz mock</title>"
            # HTTP/2 gateways commonly normalize field names to lowercase. Keep
            # the mock that way so macOS/BSD awk compatibility is exercised.
            self.send_response(200); self.send_header("x-mim-deployment", dep); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_json(404, {"error": "not_found"})
    def do_PUT(self):
        if self.path.endswith("/fast-mode"):
            self.body(); self.send_json(200, {"enabled": True, "billing": "running_per_minute", "slot": {"id": "slot_test", "state": "ready"}}); return
        if "/blobs/" in self.path:
            state["blobs"][self.path.rsplit("/", 1)[-1]] = self.body(); self.send_json(201, {"stored": True}); return
        self.send_json(404, {"error": "not_found"})
    def do_DELETE(self):
        if self.path == "/v1/services/svc_test": state["services"].pop("svc_test", None); self.send_response(204); self.end_headers(); return
        self.send_json(404, {"error": "not_found"})

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--port-file", required=True); args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(args.port_file, "w") as f: f.write(str(server.server_port))
    server.serve_forever()
