import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flows.orchestrator import run_pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OrchestratorTests(unittest.TestCase):
    def test_dry_run_compiles_and_generates_seed_sql(self):
        result = run_pipeline("7", repo_root=str(PROJECT_ROOT))

        self.assertEqual(result["status"], "ready_for_deployment")
        self.assertEqual(result["current_step"], "data_smith")
        self.assertEqual(result["audit_result"]["decision"], "SAFE")
        self.assertEqual(
            result["blueprint"]["fk_safe_order"],
            ["doctors", "patients", "appointments"],
        )
        self.assertIn("INSERT INTO doctors", result["seeds"]["sql"])
        self.assertIn("INSERT INTO appointments", result["seeds"]["sql"])
        self.assertNotIn("deployment", result)

    def test_unsafe_migration_stops_before_seed_and_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "db").mkdir()
            (root / "migrations").mkdir()
            (root / "db" / "schema.sql").write_text(
                "CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY);",
                encoding="utf-8",
            )
            (root / "migrations" / "001_unsafe.sql").write_text(
                "DROP TABLE users; ALTER TABLE users ADD COLUMN ssn TEXT NOT NULL;",
                encoding="utf-8",
            )

            result = run_pipeline("8", repo_root=str(root), execute=True)

        self.assertEqual(result["status"], "rejected_by_auditor")
        self.assertEqual(result["audit_result"]["decision"], "DO NOT DEPLOY")
        self.assertFalse(result["audit_passed"])
        self.assertNotIn("seeds", result)
        self.assertNotIn("deployment", result)

    def test_execute_path_uses_real_bridge_contract_and_runs_smoke_checks(self):
        calls = []

        class FakeBridgeClient:
            def __init__(self, base_url=None, api_key=None):
                calls.append(("init", base_url, api_key))

            def get(self, endpoint):
                calls.append(("GET", endpoint))
                return {"sufficient": True, "available_ram_mb": 2048, "free_disk_mb": 4096}

            def post(self, endpoint, payload=None, timeout=None):
                calls.append(("POST", endpoint, payload, timeout))
                if endpoint == "rebuild":
                    return {"status": "rebuilt"}
                if endpoint == "deploy_shadow_clone":
                    return {
                        "status": "live",
                        "backend_url": "http://shadow.test:51000",
                        "frontend_url": "http://shadow.test:52000",
                    }
                raise AssertionError(f"Unexpected endpoint: {endpoint}")

        def fake_http(url, *, method="GET", payload=None, timeout=20):
            calls.append(("HTTP", method, url, payload))
            if method == "POST":
                return 201, {"id": 99}
            if method == "DELETE":
                return 204, None
            return 200, {"ok": True}

        with patch("flows.orchestrator.BridgeClient", FakeBridgeClient), patch(
            "flows.orchestrator._http_json", side_effect=fake_http
        ):
            result = run_pipeline(
                "7",
                repo_root=str(PROJECT_ROOT),
                bridge_url="http://bridge.test:8888",
                api_key="test-key",
                branch="feature/test",
                execute=True,
            )

        self.assertEqual(result["status"], "pipeline_success")
        self.assertEqual(result["smoke_test"]["verdict"], "PASS")
        deploy_call = next(call for call in calls if call[:2] == ("POST", "deploy_shadow_clone"))
        self.assertEqual(deploy_call[2]["pr_id"], "7")
        self.assertIn("INSERT INTO patients", deploy_call[2]["seed_sql"])
        self.assertIn(("GET", "check_vm_headroom"), calls)


if __name__ == "__main__":
    unittest.main()
