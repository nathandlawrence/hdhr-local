"""Calendar: upcoming DVR recordings (SiliconDust cloud schedule), padded by each rule's start/end padding."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import HdhrCoordinator
from .entity import HdhrEntity
from .sensor import schedule_dvr


def to_event(s: dict) -> CalendarEvent:
    title = s["title"] + (f" - {s['episode']}" if s.get("episode") else "")
    ch = f"ch {s['channel']}" + (f" {s['channel_name']}" if s.get("channel_name") else "")
    return CalendarEvent(start=dt_util.utc_from_timestamp(s["start"]), end=dt_util.utc_from_timestamp(s["end"]),
                         summary=title, description=s.get("synopsis") or None, location=ch)


class RecordingsCalendar(HdhrEntity, CalendarEntity):
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id, "recordings_calendar", "Recordings")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.schedule is not None

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow().timestamp()
        s = next((s for s in self.coordinator.schedule or [] if s["end"] > now), None)
        return to_event(s) if s else None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        a, b = start_date.timestamp(), end_date.timestamp()
        return [to_event(s) for s in self.coordinator.schedule or [] if s["end"] > a and s["start"] < b]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HdhrCoordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def add(dev_ids) -> None:
        sd = schedule_dvr(coordinator)
        if sd in dev_ids and sd not in added:
            added.add(sd)
            async_add_entities([RecordingsCalendar(coordinator, sd)])

    add(list(coordinator.data["devices"]))
    coordinator.new_device_callbacks.append(add)
    entry.async_on_unload(lambda: coordinator.new_device_callbacks.remove(add))
