"""Polls every HDHomeRun tuner and DVR engine on the network (works with HDHomeRun; not made by SiliconDust).

Also keeps: the running error tally per tuner, the TVs/players (named in the options; freeze reports arrive on the
integration's webhook from an external freeze detector, e.g. a script watching the player), and the recording schedule from
SiliconDust's cloud (refreshed with each device rescan).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import hdhr_api
from .const import CONF_CLIENT_NAMES, CONF_DEVICE_NAMES, CONF_HOSTS, DOMAIN, POLL_SECONDS, SCAN_EVERY_POLLS

_LOGGER = logging.getLogger(__name__)
KEEP_FREEZES = 500


def player_id(name: str) -> str:
    """Players are keyed by NAME, so one TV with two addresses (wired + Wi-Fi) is one device. No underscores."""
    return "player-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unnamed")


class HdhrCoordinator(DataUpdateCoordinator[dict]):
    """One snapshot of every device every POLL_SECONDS; rediscovers devices every SCAN_EVERY_POLLS polls."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=POLL_SECONDS),
                         config_entry=entry)
        self.network = hdhr_api.Network(entry.data.get(CONF_HOSTS, []))
        self._polls = 0
        self.new_device_callbacks: list = []
        self.new_player_callbacks: list = []
        # Running error tally per tuner ("<device id>/<n>"): restored by the error-total sensors after a restart,
        # zeroed by the Reset error totals button.
        self.err_totals: dict[str, int] = {}
        self.err_since: dict[str, str] = {}
        self.err_restored: set[str] = set()      # tallies whose restore step has run
        # Recording schedule (cloud). None until the first successful fetch.
        self.schedule: list[dict] | None = None
        self.schedule_error: str | None = None
        self.schedule_at: float = 0.0
        # Guide (cloud, ~10 hours ahead): {guide number: {"name", "programs": [...]}}
        self.guide: dict = {}
        self.guide_at: float = 0.0
        # Freeze detector reports per player id: {"freezes": [iso, ...], "frozen": bool, "detector": True, ...}
        self.detected: dict[str, dict] = {}
        self._store = Store(hass, 1, f"{DOMAIN}.players")
        # Best feed for simulcasts: feed-test results per channel, rules this integration pinned, last decisions.
        self.feed: dict = {"enabled": True, "samples": {}, "errors": {}, "pinned": {}, "decisions": {}}
        self._feed_store = Store(hass, 1, f"{DOMAIN}.feed")
        self._feed_busy = False
        self.sigtest = None              # a running SignalTest (sigtest.py)
        self.sigtest_last: dict | None = None   # the last finished one's result

    async def async_load(self) -> None:
        self.detected = (await self._store.async_load() or {}).get("players", {})
        self.feed.update(await self._feed_store.async_load() or {})

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"players": self.detected}, 5)

    # --- names ---------------------------------------------------------------------------------------------------
    @property
    def device_names(self) -> dict:
        return self.config_entry.options.get(CONF_DEVICE_NAMES, {})

    @property
    def client_names(self) -> dict:
        return self.config_entry.options.get(CONF_CLIENT_NAMES, {})

    def device_name(self, dev_id: str) -> str:
        info = (self.data or {}).get("devices", {}).get(dev_id, {})
        return self.device_names.get(dev_id) or info.get("label") or f"HDHomeRun {dev_id}"

    def client_name(self, ip: str) -> str:
        return self.client_names.get(ip, ip)

    # --- players -------------------------------------------------------------------------------------------------
    def players(self) -> dict[str, dict]:
        """{player id: {"name", "ips"}} for every NAMED player plus any player a freeze detector reported."""
        out: dict[str, dict] = {}
        for ip, name in self.client_names.items():
            out.setdefault(player_id(name), {"name": name, "ips": set()})["ips"].add(ip)
        for pid, d in self.detected.items():
            for ip in d.get("ips", []):
                name = self.client_name(ip)
                out.setdefault(player_id(name), {"name": name, "ips": set()})["ips"].add(ip)
        return out

    def player_state(self, pid: str) -> dict:
        p = self.players().get(pid, {"ips": set()})
        lineup = (self.data or {}).get("lineup", {})
        sess = [s for s in (self.data or {}).get("sessions", []) if s[2] in p["ips"]]
        playing = bool(sess)
        ch = next((s[1] for s in sess if s[1]), None)
        channel = (f"{ch} {lineup.get(ch, '')}".strip() if ch else ("recording" if playing else "idle"))
        det = self.detected.get(pid, {})
        freezes = det.get("freezes", [])
        today = dt_util.now().date()
        n_today = sum(1 for f in freezes if (t := dt_util.parse_datetime(f)) and dt_util.as_local(t).date() == today)
        return {"playing": playing, "channel": channel, "detector": bool(det.get("detector")),
                "frozen": bool(det.get("frozen")) and playing, "freezes_today": n_today,
                "last_freeze": dt_util.parse_datetime(freezes[-1]) if freezes else None,
                "freeze_detail": det.get("detail"), "program": self.program_on(ch) if playing else None}

    def program_on(self, channel: str | None) -> dict | None:
        """What the guide says is on `channel` right now."""
        return hdhr_api.now_playing(self.guide, channel, time.time())

    @callback
    def player_event(self, body: dict) -> bool:
        """A freeze detector's report: {"player": ip, "event": "freeze"|"recovered"|"ping", "detail": str}."""
        ip, event = str(body.get("player", "")).split(":")[0], body.get("event")
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) or event not in ("freeze", "recovered", "ping"):
            return False
        pid = player_id(self.client_name(ip))
        new = pid not in self.detected
        d = self.detected.setdefault(pid, {"freezes": [], "frozen": False})
        d["detector"], d["ips"] = True, sorted(set(d.get("ips", [])) | {ip})
        d["seen"] = dt_util.utcnow().isoformat(timespec="seconds")
        if event == "freeze":
            d["frozen"] = True
            d["freezes"] = (d["freezes"] + [d["seen"]])[-KEEP_FREEZES:]
            d["detail"] = str(body.get("detail", ""))[:250]
        elif event == "recovered":
            d["frozen"] = False
        self._save()
        if new:
            for cb in list(self.new_player_callbacks):
                cb([pid])
        self.async_update_listeners()
        return True

    # --- tuner error tally ---------------------------------------------------------------------------------------
    def _accumulate(self, data: dict) -> None:
        now = dt_util.utcnow().isoformat(timespec="seconds")
        for dev_id, info in data["devices"].items():
            if info.get("kind") != "tuner" or not info.get("ok"):
                continue
            for n in range(info.get("count", 0)):
                k = f"{dev_id}/{n}"
                self.err_totals[k] = self.err_totals.get(k, 0) + int((info["state"].get(f"t{n}") or {}).get("new_err") or 0)
                self.err_since.setdefault(k, now)

    def reset_errors(self, dev_id: str) -> None:
        now = dt_util.utcnow().isoformat(timespec="seconds")
        for k in [k for k in self.err_totals if k.startswith(dev_id + "/")]:
            self.err_totals[k], self.err_since[k] = 0, now
        self.async_update_listeners()

    async def async_quick_sessions(self) -> None:
        """Refresh only who-is-watching-what (DVR engines + tuners, fast) for hdhr.tune's confirmation loop."""
        try:
            fresh = await self.hass.async_add_executor_job(self.network.quick_sessions)
        except (OSError, ValueError):
            return
        if self.data is not None:
            self.data["sessions"] = fresh

    # --- best feed for simulcasts ---------------------------------------------------------------------------------
    FEED_KEEP_S = 7 * 86400          # results older than a week drop out
    FEED_FAST_S = 600                # until a channel has enough results: one test per 10-minute cycle
    FEED_SLOW_S = 3600               # after that: one an hour
    FEED_QUIET_S = 300               # no tests within 5 minutes of a scheduled recording

    def feed_groups(self) -> dict[str, dict]:
        """{series id: {"title", "channels", "current"}} for simulcast series this integration may decide: the rule
        has no channel limit, or the limit is the one this integration set (manual choices are left alone)."""
        out: dict[str, dict] = {}
        for s in self.schedule or []:
            if len(s.get("airs_on", [])) < 2:
                continue
            sid, pin = s["series_id"], self.feed["pinned"].get(s["series_id"])
            rc = s.get("rule_channels")
            if rc is not None and rc != [pin]:
                continue                     # someone limited this rule by hand: theirs to decide
            g = out.setdefault(sid, {"title": s["title"], "channels": set(), "current": pin if rc == [pin] else None})
            g["channels"] |= set(s["airs_on"])
        for g in out.values():
            g["channels"] = sorted(g["channels"], key=hdhr_api.channel_key)
        return out

    def _due(self, ch: str, now: float) -> bool:
        samples = self.feed["samples"].get(ch, [])
        last = samples[-1][0] if samples else 0
        wait = self.FEED_FAST_S if len(samples) < hdhr_api.FEED_MIN_SAMPLES else self.FEED_SLOW_S
        return now - last >= wait - 30

    async def async_feed_cycle(self, *_args) -> None:
        """Every 10 minutes: test due simulcast channels on free tuners, then decide and pin. Never overlaps itself."""
        if self._feed_busy or not self.feed.get("enabled", True) or self.schedule is None or self.sigtest:
            return                           # (a signal test owns the spare tuners while it runs)
        self._feed_busy = True
        try:
            now = time.time()
            if any(0 <= s["start"] - now <= self.FEED_QUIET_S for s in self.schedule):
                return
            groups = self.feed_groups()
            due = [c for c in sorted({c for g in groups.values() for c in g["channels"]}, key=hdhr_api.channel_key)
                   if self._due(c, now)][:2]
            for ch in due:
                res = await self.hass.async_add_executor_job(self.network.probe, ch)
                if res is None:
                    break                    # not enough free tuners right now: try next cycle
                t = time.time()
                samples = [s for s in self.feed["samples"].get(ch, []) if t - s[0] < self.FEED_KEEP_S]
                self.feed["samples"][ch] = (samples + [[round(t), bool(res["ok"])]])[-300:]
                if not res["ok"]:
                    self.feed["errors"][ch] = {"at": dt_util.utcnow().isoformat(timespec="seconds"), "error": res["error"]}
                _LOGGER.info("feed test %s: %s (%s bytes, %s s)", ch, "ok" if res["ok"] else res["error"],
                             res["bytes"], res["secs"])
            changed = False
            for sid, g in groups.items():
                choice, why = hdhr_api.choose_feed(g["channels"], self.feed["samples"], g["current"])
                if choice and choice != g["current"]:
                    try:
                        await self.hass.async_add_executor_job(self.network.pin_rule, sid, choice)
                        self.feed["pinned"][sid] = choice
                        changed = True
                        _LOGGER.warning("best feed: %s now records from %s (%s)", g["title"], choice, why)
                    except (OSError, ValueError) as e:
                        why = f"could not pin {choice}: {e}"
                self.feed["decisions"][sid] = {"show": g["title"], "channels": g["channels"],
                                               "recording_from": self.feed["pinned"].get(sid), "why": why,
                                               "at": dt_util.utcnow().isoformat(timespec="seconds")}
            self._feed_store.async_delay_save(lambda: self.feed, 5)
            if changed:
                await self.async_refresh_schedule()
            self.async_update_listeners()
        finally:
            self._feed_busy = False

    @callback
    def set_feed_enabled(self, on: bool) -> None:
        self.feed["enabled"] = on
        self._feed_store.async_delay_save(lambda: self.feed, 1)
        self.async_update_listeners()

    # --- schedule ------------------------------------------------------------------------------------------------
    async def async_refresh_schedule(self) -> None:
        """Fetch the schedule now (after hdhr.record added a rule) instead of waiting for the 10-minute rescan."""
        try:
            self.schedule = await self.hass.async_add_executor_job(self.network.schedule)
            self.schedule_error, self.schedule_at = None, time.time()
        except Exception as e:  # noqa: BLE001
            self.schedule_error = f"{type(e).__name__}: {e}"[:200]
        self.async_update_listeners()

    def conflicts(self) -> tuple[int, list[dict]]:
        tuners = (self.data or {}).get("tuner_count", 0)
        return hdhr_api.conflicts(self.schedule or [], tuners)

    # --- guide ---------------------------------------------------------------------------------------------------
    GUIDE_EVERY_S = 6 * 3600         # the full 2-week guide (one XMLTV download) is refreshed every 6 hours

    async def async_refresh_guide(self, *_args) -> None:
        """Fetch the whole guide in the background. Keeps the last good one on failure."""
        try:
            guide = await self.hass.async_add_executor_job(self.network.guide)
        except Exception as e:  # noqa: BLE001 - the cloud is optional
            _LOGGER.warning("guide (cloud) not updated: %s", f"{type(e).__name__}: {e}"[:200])
            return
        if guide:
            self.guide, self.guide_at = guide, time.time()
            n = sum(len(g["programs"]) for g in guide.values())
            _LOGGER.info("guide: %d channels, %d programs", len(guide), n)
            self.async_update_listeners()

    def guide_days(self) -> float:
        ends = [g["programs"][-1]["e"] for g in self.guide.values() if g["programs"]]
        return round((max(ends) - time.time()) / 86400, 1) if ends else 0.0

    # --- polling -------------------------------------------------------------------------------------------------
    def _work(self) -> tuple[dict, list, list | None, str | None]:
        new, sched, err = [], None, None
        if self._polls % SCAN_EVERY_POLLS == 0:
            new = self.network.scan()
            try:
                sched = self.network.schedule()
            except Exception as e:  # noqa: BLE001 - the cloud is optional; keep the last good schedule
                err = f"{type(e).__name__}: {e}"[:200]
        self._polls += 1
        return self.network.poll(self.client_name), new, sched, err

    async def _async_update_data(self) -> dict:
        try:
            data, new, sched, err = await self.hass.async_add_executor_job(self._work)
        except Exception as err:  # noqa: BLE001 - surface any device failure as an unavailable update
            raise UpdateFailed(str(err)) from err
        if not data["devices"]:
            raise UpdateFailed("no HDHomeRun devices found yet")
        self._accumulate(data)
        if self.sigtest is not None:
            self.sigtest.tick(data)
        if sched is not None:
            self.schedule, self.schedule_error, self.schedule_at = sched, None, time.time()
        elif err:
            self.schedule_error = err
            _LOGGER.warning("recording schedule (cloud) not updated: %s", err)
        if new:
            for cb in list(self.new_device_callbacks):
                self.hass.loop.call_soon(cb, [d.id for d in new])
        return data
