"""Base entity for HDHR Local."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HdhrCoordinator


class HdhrEntity(CoordinatorEntity[HdhrCoordinator]):
    """An entity of one HDHomeRun device (tuner or DVR engine)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str, key: str, name: str) -> None:
        super().__init__(coordinator)
        self.dev_id, self.key = dev_id, key
        self._attr_unique_id = f"{dev_id}_{key}"
        self._attr_name = name
        info = coordinator.data["devices"][dev_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev_id)},
            name=coordinator.device_name(dev_id),
            manufacturer="SiliconDust",
            model=info.get("model"),
            sw_version=info.get("firmware"),
            configuration_url=info.get("base"),
        )

    @property
    def dev(self) -> dict:
        return (self.coordinator.data or {}).get("devices", {}).get(self.dev_id, {})

    @property
    def st(self) -> dict:
        return self.dev.get("state") or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.dev.get("ok"))


class PlayerEntity(CoordinatorEntity[HdhrCoordinator]):
    """An entity of one TV / player (named in the options; keyed by name so two addresses make one device)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HdhrCoordinator, pid: str, key: str, name: str) -> None:
        super().__init__(coordinator)
        self.pid, self.key = pid, key
        self._attr_unique_id = f"{pid}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pid)}, name=coordinator.players()[pid]["name"],
            manufacturer="HDHR Local", model="TV / player")

    @property
    def ps(self) -> dict:
        return self.coordinator.player_state(self.pid)

    @property
    def available(self) -> bool:
        return super().available and self.pid in self.coordinator.players()
