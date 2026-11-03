"""Switch: pick the best feed for simulcast recordings - on by default; off stops the feed tests
and leaves every recording rule as it is."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HdhrCoordinator
from .entity import HdhrEntity
from .sensor import schedule_dvr


class PickBestFeed(HdhrEntity, SwitchEntity):
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id, "pick_best_feed", "Pick best feed")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.feed.get("enabled", True))

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_feed_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_feed_enabled(False)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HdhrCoordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def add(dev_ids) -> None:
        sd = schedule_dvr(coordinator)
        if sd in dev_ids and sd not in added:
            added.add(sd)
            async_add_entities([PickBestFeed(coordinator, sd)])

    add(list(coordinator.data["devices"]))
    coordinator.new_device_callbacks.append(add)
    entry.async_on_unload(lambda: coordinator.new_device_callbacks.remove(add))
