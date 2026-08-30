#!/usr/bin/env python3
import importlib.util
import contextlib
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

    def activate_stream(self, path, body, request_id, on_started):
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


if __name__ == "__main__":
    unittest.main()
