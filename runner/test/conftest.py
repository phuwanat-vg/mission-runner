import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXAMPLES = ROOT.parent / "examples"

from mission_runner.config import RunnerConfig  # noqa: E402
from mission_runner.runner import Runner  # noqa: E402
from mission_runner.types import RunSource  # noqa: E402

_port = [18100]


def example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text("utf-8"))


class Harness:
    """A sim runner in a temp home with a fast clock and an event recorder."""

    def __init__(self, runner: Runner):
        self.r = runner
        self.events: list[dict] = []
        runner.events.subscribe(self.events.append)

    async def run(self, name: str, inputs: dict | None = None, **kw):
        ok, run, reason = await self.r.run_mission(name, inputs or {}, RunSource("manual", "test"), **kw)
        assert ok, reason
        return run

    async def wait_idle(self, timeout: float = 30.0) -> bool:
        t0 = time.monotonic()
        while self.r.dispatcher.busy() and time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.02)
        return not self.r.dispatcher.busy()

    async def wait_step(self, run_id: str, step_id: str, timeout: float = 10.0) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if any(e["type"] == "step.started" and e["run_id"] == run_id and e["step_id"] == step_id for e in self.events):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"step {step_id} of {run_id} never started")

    def steps(self, run_id: str) -> list[tuple[str, str]]:
        return [(e["step_id"], e["result"]["status"]) for e in self.events if e["type"] == "step.finished" and e["run_id"] == run_id]

    def logs(self, run_id: str | None = None) -> list[str]:
        return [e["text"] for e in self.events if e["type"] == "log" and (run_id is None or e.get("run_id") == run_id)]

    def run_events(self) -> list[tuple[str, str, str]]:
        return [(e["type"], e["run"]["mission"], e["run"]["status"]) for e in self.events if e["type"].startswith("run.") and "run" in e]


@pytest.fixture
async def harness(tmp_path):
    _port[0] += 1
    cfg = RunnerConfig.load(tmp_path, backend="sim", http_port=_port[0], http_host="127.0.0.1")
    cfg.sim = {**cfg.sim, "time_scale": 8.0}
    runner = Runner(cfg)
    await runner.start()
    h = Harness(runner)
    try:
        yield h
    finally:
        await runner.stop()


@pytest.fixture
def sites_doc():
    return example("sites")
