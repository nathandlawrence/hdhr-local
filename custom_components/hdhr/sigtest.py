"""Interference test: does an appliance (e.g. a dryer) disturb reception while it runs?

While a watched entity is on (the dryer), spare tuners hold the test channels and every tuned channel's errors are
tallied per tuner; then the same for `after_minutes` once it is off (the baseline). Comparing VHF with UHF, and two
tuners on one channel, separates "the dryer's noise", "one band" and "one tuner".

Spare tuners only: a hold needs two free tuners (one is always left for a TV or a recording), nothing is held within
3 minutes of a scheduled recording, and the lowest-priority hold is dropped the moment no tuner is free. A channel a TV
is already on is measured from that TV's tuner instead of holding another one.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import hdhr_api

_LOGGER = logging.getLogger(__name__)

QUIET_S = 180            # hold nothing within 3 minutes of a scheduled recording
RETRY_S = 300            # a hold that failed (no tuner, stream ended) is retried after 5 minutes
MAX_BURSTS = 80
_now = time.time         # (tests swap this, not the global clock)
UHF_MHZ = 300            # below = VHF


def _clock(t: float) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(t)).strftime("%-I:%M %p").lower()


class SignalTest:
    def __init__(self, hass: HomeAssistant, co, name: str, watch_entity: str, channels: list[str],
                 after_minutes: float, max_hours: float, notify: str | None) -> None:
        self.hass, self.co, self.name, self.watch = hass, co, name, watch_entity
        self.channels = [str(c) for c in channels]
        self.after_s, self.max_s, self.notify = after_minutes * 60, max_hours * 3600, notify
        self.started = _now()
        self.phase = "on"
        self.phase_secs = {"on": 0.0, "after": 0.0}
        self._phase_at = self.started
        self._last_tick: float | None = None
        self.holds: dict[str, dict] = {}            # channel -> {"stop": Event, "thread": Thread, "result": [..]}
        self.notes: dict[str, str] = {}             # channel -> why it isn't held / how a hold ended
        self._retry_at: dict[str, float] = {}
        self.my_ip: str | None = None               # this machine as the tuner sees it (learned from the first hold)
        self.stats: dict[str, dict] = {}            # channel -> {"mhz", "on"/"after": {"errors", "tuner_s", "tuners"}}
        self.bursts: list[list] = []                # [time, channel, tuner, errors, phase]
        self._unsubs: list = []
        self._end_timer = None
        self.ended: str | None = None

    # --- lifecycle ---------------------------------------------------------------------------------------------
    def start(self) -> None:
        st = self.hass.states.get(self.watch)
        self.phase = "on" if st is not None and st.state == "on" else "after"
        if self.phase == "after":
            self._end_timer = async_call_later(self.hass, self.after_s, self._end_after)
        self._unsubs.append(async_track_state_change_event(self.hass, [self.watch], self._on_watch))
        _LOGGER.warning("signal test '%s' started (%s %s), channels %s", self.name, self.watch, self.phase,
                        ", ".join(self.channels))

    @callback
    def _on_watch(self, event: Event) -> None:
        new = event.data.get("new_state")
        on = new is not None and new.state == "on"
        if on and self.phase == "after":
            self._switch("on")
            if self._end_timer:
                self._end_timer()
                self._end_timer = None
        elif not on and self.phase == "on" and new is not None and new.state == "off":
            self._switch("after")
            self._end_timer = async_call_later(self.hass, self.after_s, self._end_after)

    def _switch(self, phase: str) -> None:
        now = _now()
        self.phase_secs[self.phase] += now - self._phase_at
        self.phase, self._phase_at = phase, now
        _LOGGER.info("signal test '%s': phase %s", self.name, phase)
        self.co.async_update_listeners()

    @callback
    def _end_after(self, _now) -> None:
        self._end_timer = None
        self.finish("done")

    def abort(self) -> None:
        for h in self.holds.values():
            h["stop"].set()
        self.holds.clear()
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        if self._end_timer:
            self._end_timer()
            self._end_timer = None

    def finish(self, why: str) -> dict:
        if self.ended:
            return self.result()
        self.phase_secs[self.phase] += _now() - self._phase_at
        self._phase_at = _now()
        self.ended = why
        self.abort()
        res = self.result()
        _LOGGER.warning("signal test '%s' %s: %s", self.name, why, res["summary"].replace("\n", " | "))
        self.hass.async_create_task(self._report(res))
        self.co.sigtest, self.co.sigtest_last = None, res
        self.co.async_update_listeners()
        return res

    async def _report(self, res: dict) -> None:
        title = f"{self.name} interference test"
        calls = [("persistent_notification", "create", {"title": title, "message": res["summary"],
                                                        "notification_id": "hdhr_signal_test"})]
        if self.notify and "." in self.notify:
            domain, service = self.notify.split(".", 1)
            calls.append((domain, service, {"title": title, "message": res["summary"]}))
        for domain, service, data in calls:
            try:
                await self.hass.services.async_call(domain, service, data, blocking=False)
            except Exception as e:  # noqa: BLE001 - the notifications are a courtesy; the result is kept regardless
                _LOGGER.warning("signal test: %s.%s failed: %s", domain, service, e)

    # --- every coordinator poll (30 s) ---------------------------------------------------------------------------
    def tick(self, data: dict) -> None:
        if self.ended:
            return
        now = _now()
        first = self._last_tick is None              # the errors in this poll's data came before the test began
        dt = 0.0 if first else min(now - self._last_tick, 120.0)
        self._last_tick = now
        tuners = []                                  # (ip, index, state) for every tuner
        for info in data.get("devices", {}).values():
            if info.get("kind") != "tuner" or not info.get("ok"):
                continue
            ip = urlparse(info.get("base", "")).hostname
            for n in range(info.get("count", 0)):
                tuners.append((ip, n, (info["state"] or {}).get(f"t{n}") or {}))
        # 1) tally every tuned channel (held or a TV's) for this phase
        for _ip, n, t in tuners:
            ch = t.get("vct")
            if first or not ch or not t.get("target"):
                continue
            s = self.stats.setdefault(ch, {"mhz": None, "on": self._blank(), "after": self._blank()})
            s["mhz"] = t.get("freq_mhz") or s["mhz"]
            p = s[self.phase]
            err = int(t.get("new_err") or 0)
            p["errors"] += err
            p["tuner_s"] += dt
            p["tuners"][str(n)] = p["tuners"].get(str(n), 0) + err
            if err and len(self.bursts) < MAX_BURSTS:
                self.bursts.append([round(now), ch, n, err, self.phase])
        if now - self.started > self.max_s:
            self.finish(f"stopped after {self.max_s / 3600:g} hours")
            return
        # 2) tuners: release first, then hold what is safe
        for ch, h in list(self.holds.items()):
            if not h["thread"].is_alive():
                self.notes[ch] = f"hold ended: {h['result'][0] or 'unknown'}"
                self._retry_at[ch] = now + RETRY_S
                del self.holds[ch]
        free = sum(1 for _ip, _n, t in tuners if not t.get("target"))
        soon = any(0 <= s["start"] - now <= QUIET_S for s in self.co.schedule or [])
        by_tv = {t.get("vct") for _ip, _n, t in tuners if t.get("target") and t.get("target") != self.my_ip}
        for ch in [c for c in reversed(self.channels) if c in self.holds]:
            reason = ("a recording starts soon" if soon else "no tuner left free" if free == 0
                      else "a TV is on this channel (measured from its tuner)" if ch in by_tv else None)
            if reason:
                self._release(ch, reason)
                free += 1
                if reason != "a TV is on this channel (measured from its tuner)":
                    break                            # one per poll, lowest priority first
        tuner_ip = next((ip for ip, _n, _t in tuners if ip), None)
        for ch in self.channels:
            if ch in self.holds:
                continue
            if ch in by_tv:
                self.notes[ch] = "measured from a TV's tuner"
                continue
            if soon or free < 2 or tuner_ip is None or now < self._retry_at.get(ch, 0):
                if ch not in self.notes:
                    self.notes[ch] = "waiting for a spare tuner"
                continue
            self._hold(tuner_ip, ch)
            free -= 1

    @staticmethod
    def _blank() -> dict:
        return {"errors": 0, "tuner_s": 0.0, "tuners": {}}

    def _hold(self, ip: str, ch: str) -> None:
        stop, result = threading.Event(), [None]

        def opened(local_ip: str) -> None:
            self.my_ip = self.my_ip or local_ip

        def run() -> None:
            result[0] = hdhr_api.hold_channel(ip, ch, stop, on_open=opened)

        th = threading.Thread(target=run, name=f"hdhr-sigtest-{ch}", daemon=True)
        self.holds[ch] = {"stop": stop, "thread": th, "result": result}
        self.notes[ch] = "held on a spare tuner"
        th.start()
        _LOGGER.info("signal test '%s': holding %s", self.name, ch)

    def _release(self, ch: str, why: str) -> None:
        h = self.holds.pop(ch, None)
        if h:
            h["stop"].set()
        self.notes[ch] = f"released: {why}"
        self._retry_at[ch] = _now() + 60
        _LOGGER.info("signal test '%s': released %s (%s)", self.name, ch, why)

    # --- results -------------------------------------------------------------------------------------------------
    def state(self) -> str:
        return f"running ({self.name.lower()} {'on' if self.phase == 'on' else 'off'})"

    def result(self) -> dict:
        secs = dict(self.phase_secs)
        if not self.ended:
            secs[self.phase] += _now() - self._phase_at
        rows = []
        for ch in sorted(self.stats, key=hdhr_api.channel_key):
            s = self.stats[ch]
            band = "" if not s["mhz"] else ("VHF" if s["mhz"] < UHF_MHZ else "UHF")
            rows.append({"channel": ch, "band": band, "mhz": s["mhz"],
                         "on": {"errors": s["on"]["errors"], "tuner_min": round(s["on"]["tuner_s"] / 60, 1),
                                "by_tuner": s["on"]["tuners"]},
                         "off": {"errors": s["after"]["errors"], "tuner_min": round(s["after"]["tuner_s"] / 60, 1),
                                 "by_tuner": s["after"]["tuners"]}})
        verdict = self._verdict(rows)
        on_min, off_min = round(secs["on"] / 60), round(secs["after"] / 60)
        lines = [f"{_clock(self.started)} to {_clock(_now())}: {self.name.lower()} on {on_min} min, "
                 f"then {off_min} min off." + ("" if self.ended in (None, "done") else f" ({self.ended})")]
        for r in rows:
            if not r["on"]["tuner_min"] and not r["off"]["tuner_min"]:
                continue
            band = f" ({r['band']} {r['mhz']:g} MHz)" if r["mhz"] else ""
            lines.append(f"{r['channel']}{band}: {r['on']['errors']} errors with the {self.name.lower()} on "
                         f"({r['on']['tuner_min']:g} tuner-min), {r['off']['errors']} after "
                         f"({r['off']['tuner_min']:g} tuner-min)")
        missing = [c for c in self.channels if c not in self.stats]
        if missing:
            lines.append("Not measured: " + ", ".join(f"{c} ({self.notes.get(c, 'no spare tuner')})" for c in missing))
        lines.append(verdict)
        return {"name": self.name, "started": dt_util.utc_from_timestamp(self.started).isoformat(timespec="seconds"),
                "ended": self.ended, "phase": None if self.ended else self.phase, "minutes_on": on_min,
                "minutes_off": off_min, "channels": rows, "holds": sorted(self.holds), "notes": dict(self.notes),
                "bursts": [[dt_util.as_local(dt_util.utc_from_timestamp(b[0])).strftime("%H:%M:%S")] + b[1:]
                           for b in self.bursts],
                "verdict": verdict, "summary": "\n".join(lines)}

    def _verdict(self, rows: list[dict]) -> str:
        on = sum(r["on"]["errors"] for r in rows)
        off = sum(r["off"]["errors"] for r in rows)
        name = self.name.lower()
        if not rows or not any(r["on"]["tuner_min"] for r in rows):
            return f"Nothing was measured while the {name} ran (no spare tuners)."
        if on == 0 and off == 0:
            return f"No errors at all: the {name} didn't disturb reception this run."
        # one tuner bad while another tuner on the same channel stayed clean -> the tuner, not the air
        for r in rows:
            bt = r["on"]["by_tuner"]
            if len(bt) >= 2 and sum(bt.values()) and min(bt.values()) == 0:
                bad = [k for k, v in bt.items() if v]
                return (f"On {r['channel']} only tuner {', '.join(bad)} had errors while another tuner on the same "
                        f"channel was clean: points to that tuner, not the {name}.")
        on_rate = on / max(sum(r["on"]["tuner_min"] for r in rows), 1)
        off_rate = off / max(sum(r["off"]["tuner_min"] for r in rows), 1)
        vhf = sum(r["on"]["errors"] for r in rows if r["band"] == "VHF")
        uhf = sum(r["on"]["errors"] for r in rows if r["band"] == "UHF")
        if off_rate >= on_rate * 0.5:
            return f"Errors with the {name} off too, at a similar rate: not the {name} alone."
        band = (" Only VHF channels were hit; UHF stayed clean." if vhf and not uhf
                else " UHF was hit too." if uhf else "")
        return f"Errors came with the {name} on and stopped when it was off: points to the {name}.{band}"
