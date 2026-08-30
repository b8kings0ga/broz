#!/usr/bin/env python3
import importlib.util
import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skills" / "broz-deploy" / "scripts" / "broz.py"
SPEC = importlib.util.spec_from_file_location("broz_helper", SCRIPT)
broz = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(broz)


class FakeAPI:
    def __init__(self, name, delay, calls):
        self.name, self.delay, self.calls = name, delay, calls

    def activate_stream(self, path, body, request_id, on_started, continue_existing=False):
        self.calls.append(self.name)
        time.sleep(self.delay)
        deployment = {"deployment_id": "dep_exact", "status": "activated"}
        on_started(deployment)
        return deployment


class ActivationHedgeTests(unittest.TestCase):
    def setUp(self):
        self.previous = broz.ACTIVATION_HEDGE_DELAY
        broz.ACTIVATION_HEDGE_DELAY = 0.03

    def tearDown(self):
        broz.ACTIVATION_HEDGE_DELAY = self.previous

    def test_fast_primary_suppresses_duplicate_activation(self):
        calls = []
        project = type("Project", (), {})()
        project.api = FakeAPI("primary", 0.005, calls)
        project.activation_api = FakeAPI("hedge", 0, calls)
        result, _, _ = broz.race_activation(project, "/activate", {}, "key", lambda _: None)
        self.assertEqual(result["deployment_id"], "dep_exact")
        self.assertEqual(calls, ["primary"])

    def test_delayed_primary_starts_hedge(self):
        calls = []
        project = type("Project", (), {})()
        project.api = FakeAPI("primary", 0.08, calls)
        project.activation_api = FakeAPI("hedge", 0, calls)
        result, _, _ = broz.race_activation(project, "/activate", {}, "key", lambda _: None)
        self.assertEqual(result["deployment_id"], "dep_exact")
        self.assertEqual(calls[:2], ["primary", "hedge"])


class PublicVerificationBoundaryTests(unittest.TestCase):
    def test_failure_after_activation_never_cold_falls_back(self):
        original = {name: getattr(broz, name) for name in (
            "load_json", "prepare_snapshot", "fast_deployment_id", "race_activation",
            "public_lane_delays", "wait_page", "cold_deploy",
        )}
        cold_calls = []
        try:
            broz.load_json = lambda *_args, **_kwargs: {
                "manifest_digest": "manifest", "prepared_receipt": "receipt",
                "service_id": "svc", "revision_id": "rev",
            }
            broz.prepare_snapshot = lambda *_args, **_kwargs: {"manifest_digest": "manifest"}
            broz.fast_deployment_id = lambda *_args: "dep_exact"

            def activate(_project, _path, _body, _request_id, on_started):
                result = {
                    "status": "activated", "deployment_id": "dep_exact",
                    "expected_deployment_header": "dep_exact", "public_url": "https://example.invalid",
                }
                on_started(result)
                return result, 1, 2

            broz.race_activation = activate
            broz.public_lane_delays = lambda _project: (0.0,)
            broz.wait_page = lambda *_args, **_kwargs: (_ for _ in ()).throw(broz.BrozError("public mismatch"))
            broz.cold_deploy = lambda *_args, **_kwargs: cold_calls.append(True)
            project = type("Project", (), {
                "runtime": "bun", "cache_path": Path("unused"), "user_id": "user",
                "state": {}, "active_deployment_id": "", "lock": lambda self: contextlib.nullcontext(),
            })()
            with self.assertRaises(broz.BrozError) as raised:
                broz.hot_deploy(project, "cold")
            self.assertEqual(raised.exception.code, "public_verification_failed_after_activation")
            self.assertEqual(cold_calls, [])
        finally:
            for name, value in original.items():
                setattr(broz, name, value)


class PrepareRetryTests(unittest.TestCase):
    def test_runtime_bridge_convergence_stays_in_one_prepare_attempt(self):
        original_sleep = broz.time.sleep
        failures = []
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls < 4:
                error = broz.BrozError("warming", status=503, code="membership_runtime_unavailable")
                failures.append(error)
                raise error
            return "prepared"

        try:
            broz.time.sleep = lambda _delay: None
            self.assertEqual(broz.retry_fast_prepare("upload", operation), "prepared")
        finally:
            broz.time.sleep = original_sleep
        self.assertEqual(calls, 4)
        self.assertTrue(all(error.stage == "upload" for error in failures))


class ConnectionWarmupTests(unittest.TestCase):
    def test_membership_warmup_uses_liveness_not_readiness(self):
        calls = []

        class WarmAPI:
            def request(self, method, path, timeout):
                calls.append((method, path, timeout))
                return {}, 200, {}

        project = type("Project", (), {"api": WarmAPI(), "activation_api": WarmAPI()})()
        broz.warm_membership_connections(project)
        self.assertEqual(calls, [("GET", "/healthz", 6), ("GET", "/healthz", 6)])

    def test_independent_warmup_groups_run_in_parallel(self):
        originals = {name: getattr(broz, name) for name in (
            "warm_membership_connections", "warm_public_connections", "warm_node_activation_connection",
        )}
        calls = []
        try:
            for name in originals:
                setattr(broz, name, lambda *_args, marker=name: (time.sleep(0.04), calls.append(marker)))
            started = time.monotonic()
            broz.warm_deploy_connections(object(), {})
            elapsed = time.monotonic() - started
        finally:
            for name, value in originals.items():
                setattr(broz, name, value)
        self.assertEqual(set(calls), set(originals))
        self.assertLess(elapsed, 0.09)


class ActivationTicketFallbackTests(unittest.TestCase):
    def test_transient_ticket_failure_keeps_prepared_central_path(self):
        class UnavailableAPI:
            def request(self, *_args, **_kwargs):
                raise broz.BrozError("warming", status=503, code="membership_runtime_unavailable")

        project = type("Project", (), {"api": UnavailableAPI(), "user_id": "user", "state": {}})()
        cache = {"service_id": "svc", "revision_id": "rev", "prepared_receipt": "receipt"}
        result = broz.ensure_activation_ticket(project, cache, force=True)
        self.assertNotIn("activation_ticket", result)
        self.assertGreater(result["activation_ticket_retry_unix"], time.time())


class WatcherSchedulingTests(unittest.TestCase):
    def test_accept_wait_tracks_next_scan_without_delaying_socket_wakeup(self):
        self.assertEqual(broz.watcher_accept_timeout(10.0, 9.0), 0.001)
        self.assertAlmostEqual(broz.watcher_accept_timeout(10.0, 10.08), 0.08)
        self.assertEqual(broz.watcher_accept_timeout(10.0, 11.0), 0.25)

    def test_worker_command_accepts_fragmented_unix_stream_reads(self):
        class FragmentedSocket:
            def __init__(self):
                self.fragments = [b'{"comm', b'and":"de', b'ploy"}\n']

            def recv(self, _limit):
                return self.fragments.pop(0) if self.fragments else b""

        self.assertEqual(broz.recv_worker_request(FragmentedSocket()), {"command": "deploy"})

    def test_worker_command_rejects_truncated_message(self):
        class TruncatedSocket:
            calls = 0

            def recv(self, _limit):
                self.calls += 1
                return b'{"command":"deploy"}' if self.calls == 1 else b""

        with self.assertRaises(broz.BrozError):
            broz.recv_worker_request(TruncatedSocket())


class SnapshotFreezeTests(unittest.TestCase):
    def test_hash_only_freeze_matches_materialized_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"scripts":{"start":"bun run server.js"}}')
            (root / "bun.lock").write_text("lockfileVersion = 1\n")
            (root / "server.js").write_text("Bun.serve({port: 8080})\n")
            project = type("Project", (), {"root": root, "runtime": "bun", "arch": "amd64", "binary": None})()
            with tempfile.TemporaryDirectory() as full_dir, tempfile.TemporaryDirectory() as hash_dir:
                full = broz.prepare_snapshot(project, Path(full_dir), materialize=True)
                hashed = broz.prepare_snapshot(project, Path(hash_dir), materialize=False)
            self.assertEqual(full["manifest_digest"], hashed["manifest_digest"])
            self.assertEqual(full["content_snapshot_digest"], hashed["content_snapshot_digest"])
            self.assertTrue(all("_local" in blob for blob in full["blobs"]))
            self.assertTrue(all("_local" not in blob for blob in hashed["blobs"]))


class CompletePageTests(unittest.TestCase):
    class Response:
        def __init__(self, body, length=None):
            self.stream = io.BytesIO(body)
            self.length = length

        def getheader(self, name):
            return self.length if name == "Content-Length" else None

        def read(self, amount):
            return self.stream.read(amount)

    def test_complete_page_at_limit_is_accepted(self):
        body = b"x" * 32
        self.assertEqual(broz.read_complete_page(self.Response(body, "32"), 32), body)

    def test_declared_or_streamed_oversize_page_is_rejected(self):
        with self.assertRaises(broz.BrozError):
            broz.read_complete_page(self.Response(b"", "33"), 32)
        with self.assertRaises(broz.BrozError):
            broz.read_complete_page(self.Response(b"x" * 33), 32)


if __name__ == "__main__":
    unittest.main()
