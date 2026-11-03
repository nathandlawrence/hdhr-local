"""Buttons: reset a tuner device's running error totals. (Touches nothing on the device itself.)"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HdhrCoordinator
from .entity import HdhrEntity


class HdhrResetErrors(HdhrEntity, ButtonEntity):
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id, "reset_errors", "Reset error totals")

    @property
    def available(self) -> bool:
        return True       # resetting the tally works even while the device is offline

    async def async_press(self) -> None:
        self.coordinator.reset_errors(self.dev_id)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HdhrCoordinator = entry.runtime_data

    @callback
    def add(dev_ids) -> None:
        devices = coordinator.data["devices"]
        async_add_entities(HdhrResetErrors(coordinator, d) for d in dev_ids if devices.get(d, {}).get("kind") == "tuner")

    add(list(coordinator.data["devices"]))
    coordinator.new_device_callbacks.append(add)
    entry.async_on_unload(lambda: coordinator.new_device_callbacks.remove(add))
