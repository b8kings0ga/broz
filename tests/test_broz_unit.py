#!/usr/bin/env python3
import importlib.util
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
        deployment = {"deployment_id": "dep_exact", "status": "accessible"}
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


if __name__ == "__main__":
    unittest.main()
