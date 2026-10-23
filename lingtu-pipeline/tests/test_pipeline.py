from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path

from pipeline_app.pipeline import PipelineService


class PipelineServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.service = PipelineService(
            data_dir=self.data_dir,
            retry_delay_seconds=0,
            auto_start=False,
        )

    def tearDown(self) -> None:
        self.service.stop()
        self.temp_dir.cleanup()

    def make_input(self, fail_step: str | None = None, fail_times: int = 0) -> dict:
        return {
            "asset_name": f"asset-{uuid.uuid4().hex}.mp4",
            "step_delay_ms": 0,
            "fail_step": fail_step,
            "fail_times": fail_times,
            "pipeline_version": 1,
        }

    def wait_for_terminal(self, task_id: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.service.run_once()
            task = self.service.storage.get_task(task_id)
            if task and task["status"] in {"succeeded", "failed"}:
                return task
            time.sleep(0.01)
        self.fail(f"task {task_id} did not reach a terminal state")

    def test_happy_path_completes_all_steps_in_order(self) -> None:
        task_id = self.service.create_task("happy path", self.make_input())
        task = self.wait_for_terminal(task_id)
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual([step["status"] for step in task["steps"]], ["succeeded"] * 4)
        self.assertTrue(all(Path(step["output_path"]).is_file() for step in task["steps"]))

    def test_failed_step_is_retried_without_restarting_previous_steps(self) -> None:
        task_id = self.service.create_task(
            "retry path",
            self.make_input("ai_style_transfer", fail_times=1),
        )
        task = self.wait_for_terminal(task_id)
        self.assertEqual(task["status"], "succeeded")
        retried = next(step for step in task["steps"] if step["name"] == "ai_style_transfer")
        self.assertEqual(retried["attempts"], 2)
        events = [item["event"] for item in task["history"]]
        self.assertIn("retry_scheduled", events)
        self.assertEqual(task["steps"][0]["attempts"], 1)
        self.assertEqual(task["steps"][1]["attempts"], 1)

    def test_running_step_is_recovered_after_process_restart(self) -> None:
        task_id = self.service.create_task("recovery path", self.make_input())
        running_step = self.service.storage.claim_next_step()
        self.assertEqual(running_step["name"], "validate_asset")

        recovered_service = PipelineService(
            data_dir=self.data_dir,
            retry_delay_seconds=0,
            auto_start=False,
        )
        self.addCleanup(recovered_service.stop)
        self.assertEqual(recovered_service.recovered_steps, 1)

        task = self.wait_for_terminal(task_id)
        self.assertEqual(task["status"], "succeeded")
        events = [item["event"] for item in task["history"]]
        self.assertIn("step_recovered", events)

    def test_identical_task_reuses_cached_step_results(self) -> None:
        shared_input = self.make_input()
        first_id = self.service.create_task("cache source", shared_input)
        self.wait_for_terminal(first_id)

        second_id = self.service.create_task("cache consumer", shared_input)
        second = self.wait_for_terminal(second_id)
        self.assertEqual(second["status"], "succeeded")
        self.assertTrue(all(step["reused"] == 1 for step in second["steps"]))
        self.assertEqual(
            {step["output_path"] for step in second["steps"]},
            {
                step["output_path"]
                for step in self.service.storage.get_task(first_id)["steps"]
            },
        )

    def test_exhausted_retries_mark_task_failed(self) -> None:
        task_id = self.service.create_task(
            "failure path",
            self.make_input("compose_preview", fail_times=3),
        )
        task = self.wait_for_terminal(task_id)
        self.assertEqual(task["status"], "failed")
        failed = next(step for step in task["steps"] if step["name"] == "compose_preview")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempts"], 3)
        self.assertIn("step_failed", [item["event"] for item in task["history"]])


if __name__ == "__main__":
    unittest.main()
