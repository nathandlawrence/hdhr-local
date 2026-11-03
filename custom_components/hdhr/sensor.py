"""Sensors: per tuner (channel, viewer, signal, errors, bitrate), per DVR engine (space, feeds, recordings)."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfDataRate, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import hdhr_api
from .coordinator import HdhrCoordinator
from .entity import HdhrEntity, PlayerEntity


@dataclass(frozen=True)
class Spec:
    key: str
    name: str
    value: Callable[[dict], object]
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    category: EntityCategory | None = None
    attrs: Callable[[dict], dict] | None = None


def tuner_specs(count: int) -> list[Spec]:
    specs = [Spec("tuners_in_use", "Tuners in use", lambda s: s.get("in_use"), state_class=SensorStateClass.MEASUREMENT,
                  icon="mdi:antenna")]
    for n in range(count):
        t = f"t{n}"
        g = lambda k, t=t: (lambda s: (s.get(t) or {}).get(k))  # noqa: E731
        specs += [
            Spec(f"tuner_{n}_channel", f"Tuner {n} channel", g("channel"), icon="mdi:television-classic",
                 attrs=lambda s, t=t: {"frequency_mhz": (s.get(t) or {}).get("freq_mhz"),
                                       "virtual_channel": (s.get(t) or {}).get("vct")}),
            Spec(f"tuner_{n}_viewer", f"Tuner {n} viewer", g("viewer"), icon="mdi:account-eye"),
            Spec(f"tuner_{n}_signal_strength", f"Tuner {n} signal strength", g("ss"), PERCENTAGE,
                 state_class=SensorStateClass.MEASUREMENT, icon="mdi:signal"),
            Spec(f"tuner_{n}_signal_quality", f"Tuner {n} signal quality", g("snq"), PERCENTAGE,
                 state_class=SensorStateClass.MEASUREMENT, icon="mdi:signal-variant"),
            Spec(f"tuner_{n}_symbol_quality", f"Tuner {n} symbol quality", g("seq"), PERCENTAGE,
                 state_class=SensorStateClass.MEASUREMENT, icon="mdi:sine-wave", category=EntityCategory.DIAGNOSTIC),
            Spec(f"tuner_{n}_errors", f"Tuner {n} stream errors", g("epm"), "errors/min",
                 state_class=SensorStateClass.MEASUREMENT, icon="mdi:alert-circle-outline"),
            Spec(f"tuner_{n}_bitrate", f"Tuner {n} bitrate", g("mbps"), UnitOfDataRate.MEGABITS_PER_SECOND,
                 SensorDeviceClass.DATA_RATE, SensorStateClass.MEASUREMENT, category=EntityCategory.DIAGNOSTIC),
        ]
    return specs


DVR_SPECS = [
    Spec("free_space", "Free space", lambda s: s.get("free_gb"), UnitOfInformation.GIGABYTES,
         SensorDeviceClass.DATA_SIZE, SensorStateClass.MEASUREMENT, icon="mdi:harddisk"),
    Spec("used", "Disk used", lambda s: s.get("used_pct"), PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
         icon="mdi:chart-donut"),
    Spec("live_streams", "Live streams", lambda s: s.get("live"), state_class=SensorStateClass.MEASUREMENT,
         icon="mdi:television-play", attrs=lambda s: {"streams": s.get("live_list", [])}),
    Spec("recordings", "Recordings", lambda s: s.get("recordings"), state_class=SensorStateClass.MEASUREMENT,
         icon="mdi:filmstrip-box-multiple"),
    Spec("last_recording", "Last recording", lambda s: s.get("last"), icon="mdi:history",
         attrs=lambda s: s.get("last_attrs") or {}),
]


def specs_for(info: dict) -> list[Spec]:
    return tuner_specs(info.get("count", 0)) if info.get("kind") == "tuner" else DVR_SPECS


class HdhrSensor(HdhrEntity, SensorEntity):
    def __init__(self, coordinator: HdhrCoordinator, dev_id: str, spec: Spec) -> None:
        super().__init__(coordinator, dev_id, spec.key, spec.name)
        self.spec = spec
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        self._attr_icon = spec.icon
        self._attr_entity_category = spec.category

    @property
    def native_value(self):
        v = self.spec.value(self.st)
        return v[:255] if isinstance(v, str) else v

    @property
    def extra_state_attributes(self):
        return self.spec.attrs(self.st) if self.spec.attrs else None


class HdhrErrorTotal(HdhrEntity, RestoreSensor):
    """Running error tally (te + crc) of one tuner, or of the whole device when n is None. Survives restarts;
    the device's Reset error totals button zeroes it."""

    _attr_native_unit_of_measurement = "errors"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str, n: int | None) -> None:
        key, name = ("error_total", "Error total") if n is None else (f"tuner_{n}_error_total", f"Tuner {n} error total")
        super().__init__(coordinator, dev_id, key, name)
        self.n = n

    def _keys(self) -> list[str]:
        if self.n is not None:
            return [f"{self.dev_id}/{self.n}"]
        return [k for k in self.coordinator.err_totals if k.startswith(self.dev_id + "/")]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.n is None:
            return
        k = self._keys()[0]
        last, last_state = await self.async_get_last_sensor_data(), await self.async_get_last_state()
        if last is not None and last.native_value not in (None, "unknown", "unavailable"):
            try:
                self.coordinator.err_totals[k] = int(float(last.native_value)) + self.coordinator.err_totals.get(k, 0)
            except (TypeError, ValueError):
                pass
            if last_state is not None and last_state.attributes.get("since"):
                self.coordinator.err_since[k] = last_state.attributes["since"]
        self.coordinator.err_restored.add(k)
        self.coordinator.async_update_listeners()       # lets the device total report now

    @property
    def native_value(self) -> int | None:
        keys = self._keys()
        if self.n is None and not all(k in self.coordinator.err_restored for k in keys):
            return None   # device total waits for every tuner's restored tally (a 0 blip would skew statistics)
        return sum(self.coordinator.err_totals.get(k, 0) for k in keys)

    @property
    def extra_state_attributes(self):
        since = sorted(self.coordinator.err_since.get(k) for k in self._keys() if self.coordinator.err_since.get(k))
        return {"since": since[0] if since else None}


# --- TVs / players ---------------------------------------------------------------------------------------------

class PlayerChannel(PlayerEntity, SensorEntity):
    _attr_icon = "mdi:television-classic"

    def __init__(self, coordinator, pid):
        super().__init__(coordinator, pid, "channel", "Channel")

    @property
    def native_value(self):
        return self.ps["channel"]


def _program_attrs(p: dict | None, channel: str | None) -> dict:
    if not p:
        return {"channel": channel}
    loc = lambda t: dt_util.as_local(dt_util.utc_from_timestamp(t)).isoformat(timespec="minutes")  # noqa: E731
    return {"channel": channel, "episode": p.get("ep") or None, "starts": loc(p["s"]), "ends": loc(p["e"]),
            "synopsis": p.get("syn") or None}


class PlayerNowPlaying(PlayerEntity, SensorEntity):
    """The show on this TV right now, from the guide. The title is the STATE, so voice assistants can read it."""

    _attr_icon = "mdi:television-guide"

    def __init__(self, coordinator, pid):
        super().__init__(coordinator, pid, "now_playing", "Now playing")

    @property
    def native_value(self):
        ps = self.ps
        if not ps["playing"]:
            return "Nothing"
        p = ps.get("program")
        return (p["t"] if p else ps["channel"])[:255]

    @property
    def extra_state_attributes(self):
        return _program_attrs(self.ps.get("program"), self.ps["channel"])


class TunerProgram(HdhrEntity, SensorEntity):
    """The show on a tuner's channel right now, from the guide."""

    _attr_icon = "mdi:television-guide"

    def __init__(self, coordinator, dev_id, n):
        super().__init__(coordinator, dev_id, f"tuner_{n}_program", f"Tuner {n} program")
        self.n = n

    def _vct(self):
        return (self.st.get(f"t{self.n}") or {}).get("vct")

    @property
    def native_value(self):
        vct = self._vct()
        if not vct:
            return "idle"
        p = self.coordinator.program_on(vct)
        return (p["t"] if p else vct)[:255]

    @property
    def extra_state_attributes(self):
        vct = self._vct()
        return _program_attrs(self.coordinator.program_on(vct) if vct else None, vct)


class PlayerFreezesToday(PlayerEntity, SensorEntity):
    _attr_icon = "mdi:snowflake-alert"
    _attr_native_unit_of_measurement = "freezes"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, pid):
        super().__init__(coordinator, pid, "freezes_today", "Freezes today")

    @property
    def native_value(self):
        return self.ps["freezes_today"]


class PlayerLastFreeze(PlayerEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:snowflake-alert"

    def __init__(self, coordinator, pid):
        super().__init__(coordinator, pid, "last_freeze", "Last freeze")

    @property
    def native_value(self):
        return self.ps["last_freeze"]

    @property
    def extra_state_attributes(self):
        return {"detail": self.ps.get("freeze_detail")}


def player_sensors(coordinator, pid) -> list:
    ents = [PlayerChannel(coordinator, pid), PlayerNowPlaying(coordinator, pid)]
    if coordinator.detected.get(pid, {}).get("detector"):     # only players a freeze detector reports on
        ents += [PlayerFreezesToday(coordinator, pid), PlayerLastFreeze(coordinator, pid)]
    return ents


# --- recording schedule (cloud) ---------------------------------------------------------------------------------

class ScheduleSensor(HdhrEntity, SensorEntity):
    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.schedule is not None


class NextRecording(ScheduleSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:record-rec"

    def __init__(self, coordinator, dev_id):
        super().__init__(coordinator, dev_id, "next_recording", "Next recording")

    def _next(self):
        now = time.time()
        return next((s for s in self.coordinator.schedule or [] if s["start"] > now), None)

    @property
    def native_value(self):
        s = self._next()
        return dt_util.utc_from_timestamp(s["start"]) if s else None

    @property
    def extra_state_attributes(self):
        s = self._next()
        if not s:
            return {"title": None}
        return {"title": s["title"], "episode": s["episode"], "channel": s["channel"],
                "channel_name": s["channel_name"], "ends": dt_util.utc_from_timestamp(s["end"]).isoformat()}


class UpcomingRecordings(ScheduleSensor):
    _attr_icon = "mdi:calendar-clock"
    _attr_native_unit_of_measurement = "recordings"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, dev_id):
        super().__init__(coordinator, dev_id, "upcoming_recordings", "Upcoming recordings")

    @property
    def native_value(self):
        return len(self.coordinator.schedule or [])

    @property
    def extra_state_attributes(self):
        return {"window_days": 14, "fetched": dt_util.utc_from_timestamp(self.coordinator.schedule_at).isoformat()
                if self.coordinator.schedule_at else None, "cloud_error": self.coordinator.schedule_error}


class PeakTuners(ScheduleSensor):
    _attr_icon = "mdi:antenna"
    _attr_native_unit_of_measurement = "tuners"

    def __init__(self, coordinator, dev_id):
        super().__init__(coordinator, dev_id, "peak_tuners", "Peak tuners needed")

    @property
    def native_value(self):
        return self.coordinator.conflicts()[0]

    @property
    def extra_state_attributes(self):
        return {"tuners": (self.coordinator.data or {}).get("tuner_count")}


class FeedReliability(HdhrEntity, SensorEntity):
    """How often each simulcast channel actually delivered video in the feed tests (last 7 days)."""

    _attr_icon = "mdi:chart-bar"
    _attr_native_unit_of_measurement = "channels"

    def __init__(self, coordinator, dev_id):
        super().__init__(coordinator, dev_id, "feed_reliability", "Feed reliability")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return len(self.coordinator.feed["samples"])

    @property
    def extra_state_attributes(self):
        out = {}
        for ch in sorted(self.coordinator.feed["samples"], key=hdhr_api.channel_key):
            s = hdhr_api.feed_stats(self.coordinator.feed["samples"][ch])
            out[ch] = f"{s['ok']}/{s['tries']} delivered" + (f" ({round(100 * s['rate'])}%)" if s["tries"] else "")
        out["last_failures"] = self.coordinator.feed.get("errors", {})
        out["needed_per_channel"] = hdhr_api.FEED_MIN_SAMPLES
        return out


class FeedChoices(HdhrEntity, SensorEntity):
    """Simulcast shows and which channel each records from (chosen from the feed-test data), with the numbers."""

    _attr_icon = "mdi:tune-variant"
    _attr_native_unit_of_measurement = "shows"

    def __init__(self, coordinator, dev_id):
        super().__init__(coordinator, dev_id, "feed_choices", "Feed choices")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return len(self.coordinator.feed["pinned"])

    @property
    def extra_state_attributes(self):
        return {"decisions": list(self.coordinator.feed.get("decisions", {}).values()),
                "enabled": self.coordinator.feed.get("enabled", True)}


def schedule_dvr(coordinator) -> str | None:
    """The schedule is per account, so it hangs off the first DVR engine."""
    dvrs = sorted(d for d, i in (coordinator.data or {}).get("devices", {}).items() if i.get("kind") == "dvr")
    return dvrs[0] if dvrs else None


class SignalTestSensor(HdhrEntity, SensorEntity):
    """The interference test (sigtest.py): idle / running (dryer on|off) / the last result, with the numbers."""

    _attr_icon = "mdi:sine-wave"

    def __init__(self, coordinator: HdhrCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id, "signal_test", "Signal test")

    @property
    def native_value(self) -> str:
        t = self.coordinator.sigtest
        if t is not None:
            return t.state()
        last = self.coordinator.sigtest_last
        return "idle" if not last else f"done ({last['ended']})"[:255]

    @property
    def extra_state_attributes(self):
        t = self.coordinator.sigtest
        return t.result() if t is not None else (self.coordinator.sigtest_last or {})


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
        ents: list = [HdhrSensor(coordinator, d, s) for d in dev_ids if d in devices for s in specs_for(devices[d])]
        for d in dev_ids:
            if devices.get(d, {}).get("kind") == "tuner":
                ents += [HdhrErrorTotal(coordinator, d, n) for n in range(devices[d].get("count", 0))]
                ents += [TunerProgram(coordinator, d, n) for n in range(devices[d].get("count", 0))]
                ents.append(HdhrErrorTotal(coordinator, d, None))
                ents.append(SignalTestSensor(coordinator, d))
        sd = schedule_dvr(coordinator)
        if sd in dev_ids:
            ents += [NextRecording(coordinator, sd), UpcomingRecordings(coordinator, sd), PeakTuners(coordinator, sd),
                     FeedReliability(coordinator, sd), FeedChoices(coordinator, sd)]
        add_ents(ents)

    @callback
    def add_players(pids) -> None:
        add_ents([e for p in pids if p in coordinator.players() for e in player_sensors(coordinator, p)])

    add(list(coordinator.data["devices"]))
    add_players(list(coordinator.players()))
    coordinator.new_device_callbacks.append(add)
    coordinator.new_player_callbacks.append(add_players)
    entry.async_on_unload(lambda: coordinator.new_device_callbacks.remove(add))
    entry.async_on_unload(lambda: coordinator.new_player_callbacks.remove(add_players))
