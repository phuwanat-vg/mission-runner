"""``ask_user``: one pending question at a time, answered from any UI
(editor, robot web page, ROS service, MQTT)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from .events import EventBus
from .types import Prompt, StepFailed, StepTimeout, TaskCanceled


class PromptManager:
    def __init__(self, events: EventBus):
        self.events = events
        self.current: Prompt | None = None
        self._future: asyncio.Future[str] | None = None

    async def ask(self, *, run_id: str, mission: str, step_id: str, text: str, options: list[str], default: str | None, timeout_s: float | None) -> str:
        if self.current is not None:
            raise StepFailed("another question is already waiting for an answer")
        loop = asyncio.get_running_loop()
        expires = None
        if timeout_s:
            expires = (datetime.now(timezone.utc) + timedelta(seconds=timeout_s)).astimezone().isoformat(timespec="seconds")
        prompt = Prompt(id=uuid.uuid4().hex[:8], run_id=run_id, mission=mission, step_id=step_id, text=text, options=list(options), default=default, expires_at=expires)
        self.current = prompt
        self._future = loop.create_future()
        self.events.emit("prompt", prompt=prompt.as_dict())
        try:
            if timeout_s:
                try:
                    answer = await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout_s)
                except asyncio.TimeoutError:
                    if default is None:
                        raise StepTimeout(f"no answer within {timeout_s:g} s") from None
                    answer = default
                    self.events.emit("prompt.answered", prompt_id=prompt.id, answer=answer, by="timeout")
            else:
                answer = await self._future
            return answer
        except asyncio.CancelledError:
            self.events.emit("prompt.answered", prompt_id=prompt.id, answer=None, by="canceled")
            raise TaskCanceled() from None
        finally:
            self.current = None
            self._future = None

    def answer(self, prompt_id: str, answer: str) -> tuple[bool, str]:
        p = self.current
        if p is None or p.id != prompt_id:
            return False, "no such prompt"
        if answer not in p.options:
            return False, f"answer must be one of {p.options}"
        if self._future and not self._future.done():
            self._future.set_result(answer)
            self.events.emit("prompt.answered", prompt_id=p.id, answer=answer, by="user")
        return True, "ok"

    def cancel(self) -> None:
        if self._future and not self._future.done():
            self._future.cancel()
