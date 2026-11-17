"""Tests for unknown-references computations running in a worker process."""

# ruff: noqa: SLF001
# pylint: disable=protected-access

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_socket

from homeassistant.helpers.entity_component import DATA_INSTANCES

from custom_components.spook import process_executor
from custom_components.spook.ectoplasms.script.repairs.unknown_area_references import (
    SpookRepair,
)
from custom_components.spook.repairs import InspectedEntity
from tests.process_helpers import PidRecordingRepair
from tests.repair_helpers import async_issue_about

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry as ar, issue_registry as ir


def _snapshot() -> InspectedEntity:
    """Return a snapshot of nothing, for a round that only reports itself."""
    return InspectedEntity(
        raw_config=None, referenced=frozenset(), action_sequence=None
    )


@pytest.fixture(autouse=True)
def _process_executor_state(request: pytest.FixtureRequest) -> Iterator[None]:
    """Allow the fork server's socket for marked tests; reset pool state.

    The test harness restricts socket connections to localhost, and a
    worker process cannot be started within that: the pool's manager
    connects to the interpreter's fork server over a Unix socket,
    whose host is no host at all. A test marked `enable_socket`
    therefore re-allows Unix sockets for its run (the harness's own
    setup restricts them again for the next test). Every test here
    starts from a clean slate and leaves one behind: rounds run by
    other tests, in a harness where the worker cannot start, count
    as failures in the module state and would otherwise latch the
    give-up for the whole session before these tests ever run.
    """
    if request.node.get_closest_marker("enable_socket"):
        pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    executor = process_executor._STATE.executor
    process_executor._STATE.executor = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    process_executor._STATE.consecutive_failures = 0
    process_executor._STATE.given_up = False
    yield
    # Shut the pool down fully, threads joined: the harness fails a
    # test that leaves the pool's manager thread behind.
    executor = process_executor._STATE.executor
    process_executor._STATE.executor = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    # A pool dropped after a failure was shut down without waiting;
    # give its manager thread the moment it needs to actually leave.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        type(thread).__name__ == "_ExecutorManagerThread"
        for thread in threading.enumerate()
    ):
        time.sleep(0.05)
    process_executor._STATE.consecutive_failures = 0
    process_executor._STATE.given_up = False


@pytest.mark.enable_socket
async def test_round_is_computed_in_another_process(
    hass: HomeAssistant,
) -> None:
    """Test a round runs in the worker process, not this one, and reuses it.

    The worker process is the point: its cost lands on another core,
    under another interpreter, and cannot contend with the event loop.
    A repair whose finding is the ID of the process that computed it
    shows where the round ran, and two rounds show the same worker
    answering both, rather than a process spawned per round.
    """
    repair = PidRecordingRepair(hass)
    repair._known_ids = set()

    first = await repair._async_compute_round([_snapshot()])
    second = await repair._async_compute_round([_snapshot()])

    assert first == second
    (pid,) = first[0]
    assert pid != str(os.getpid())


async def test_round_falls_back_to_threads_when_the_pool_cannot_start(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a round still completes, in threads, when the pool cannot start.

    The fallback is the guarantee the process is built on: whatever is
    wrong with it, a repair gets its findings anyway. Only the
    isolation is lost, for that round.
    """

    def _broken_factory() -> Any:
        msg = "no worker today"
        raise RuntimeError(msg)

    monkeypatch.setattr(process_executor, "_create_executor", _broken_factory)

    repair = PidRecordingRepair(hass)
    repair._known_ids = set()

    unknowns = await repair._async_compute_round([_snapshot()])

    # Computed here, in this process, by the thread executor instead.
    assert unknowns == [{str(os.getpid())}]
    assert "worker process" in caplog.text


@pytest.mark.enable_socket
async def test_round_falls_back_to_threads_when_the_payload_cannot_cross(
    hass: HomeAssistant,
) -> None:
    """Test a round still completes when its state cannot be pickled.

    A lambda in the cached state cannot cross to the worker process.
    The round must not be lost with it: the thread executor computes
    it here, where nothing needs pickling.
    """

    def _unpicklable(value: str) -> str:
        return value

    repair = PidRecordingRepair(hass)
    # A set holding a local function is a set, but no pickle of it exists.
    repair._known_ids = {_unpicklable}  # type: ignore[arg-type]

    unknowns = await repair._async_compute_round([_snapshot()])

    assert unknowns == [{str(os.getpid())}]


@pytest.mark.enable_socket
async def test_area_round_runs_in_the_worker_process_end_to_end(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    issue_registry: ir.IssueRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test a covered repair raises and clears an issue via the worker process.

    The same computation in this process is sabotaged, so findings can
    only have come from the worker. The issue is then cleared by a
    second round, also in the worker, once the area exists.
    """

    def _sabotaged(_repair: SpookRepair, _entities: Any) -> list[set[str]]:
        msg = "computed in the test process"
        raise AssertionError(msg)

    monkeypatch.setattr(SpookRepair, "_compute_unknown_references_for_all", _sabotaged)

    area = area_registry.async_create("Upstairs")
    entity = SimpleNamespace(
        entity_id="script.spooky",
        name="Spooky",
        unique_id="spooky",
        referenced_areas={area.id, "ghost_area"},
    )
    hass.data.setdefault(DATA_INSTANCES, {})["script"] = SimpleNamespace(
        entities=[entity],
    )

    repair = SpookRepair(hass)
    await repair._async_inspect_with_cleanup()

    issue = async_issue_about(
        issue_registry, "script_unknown_area_references_script.spooky"
    )
    assert issue
    assert issue.translation_placeholders
    assert issue.translation_placeholders["areas"] == "- `ghost_area`"

    area_registry.async_create("Ghost Area")
    await repair._async_inspect_with_cleanup()

    assert (
        async_issue_about(
            issue_registry, "script_unknown_area_references_script.spooky"
        )
        is None
    )
