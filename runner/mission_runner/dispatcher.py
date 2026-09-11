"""Decides what runs when: one active run, a priority queue, suspended runs
waiting to resume, and the dispatch policies described in
docs/mission-format.md."""

from __future__ import annotations

import asyncio
import logging
import random
import string
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .interpreter import Interpreter, RunContext, RunOutcome
from .model import Mission
from .store import RunLog
from .types import MissionError, Run, RunSource, RunStatus, StepFailed, now_iso

log = logging.getLogger("mission.dispatcher")

POLICIES = ("queue", "preempt", "preempt_latest", "reject_if_busy", "interrupt_and_resume")

MissionLookup = Callable[[str], Mission | None]
RunHook = Callable[[Run, RunContext | None], Awaitable[None] | None]


class Dispatcher:
    def __init__(self, interpreter: Interpreter, lookup: MissionLookup, run_log: RunLog | None = None):
        self.interp = interpreter
        self.lookup = lookup
        self.run_log = run_log
        self.events = interpreter.s.events
        self.current: Run | None = None
        self.current_ctx: RunContext | None = None
        self.current_task: asyncio.Task[None] | None = None
        #: innermost first is the last element: parent, child, grandchild...
        self.ctx_stack: list[RunContext] = []
        self.queue: list[Run] = []
        self.suspended: list[tuple[Run, RunContext]] = []
        self.on_run_started: RunHook | None = None
        self.on_run_finished: RunHook | None = None
        self._wake = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None
        self._stopping = False

    # ----- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._stopping = False
        self._pump_task = asyncio.create_task(self._pump(), name="dispatcher")

    async def stop(self) -> None:
        self._stopping = True
        await self.stop_all("runner shutting down")
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None

    # ----- status -------------------------------------------------------------

    @property
    def active(self) -> Run | None:
        """The current run unless it already finished (the pump clears it a tick later)."""
        cur = self.current
        return cur if cur is not None and not cur.status.finished else None

    @property
    def state(self) -> str:
        cur = self.active
        if cur is None:
            return "idle"
        return "paused" if cur.status == RunStatus.PAUSED else "running"

    def busy(self) -> bool:
        return self.current is not None or bool(self.queue) or bool(self.suspended)

    def status(self) -> dict[str, Any]:
        cur = self.active
        return {
            "state": self.state,
            "run": cur.as_dict() if cur else None,
            "queue": [r.as_dict() for r in self.queue],
            "suspended": [r.as_dict() for r, _ in self.suspended],
        }

    def find(self, run_id: str) -> Run | None:
        if self.current and self.current.id == run_id:
            return self.current
        for r in self.queue:
            if r.id == run_id:
                return r
        for r, _ in self.suspended:
            if r.id == run_id:
                return r
        for c in self.ctx_stack:
            if c.run.id == run_id:
                return c.run
        return None

    # ----- submit ----------------------------------------------------------------

    async def submit(
        self,
        mission_name: str,
        inputs: dict[str, Any] | None,
        source: RunSource,
        policy: str | None = None,
        priority: int | None = None,
    ) -> tuple[bool, Run | None, str]:
        mission = self.lookup(mission_name)
        if mission is None:
            return False, None, f"unknown mission '{mission_name}'"
        if mission.is_global:
            return False, None, "the global mission cannot be run"
        pol = policy or mission.policy
        if pol not in POLICIES:
            return False, None, f"unknown policy '{pol}'"
        prio = mission.priority if priority is None else int(priority)
        run = Run(id=_new_run_id(), mission=mission.name, inputs=dict(inputs or {}), source=source, priority=prio, policy=pol)
        # Validate inputs early so a bad trigger payload is reported at submit time.
        try:
            self.interp.make_context(run, mission)
        except StepFailed as e:
            return False, None, str(e)

        cur = self.current
        if pol == "reject_if_busy" and self.busy():
            return False, None, "busy"

        if pol == "preempt_latest":
            dropped = [r for r in self.queue if r.mission == mission.name]
            for r in dropped:
                self.queue.remove(r)
                self._finish_queued(r, "replaced by a newer request")

        if pol in ("preempt", "preempt_latest") and cur is not None:
            if prio >= cur.priority:
                await self._cancel_current("preempted by %s" % mission.name)
                self.queue.insert(0, run)
                self._enqueue_event(run)
                self._wake.set()
                return True, run, "preempting"
            self._push_queue(run)
            return True, run, "queued (current run has higher priority)"

        if pol == "interrupt_and_resume" and cur is not None:
            if prio >= cur.priority:
                await self._suspend_current(f"interrupted by {mission.name}")
                self.queue.insert(0, run)
                self._enqueue_event(run)
                self._wake.set()
                return True, run, "interrupting"
            self._push_queue(run)
            return True, run, "queued (current run has higher priority)"

        self._push_queue(run)
        return True, run, "queued" if (cur is not None or len(self.queue) > 1) else "starting"

    def _push_queue(self, run: Run) -> None:
        self.queue.append(run)
        self.queue.sort(key=lambda r: (-r.priority, r.created_mono))
        self._enqueue_event(run)
        self._wake.set()

    def _enqueue_event(self, run: Run) -> None:
        run.status = RunStatus.QUEUED
        self._persist(run)
        self.events.emit("run.queued", run=run.as_dict())

    def _finish_queued(self, run: Run, reason: str) -> None:
        run.status = RunStatus.CANCELED
        run.error = reason
        run.finished_at = now_iso()
        self._persist(run)
        self.events.emit("run.finished", run=run.as_dict())

    # ----- control -----------------------------------------------------------------

    async def cancel(self, run_id: str, reason: str = "canceled by user") -> tuple[bool, str]:
        if self.current and self.current.id == run_id:
            await self._cancel_current(reason)
            return True, "canceling"
        for r in self.queue:
            if r.id == run_id:
                self.queue.remove(r)
                self._finish_queued(r, reason)
                return True, "removed from queue"
        for item in self.suspended:
            if item[0].id == run_id:
                self.suspended.remove(item)
                await self._abort_suspended(item[0], item[1], reason)
                return True, "canceled"
        return False, "no such run"

    async def pause(self, run_id: str | None = None) -> tuple[bool, str]:
        cur = self.current
        if cur is None or (run_id and cur.id != run_id):
            return False, "nothing is running"
        if cur.status == RunStatus.PAUSED:
            return True, "already paused"
        ctx = self.ctx_stack[-1] if self.ctx_stack else self.current_ctx
        if ctx is None:
            return False, "no context"
        ctx.control = "pause"
        ctx.control_reason = "paused by user"
        if ctx.step_task:
            ctx.step_task.cancel()
        return True, "pausing"

    async def resume(self, run_id: str | None = None) -> tuple[bool, str]:
        cur = self.current
        if cur is None or (run_id and cur.id != run_id):
            return False, "nothing is running"
        for ctx in self.ctx_stack or ([self.current_ctx] if self.current_ctx else []):
            if ctx.control == "pause":
                ctx.control = None
            ctx.resume_event.set()
        return True, "resuming"

    async def stop_all(self, reason: str = "stopped by user") -> None:
        """The STOP button: cancel the active run, the queue and suspended runs."""
        for r in list(self.queue):
            self.queue.remove(r)
            self._finish_queued(r, reason)
        if self.current is not None:
            await self._cancel_current(reason)
            task = self.current_task
            if task:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=30)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        while self.suspended:
            run, ctx = self.suspended.pop()
            await self._abort_suspended(run, ctx, reason)

    async def _cancel_current(self, reason: str) -> None:
        for ctx in self.ctx_stack or ([self.current_ctx] if self.current_ctx else []):
            ctx.control = "cancel"
            ctx.control_reason = reason
            ctx.resume_event.set()
        inner = self.ctx_stack[-1] if self.ctx_stack else self.current_ctx
        if inner and inner.step_task:
            inner.step_task.cancel()

    async def _suspend_current(self, reason: str) -> None:
        for ctx in self.ctx_stack or ([self.current_ctx] if self.current_ctx else []):
            ctx.control = "suspend"
            ctx.control_reason = reason
            ctx.resume_event.set()
        inner = self.ctx_stack[-1] if self.ctx_stack else self.current_ctx
        if inner and inner.step_task:
            inner.step_task.cancel()
        # Wait for the run task to yield the suspended outcome before starting the next run.
        task = self.current_task
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def _abort_suspended(self, run: Run, ctx: RunContext, reason: str) -> None:
        run.status = RunStatus.CANCELED
        run.error = reason
        run.finished_at = now_iso()
        self.events.emit("run.finished", run=run.as_dict())
        self._persist(run)
        if ctx.mission.on_abort:
            try:
                await self.interp.run_on_abort(ctx, RunOutcome(RunStatus.CANCELED, reason))
            except Exception:  # noqa: BLE001
                log.exception("on_abort of suspended run failed")
        await self._call_hook(self.on_run_finished, run, ctx)

    # ----- execution ---------------------------------------------------------------

    async def _pump(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.current is None and (self.queue or self.suspended) and not self._stopping:
                if self.queue:
                    run = self.queue.pop(0)
                    mission = self.lookup(run.mission)
                    if mission is None:
                        self._finish_queued(run, "mission was deleted")
                        continue
                    try:
                        ctx = self.interp.make_context(run, mission)
                    except StepFailed as e:
                        self._finish_queued(run, str(e))
                        continue
                    resume = False
                else:
                    run, ctx = self.suspended.pop()
                    ctx.resume_path = run.resume_path
                    ctx.control = None
                    ctx.control_reason = ""
                    ctx.resume_event.set()
                    resume = True
                self.current = run
                self.current_ctx = ctx
                self.ctx_stack = [ctx]
                self.current_task = asyncio.create_task(self._execute(run, ctx, resume), name=f"run:{run.id}")
                try:
                    await self.current_task
                except asyncio.CancelledError:
                    if self._stopping:
                        raise
                except Exception:  # noqa: BLE001
                    log.exception("run task failed")
                finally:
                    self.current = None
                    self.current_ctx = None
                    self.ctx_stack = []
                    self.current_task = None

    async def _execute(self, run: Run, ctx: RunContext, resume: bool) -> None:
        run.status = RunStatus.RUNNING
        if resume:
            run.source = RunSource("resume", run.source.id, run.source.detail)
            self.events.emit("run.resumed", run=run.as_dict())
        else:
            run.started_at = now_iso()
            self.events.emit("run.started", run=run.as_dict())
        self._persist(run)
        await self._call_hook(self.on_run_started, run, ctx)
        outcome = await self.interp.run(ctx)
        self._apply_outcome(run, outcome)
        if outcome.status == RunStatus.SUSPENDED:
            run.resume_path = outcome.resume_path
            self.suspended.append((run, ctx))
            self.events.emit("run.suspended", run=run.as_dict())
            self._persist(run)
            await self._call_hook(self.on_run_finished, run, ctx)
            return
        self.events.emit("run.finished", run=run.as_dict())
        self._persist(run)
        await self._call_hook(self.on_run_finished, run, ctx)
        self._wake.set()

    def _apply_outcome(self, run: Run, outcome: RunOutcome) -> None:
        run.status = outcome.status
        run.error = outcome.error
        run.result = outcome.result
        run.step = None
        run.feedback = None
        if outcome.status != RunStatus.SUSPENDED:
            run.finished_at = now_iso()

    async def run_sub_mission(self, ctx: RunContext, name: str, inputs: dict[str, Any]) -> Any:
        """Run another mission inline inside the current run (the run_mission step)."""
        mission = self.lookup(name)
        if mission is None:
            raise MissionError(f"unknown mission '{name}'")
        if mission.is_global:
            raise MissionError("the global mission cannot be run")
        child = Run(id=_new_run_id(), mission=mission.name, inputs=dict(inputs), source=RunSource("sub", ctx.run.id, ctx.mission.name), priority=ctx.run.priority, policy="queue", parent_run_id=ctx.run.id)
        cctx = self.interp.make_context(child, mission)
        cctx.depth = ctx.depth + 1
        child.status = RunStatus.RUNNING
        child.started_at = now_iso()
        self.ctx_stack.append(cctx)
        self.events.emit("run.started", run=child.as_dict())
        self._persist(child)
        try:
            outcome = await self.interp.run(cctx)
        finally:
            if self.ctx_stack and self.ctx_stack[-1] is cctx:
                self.ctx_stack.pop()
        self._apply_outcome(child, outcome)
        if outcome.status == RunStatus.SUSPENDED:
            child.status = RunStatus.CANCELED
            child.error = "suspended with parent (restarts on resume)"
            child.finished_at = now_iso()
        self.events.emit("run.finished", run=child.as_dict())
        self._persist(child)
        return outcome

    # ----- misc ------------------------------------------------------------------

    def _persist(self, run: Run) -> None:
        if self.run_log is None:
            return
        try:
            self.run_log.upsert_run(run.as_dict())
        except Exception:  # noqa: BLE001
            log.exception("run log write failed")

    async def _call_hook(self, hook: RunHook | None, run: Run, ctx: RunContext | None) -> None:
        if hook is None:
            return
        try:
            r = hook(run, ctx)
            if r is not None:
                await r
        except Exception:  # noqa: BLE001
            log.exception("run hook failed")


def _new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{stamp}-{suffix}"


__all__ = ["Dispatcher", "POLICIES"]
_ = time  # keep for potential timing hooks
