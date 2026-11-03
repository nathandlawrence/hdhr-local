"""Binary sensors: DVR recording now + tuner conflict ahead; per TV / player: playing, frozen."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import HdhrCoordinator
from .entity import HdhrEntity, PlayerEntity
from .sensor import schedule_dvr


class HdhrRecording(HdhrEntity, BinarySensorEntity):
    _attr_icon = "mdi:record-rec"

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id, "recording", "Recording")

    @property
    def is_on(self) -> bool | None:
        v = self.st.get("recording")
        return None if v is None else bool(v)

    @property
    def extra_state_attributes(self):
        return {"sessions": self.st.get("recording_list", [])}


class TunerConflict(HdhrEntity, BinarySensorEntity):
    """On when, in the next 14 days, more recordings overlap than there are tuners (some would not record)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id, "tuner_conflict", "Tuner conflict")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.schedule is not None

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.conflicts()[1])

    @property
    def extra_state_attributes(self):
        peak, windows = self.coordinator.conflicts()
        loc = lambda t: dt_util.as_local(dt_util.utc_from_timestamp(t)).isoformat(timespec="minutes")  # noqa: E731
        return {"tuners": (self.coordinator.data or {}).get("tuner_count"), "peak_needed": peak,
                "conflicts": [{"start": loc(c["start"]), "end": loc(c["end"]), "needed": c["needed"],
                               "shows": c["shows"]} for c in windows[:20]]}


class PlayerPlaying(PlayerEntity, BinarySensorEntity):
    _attr_icon = "mdi:television-play"

    def __init__(self, coordinator, pid):
        super().__init__(coordinator, pid, "playing", "Playing")

    @property
    def is_on(self) -> bool:
        return self.ps["playing"]


class PlayerFrozen(PlayerEntity, BinarySensorEntity):
    """Reported by an external freeze detector (any script that watches the player); cleared on its recovery report or when play stops."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:snowflake-alert"

    def __init__(self, coordinator, pid):
        super().__init__(coordinator, pid, "frozen", "Frozen")

    @property
    def is_on(self) -> bool:
        return self.ps["frozen"]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HdhrCoordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_ents(ents) -> None:
        fresh = [e for e in ents if e.unique_id not in added]
        added.update(e.unique_id for e in fresh)
        if fresh:
            async_add_entities(fresh)

    @callback
    def add(dev_ids) -> None:
        devices = coordinator.data["devices"]
        ents: list = [HdhrRecording(coordinator, d) for d in dev_ids if devices.get(d, {}).get("kind") == "dvr"]
        sd = schedule_dvr(coordinator)
        if sd in dev_ids:
            ents.append(TunerConflict(coordinator, sd))
        add_ents(ents)

    @callback
    def add_players(pids) -> None:
        ents: list = []
        for p in pids:
            if p not in coordinator.players():
                continue
            ents.append(PlayerPlaying(coordinator, p))
            if coordinator.detected.get(p, {}).get("detector"):
                ents.append(PlayerFrozen(coordinator, p))
        add_ents(ents)

    add(list(coordinator.data["devices"]))
    add_players(list(coordinator.players()))
    coordinator.new_device_callbacks.append(add)
    coordinator.new_player_callbacks.append(add_players)
    entry.async_on_unload(lambda: coordinator.new_device_callbacks.remove(add))
    entry.async_on_unload(lambda: coordinator.new_player_callbacks.remove(add_players))
