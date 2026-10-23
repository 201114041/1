from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .db import Storage, utc_now


PIPELINE_VERSION = 1
STEP_NAMES = [
    "validate_asset",
    "extract_frames",
    "ai_style_transfer",
    "compose_preview",
]

STEP_LABELS = {
    "validate_asset": "校验动画素材",
    "extract_frames": "抽取关键帧",
    "ai_style_transfer": "AI 风格迁移（Mock）",
    "compose_preview": "合成预览结果",
}


class StepExecutionError(RuntimeError):
    pass


class MockStepRunner:
    """Deterministic, replaceable stand-in for real AI processing."""

    def run(self, task_input: dict[str, Any], step_name: str, attempt: int) -> dict[str, Any]:
        delay_ms = max(0, min(int(task_input.get("step_delay_ms", 500)), 30000))
        time.sleep(delay_ms / 1000)
        fail_step = task_input.get("fail_step")
        fail_times = max(0, int(task_input.get("fail_times", 0)))
        if fail_step == step_name and attempt <= fail_times:
            raise StepExecutionError(
                f"Mock failure injected on {step_name}, attempt {attempt}/{fail_times}"
            )
        return {
            "step": step_name,
            "label": STEP_LABELS.get(step_name, step_name),
            "attempt": attempt,
            "mock_artifact": f"{step_name}-{uuid.uuid4().hex[:8]}.bin",
            "finished_at": utc_now(),
        }


class PipelineService:
    """Single-process worker with persisted task and step state."""

    def __init__(
        self,
        data_dir: Path,
        retry_delay_seconds: float = 1.0,
        auto_start: bool = True,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.results_dir = self.data_dir / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.data_dir / "pipeline.db")
        self.storage.initialize()
        self.recovered_steps = self.storage.recover_interrupted_steps()
        self.retry_delay_seconds = retry_delay_seconds
        self.runner = MockStepRunner()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        if auto_start:
            self.start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, name="pipeline-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def create_task(self, title: str, task_input: dict[str, Any]) -> str:
        return self.storage.create_task(title, task_input, STEP_NAMES)

    def run_once(self) -> bool:
        step = self.storage.claim_next_step()
        if step is None:
            return False
        try:
            task_input = json.loads(step["input_json"])
            cache_key = self._cache_key(task_input, step["name"])
            cached = self.storage.get_cached_result(cache_key)
            if cached and Path(cached["output_path"]).is_file():
                self.storage.complete_step(step, cached["output_path"], reused=True)
                return True

            result = self.runner.run(task_input, step["name"], int(step["attempts"]))
            output_path = self.results_dir / f"{cache_key}.json"
            self._write_json_atomic(output_path, result)
            self.storage.save_cached_result(cache_key, str(output_path), step["name"])
            self.storage.complete_step(step, str(output_path), reused=False)
        except Exception as exc:
            self.storage.fail_step(step, str(exc), self.retry_delay_seconds)
        return True

    def run_until_idle(self, max_rounds: int = 100) -> None:
        for _ in range(max_rounds):
            if not self.run_once():
                return
        raise RuntimeError("Pipeline did not become idle within max_rounds")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self.run_once()
            if not processed:
                self._stop_event.wait(0.15)

    @staticmethod
    def _cache_key(task_input: dict[str, Any], step_name: str) -> str:
        payload = {
            "version": PIPELINE_VERSION,
            "input": task_input,
            "step": step_name,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
