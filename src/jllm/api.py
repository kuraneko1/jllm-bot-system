"""Small, domain-independent plugin API."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class Plugin(Protocol):
    async def setup(self, ctx: PluginContext) -> Mapping[str, object]: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


async def _await_cleanup(task: asyncio.Task[Any]) -> Any:
    """Wait for cleanup despite repeated cancellation, then propagate it."""

    async def wait() -> None:
        try:
            await task
        except BaseException:
            pass

    # Python 3.14 shield logs exception groups from a cancelled waiter.
    waiter = asyncio.create_task(wait())
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


class ResourceScope:
    def __init__(self, owner: str = "unknown") -> None:
        self._owner = owner
        self._disposers: list[Callable[[], Any]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def add(self, disposer: Callable[[], Any]) -> None:
        if self._closed:
            raise RuntimeError("scope is closed")
        self._disposers.append(disposer)

    def create_task(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("scope is closed")
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger("jllm.plugin").error("background task failed for %r", self._owner)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await _await_cleanup(self._close_task)

    async def _close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        errors = []
        for dispose in reversed(self._disposers):
            try:
                result = dispose()
                if inspect.isawaitable(result):
                    await result
            except Exception, asyncio.CancelledError:
                errors.append(RuntimeError(f"resource cleanup failed for {self._owner!r}"))
        if errors:
            raise ExceptionGroup("resource cleanup failed", errors)


@dataclass(frozen=True)
class PluginContext:
    config: Mapping[str, Any]
    dependencies: Mapping[str, Any]
    scope: ResourceScope
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("jllm.plugin"))
    dependency_ids: Mapping[str, Any] = field(default_factory=dict)
