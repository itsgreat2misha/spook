"""Spook - Your homie."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .const import LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import Event, HomeAssistant


# How long a round in the worker process may take before the round is
# given up on and computed in the thread executor instead. A round
# normally takes seconds; this only fires when something is wrong.
ROUND_TIMEOUT = 120

# After this many rounds in a row have failed in the worker process,
# the process is not tried again for the rest of the run: whatever is
# wrong with it is not passing, and the thread executor is fine.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class _PoolState:
    """The worker pool and how it has been doing, kept in one place."""

    executor: ProcessPoolExecutor | None = None
    stop_listener_registered: bool = False
    consecutive_failures: int = 0
    given_up: bool = False


_STATE = _PoolState()


def _create_executor() -> ProcessPoolExecutor:
    """Create the worker pool.

    Its own function, so a test can break it and watch the caller cope.
    """
    # One worker: a round is one job, and one job gets one other core.
    # That is the whole point — the burn lands there, not on the core
    # the event loop lives on.
    return ProcessPoolExecutor(max_workers=1)


def _shutdown_executor(_event: Event) -> None:
    """Shut the worker pool down, if there is one."""
    if _STATE.executor is not None:
        _STATE.executor.shutdown(wait=False, cancel_futures=True)
        _STATE.executor = None


def _drop_executor() -> None:
    """Forget the worker pool after a failure, so the next round remakes it."""
    if _STATE.executor is not None:
        _STATE.executor.shutdown(wait=False, cancel_futures=True)
        _STATE.executor = None


def _get_executor(hass: HomeAssistant) -> ProcessPoolExecutor:
    """Return the worker pool, creating it on first use."""
    if _STATE.executor is None:
        _STATE.executor = _create_executor()
        if not _STATE.stop_listener_registered:
            _STATE.stop_listener_registered = True
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown_executor)
    return _STATE.executor


async def async_run_round(
    hass: HomeAssistant,
    func: Callable[[Any], Any],
    payload: Any,
) -> Any | None:
    """Run one round of computation in the worker process.

    Return the round's result, or None when the round could not be run
    there at all: the pool could not be created, the payload could not
    be pickled, the worker died, or the round took too long. None tells
    the caller to compute the round in the thread executor instead, so
    a broken process costs a repair its isolation for one round, never
    its findings. Repairs degrade; the event loop never does.
    """
    if _STATE.given_up:
        return None

    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(_get_executor(hass), func, payload)
        # Shielded, so giving up on the wait does not pretend the work
        # can be cancelled: a round that is running keeps its (now
        # dropped) worker until it finishes, and this round is simply
        # computed again, in threads.
        result = await asyncio.wait_for(asyncio.shield(future), timeout=ROUND_TIMEOUT)
    except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Anything at all, by design: whatever went wrong in there, the
        # round is computed in the thread executor instead.
        LOGGER.warning(
            "Spook could not compute a round in its worker process (%s: %s); "
            "computing it in the thread executor instead",
            type(err).__name__,
            err,
        )
        _STATE.consecutive_failures += 1
        _drop_executor()
        if _STATE.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            _STATE.given_up = True
            LOGGER.warning(
                "Spook's worker process has failed %d rounds in a row; "
                "not trying it again until Home Assistant restarts",
                _STATE.consecutive_failures,
            )
        return None

    _STATE.consecutive_failures = 0
    return result
