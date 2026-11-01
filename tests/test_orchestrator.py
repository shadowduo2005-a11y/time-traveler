import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from flows.orchestrator import run_pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OrchestratorTests(unittest.TestCase):
    def test_dry_run_compiles_and_generates_seed_sql(self):
        result = run_pipeline("7", repo_root=str(PROJECT_ROOT), ai_mode="off")

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

            result = run_pipeline("8", repo_root=str(root), execute=True, ai_mode="off")

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
                ai_mode="off",
            )

        self.assertEqual(result["status"], "pipeline_success")
        self.assertEqual(result["smoke_test"]["verdict"], "PASS")
        deploy_call = next(call for call in calls if call[:2] == ("POST", "deploy_shadow_clone"))
        self.assertEqual(deploy_call[2]["pr_id"], "7")
        self.assertIn("INSERT INTO patients", deploy_call[2]["seed_sql"])
        self.assertIn(("GET", "check_vm_headroom"), calls)

    def test_required_gemini_mode_enriches_three_specialists(self):
        responses = {
            "shadow-architect": {
                "summary": "Three related tables with a valid FK order.",
                "risks": ["Foreign-key indexes require review."],
                "migration_order": ["doctors", "patients", "appointments"],
                "recommendations": ["Test migrations from the previous schema."],
            },
            "migration-auditor": {
                "decision": "CAUTION",
                "rationale": "Operational review is required before production use.",
                "findings": [
                    {
                        "migration_path": "migrations/004_add_appointments.sql",
                        "level": "WARN",
                        "category": "performance",
                        "message": "Add indexes for foreign-key lookup columns.",
                    }
                ],
            },
            "data-smith": {
                "hints": [
                    {
                        "table": "doctors",
                        "column": "specialty",
                        "values": ["Cardiology", "Neurology"],
                    }
                ],
                "privacy_notes": ["All values are fictional."],
            },
        }

        def fake_gemini(agent, prompt, response_model, model):
            self.assertEqual(model, "gemini-test")
            self.assertTrue(prompt)
            return responses[agent]

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}), patch(
            "flows.orchestrator._gemini_json", side_effect=fake_gemini
        ) as gemini:
            result = run_pipeline(
                "7",
                repo_root=str(PROJECT_ROOT),
                ai_mode="required",
                gemini_model="gemini-test",
            )

        self.assertEqual(gemini.call_count, 3)
        self.assertTrue(result["ai"]["enabled"])
        self.assertEqual(result["audit_result"]["decision"], "CAUTION")
        self.assertIn("gemini_review", result["blueprint"])
        self.assertIn("gemini_guidance", result["seeds"])
        self.assertIn("Cardiology", result["seeds"]["sql"])

    def test_required_gemini_mode_fails_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = run_pipeline(
                "7",
                repo_root=str(PROJECT_ROOT),
                ai_mode="required",
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("GEMINI_API_KEY", result["error"])


if __name__ == "__main__":
    unittest.main()
