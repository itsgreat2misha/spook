"""Tests for which config entry changes have a repair look again."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntryChange,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.spook.repairs import AbstractSpookRepair

# The first look on activating, then one per change worth looking at.
ONE_MORE_LOOK = 2
TWO_MORE_LOOKS = 3

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory

    from homeassistant.core import HomeAssistant


class _UnscopedRepair(AbstractSpookRepair):
    """Repair that looks again on a change to any config entry."""

    domain = "mock"
    repair = "mock_unscoped_repair"
    inspect_config_entry_changed = True

    inspections = 0

    async def async_inspect(self) -> None:
        """Count the inspection."""
        self.inspections += 1


class _ScopedRepair(AbstractSpookRepair):
    """Repair that looks again on a change to a template entry."""

    domain = "mock"
    repair = "mock_scoped_repair"
    inspect_config_entry_changed = "template"

    inspections = 0

    async def async_inspect(self) -> None:
        """Count the inspection."""
        self.inspections += 1


async def _settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let the inspection debouncer run whatever it has waiting."""
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _activated(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    repair: _UnscopedRepair | _ScopedRepair,
) -> _UnscopedRepair | _ScopedRepair:
    """Return an activated repair past its first look."""
    await repair.async_activate()
    await _settle(hass, freezer)
    assert repair.inspections == 1
    return repair


async def test_entry_update_does_not_have_unscoped_repair_look_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test an entry writing its data does not trigger a look.

    Integrations refresh what they store all day long, refreshed
    credentials being the common case. None of it can change what an
    automation, script or dashboard references, so an unscoped repair
    lets it go instead of going through the whole house again.
    """
    entry = MockConfigEntry(domain="wallbox", data={"token": "stale"})
    entry.add_to_hass(hass)
    repair = await _activated(hass, freezer, _UnscopedRepair(hass))

    hass.config_entries.async_update_entry(entry, data={"token": "fresh"})
    await _settle(hass, freezer)

    assert repair.inspections == 1
    await repair.async_deactivate()


async def test_entry_added_and_removed_have_unscoped_repair_look_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test an entry arriving or going still triggers a look."""
    entry = MockConfigEntry(domain="wallbox")
    entry.add_to_hass(hass)
    repair = await _activated(hass, freezer, _UnscopedRepair(hass))

    async_dispatcher_send(
        hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.ADDED, entry
    )
    await _settle(hass, freezer)
    assert repair.inspections == ONE_MORE_LOOK

    async_dispatcher_send(
        hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.REMOVED, entry
    )
    await _settle(hass, freezer)
    assert repair.inspections == TWO_MORE_LOOKS
    await repair.async_deactivate()


async def test_entry_update_still_has_scoped_repair_look_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a scoped repair still looks when its own kind of entry changes.

    What a template helper is lives in its entry, so an update to one is
    the repair's whole subject changing, not churn to let go.
    """
    entry = MockConfigEntry(domain="template", options={"name": "Before"})
    entry.add_to_hass(hass)
    repair = await _activated(hass, freezer, _ScopedRepair(hass))

    hass.config_entries.async_update_entry(entry, options={"name": "After"})
    await _settle(hass, freezer)

    assert repair.inspections == ONE_MORE_LOOK
    await repair.async_deactivate()


async def test_entry_update_does_not_have_scoped_repair_look_at_others(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a scoped repair lets an update to another kind of entry go."""
    entry = MockConfigEntry(domain="wallbox", data={"token": "stale"})
    entry.add_to_hass(hass)
    repair = await _activated(hass, freezer, _ScopedRepair(hass))

    hass.config_entries.async_update_entry(entry, data={"token": "fresh"})
    await _settle(hass, freezer)

    assert repair.inspections == 1
    await repair.async_deactivate()
