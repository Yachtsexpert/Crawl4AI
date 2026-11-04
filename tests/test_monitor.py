import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from monitor.main import actor_input, canonical, collect, load_recovery_runs, validate


class FakeClient:
    def __init__(self, markdown="A boat", status="SUCCEEDED"):
        self.markdown, self.status = markdown, status

    def actor(self, name):
        return self

    def call(self, **kwargs):
        # Mimic apify-client >=3: typed Pydantic Run with attributes and enum status.
        return SimpleNamespace(id="test-run", status=SimpleNamespace(value=self.status),
                               default_dataset_id="test-dataset")

    def run(self, run_id):
        self.reused_run_id = run_id
        return self

    def get(self):
        return self.call()

    def dataset(self, dataset_id):
        return self

    def iterate_items(self):
        yield {"url": "https://example.com/news/a?utm_source=test",
               "markdown": self.markdown, "metadata": {"title": "A"}}


CFG = {"schema_version": 1, "max_charge_usd_per_source": "0.10",
       "sources": [{"id": "test-news", "url": "https://example.com/news",
                    "max_pages": 2, "enabled": True}]}


class TestMonitor(unittest.TestCase):
    def test_config(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "config.json"
            path.write_text(json.dumps(CFG), encoding="utf-8")
            self.assertEqual(len(validate(path)["sources"]), 1)

    def test_dedupe_and_changes(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base)
            self.assertEqual(collect(FakeClient(), CFG, path), 0)
            self.assertEqual(collect(FakeClient("A boat"), CFG, path), 0)
            self.assertEqual(collect(FakeClient("B boat"), CFG, path), 0)
            changes = [json.loads(x) for x in (path / "changes.jsonl").read_text().splitlines()]
            self.assertEqual([x["event"] for x in changes], ["NEW_PAGE", "CONTENT_CHANGED"])
            self.assertNotIn("B boat", (path / "observations.jsonl").read_text())
            observations = [json.loads(x) for x in (path / "observations.jsonl").read_text().splitlines()]
            self.assertTrue(all(x["actor_run_id"] == "test-run" for x in observations))
            self.assertEqual(len(observations), 3)

    def test_fail_preserves_state(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base)
            self.assertEqual(collect(FakeClient(), CFG, path), 0)
            old = (path / "last_seen.json").read_text()
            self.assertEqual(collect(FakeClient("changed", status="FAILED"), CFG, path), 1)
            self.assertEqual((path / "last_seen.json").read_text(), old)
            last_run = json.loads((path / "runs.jsonl").read_text().splitlines()[-1])
            self.assertEqual(last_run["error_stage"], "run_status")
            self.assertEqual(last_run["actor_status"], "FAILED")
            self.assertEqual(last_run["actor_run_id"], "test-run")

    def test_recovery_does_not_start_actor(self):
        class ReplayClient(FakeClient):
            def actor(self, name):
                raise AssertionError("Recovery must not start billable Actors")

        with tempfile.TemporaryDirectory() as base:
            client = ReplayClient()
            self.assertEqual(collect(client, CFG, Path(base),
                                     replay_runs={"test-news": "abcdefghijklmnopq"}), 0)
            self.assertEqual(client.reused_run_id, "abcdefghijklmnopq")
            observation = json.loads((Path(base) / "observations.jsonl").read_text().splitlines()[0])
            self.assertEqual(observation["collection_mode"], "REPLAY")

    def test_recovery_mapping_validation(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "runs.json"
            path.write_text(json.dumps({"test-news": "abcdefghijklmnopq"}))
            self.assertEqual(load_recovery_runs(path, CFG)["test-news"], "abcdefghijklmnopq")
            path.write_text(json.dumps({"unrelated": "abcdefghijklmnopq"}))
            with self.assertRaises(ValueError):
                load_recovery_runs(path, CFG)

    def test_actor_limits(self):
        params = actor_input(CFG["sources"][0])
        self.assertTrue(params["respectRobotsTxtFile"])
        self.assertEqual(params["maxCrawlPages"], 2)

    def test_canonical(self):
        self.assertEqual(canonical("https://Example.com/a/?utm_source=b&year=2026#foo"),
                         "https://example.com/a?year=2026")


if __name__ == "__main__":
    unittest.main()
