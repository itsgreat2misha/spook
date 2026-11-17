"""Shared helpers for testing Spook's worker process rounds."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from custom_components.spook.repairs import (
    AbstractSpookEntityComponentUnknownReferencesRepair,
)

if TYPE_CHECKING:
    from custom_components.spook.repairs import InspectedEntity


class PidRecordingRepair(AbstractSpookEntityComponentUnknownReferencesRepair):
    """A repair whose only finding is the process its computation ran in.

    Its cached state is a set of strings, plain data, so its rounds may
    run in the worker process — where the recorded ID is the worker's,
    which is exactly what the tests about the worker process read back.
    """

    domain = "automation"
    repair = "test_pid_recording"
    entity_label = "automation"
    reference_label = "entities"
    edit_url_pattern = "/config/automation/edit/{unique_id}"

    computation_context_attrs = ("_known_ids",)

    _known_ids: set[str]

    async def _async_setup_inspection(self) -> None:
        """Cache nothing worth knowing, so every round reports its process."""
        self._known_ids = set()

    def _compute_unknown_references(self, _entity: InspectedEntity) -> set[str]:
        """Return this process's ID, as an unknown reference."""
        return {str(os.getpid())} - self._known_ids
