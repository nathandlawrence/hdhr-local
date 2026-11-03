"""Actions for voice assistants and automations: change a TV's channel, record a show.

hdhr.record - find a show in SiliconDust's guide and add a recording rule (every new airing, or just the next one),
              then tell the DVR engine. WRITES the DVR schedule (cloud).
hdhr.tune   - put a TV / player on a channel through its Home Assistant remote entity (Android TV Remote): opens the
              HDHomeRun app if nothing is playing, types the channel number, checks it took, and falls back to
              Channel Up/Down steps for nearby channels.
Both answer with a sentence in "message" that a voice assistant can say.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from . import hdhr_api
from .sigtest import SignalTest
from .const import CONF_PLAYER_REMOTES, CONF_TV_APP, DEFAULT_TV_APP, DOMAIN

_LOGGER = logging.getLogger(__name__)
MAX_STEPS = 20            # Channel Up/Down fallback only for channels this close in the lineup
STEP_DELAY = 2.5          # seconds between Channel Up/Down presses (each one tunes)
KEY_DELAY = 0.4           # seconds between the typed number's keys (HA's default; 0.15 was too fast)
ENTER_PAUSE = 0.8         # Enter goes separately, after the Search Channels box has opened (an early Enter got lost)
BOX_OPEN = 1.5            # wait for the Search Channels box and its on-screen keyboard (0.4-1.0 s on an NVIDIA Shield)
# Backing out of a stuck Search Channels box: Back does nothing there; Left on the D-pad closes it (and Back on
# plain live TV does not exit the app). Then Back closes the channel strip / controls if they are open.
BACK_OUT = ["DPAD_LEFT", "BACK"]
SETTLE_POLL = 0.5         # how often the DVR is asked which channel a TV is on while confirming
LAND_WAIT = 5.0           # wait for the number to land (the change itself takes ~2 s)
STEP_WAIT = 5.0           # wait after the last Channel Up/Down press

# Newest wins, per TV (a second change arriving mid-way must not interleave its keys with the first one's). A newer change stops the older one before its next key, then runs alone.
_TUNE_LOCKS: dict[str, asyncio.Lock] = {}
_TUNE_GEN: dict[str, int] = {}


class _Superseded(Exception):
    """A newer channel change for the same TV arrived."""

RECORD_SCHEMA = vol.All(vol.Schema({
    vol.Optional("show"): cv.string,
    vol.Optional("mode", default="series"): vol.In(["series", "next"]),
    vol.Optional("channel"): cv.string,
    # from a guide listing (the Guide tab): the exact series, and for mode "next" the airing's start (unix seconds)
    vol.Optional("series_id"): cv.string,
    vol.Optional("start"): vol.Coerce(int),
}), cv.has_at_least_one_key("show", "series_id"))
TUNE_SCHEMA = vol.Schema({vol.Required("player"): cv.string, vol.Required("channel"): cv.string})
SIGTEST_SCHEMA = vol.Schema({
    vol.Optional("watch_entity"): cv.entity_id,
    vol.Optional("name", default="Dryer"): cv.string,
    vol.Optional("channels", default=[]): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("after_minutes", default=15): vol.All(vol.Coerce(float), vol.Range(min=1, max=120)),
    vol.Optional("max_hours", default=3): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=12)),
    vol.Optional("notify"): cv.string,
    vol.Optional("stop", default=False): cv.boolean,
})


def _coordinator(hass: HomeAssistant):
    entry = next((e for e in hass.config_entries.async_entries(DOMAIN) if getattr(e, "runtime_data", None)), None)
    if entry is None:
        raise HomeAssistantError("HDHR Local is not loaded")
    return entry.runtime_data


def parse_remotes(text: str) -> dict[str, str]:
    """'Living Room TV=remote.shield, Den TV=remote.x' -> {name: entity id}."""
    out = {}
    for pair in re.split(r"[,;\n]", text or ""):
        name, sep, ent = pair.partition("=")
        if sep and name.strip() and re.fullmatch(r"remote\.[a-z0-9_]+", ent.strip()):
            out[name.strip()] = ent.strip()
    return out


async def _record(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    co = _coordinator(hass)
    lineup = (co.data or {}).get("lineup", {})
    ch = None
    if call.data.get("channel"):
        ch = hdhr_api.resolve_channel(call.data["channel"], lineup)
        if not ch:
            return {"ok": False, "message": f"I couldn't find channel {call.data['channel']}."}
    try:
        if call.data.get("series_id"):
            # straight from a guide listing: no title search
            res = await hass.async_add_executor_job(co.network.record_exact, call.data["series_id"], call.data["mode"],
                                                    call.data.get("start"), ch)
            listing = next((p for g in co.guide.values() for p in g["programs"]
                            if p["sid"] == call.data["series_id"]), None)
            res["title"] = call.data.get("show") or (listing or {}).get("t") or "that show"
            if call.data["mode"] == "next" and call.data.get("start"):
                res["next_airing"] = {"start": call.data["start"], "channel": ch}
        else:
            res = await hass.async_add_executor_job(co.network.record, call.data["show"], call.data["mode"], ch)
    except (OSError, ValueError) as err:
        raise HomeAssistantError(f"SiliconDust's service didn't answer: {err}") from err
    if not res.get("ok"):
        return {"ok": False, "message": res.get("error", "That didn't work.")}
    await co.async_refresh_schedule()
    title, nxt = res["title"], res.get("next_airing")
    when = ""
    if nxt:
        t = dt_util.as_local(dt_util.utc_from_timestamp(nxt["start"]))
        when = f" Next airing: {t:%A %B} {t.day} at {t:%I:%M %p}".replace(" 0", " ") + f" on channel {nxt['channel']}."
    if res["mode"] == "next":
        msg = f"Recording the next airing of {title}.{when}"
    else:
        msg = f"Recording every new airing of {title}" + (f" on channel {ch}" if ch else "") + "." + \
              (when if nxt else " Nothing is scheduled in the guide yet; it will record when it airs.")
    return {"ok": True, "message": msg, "title": title, "mode": res["mode"], "series_id": res["series_id"]}


async def _tune(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    co = _coordinator(hass)
    players = co.players()
    names = sorted(p["name"] for p in players.values())
    name = hdhr_api.resolve_player(call.data["player"], names)
    if not name:
        return {"ok": False, "message": f"I don't know a TV called {call.data['player']}. I know: {', '.join(names)}."}
    lineup = (co.data or {}).get("lineup", {})
    target = hdhr_api.resolve_channel(call.data["channel"], lineup)
    if not target:
        # Not a channel or station: a show airing right now? (tune by program name)
        hits = [h for h in hdhr_api.programs_now(co.guide, call.data["channel"], time.time()) if h[0] in lineup]
        titles = sorted({p["t"] for _, p in hits})
        if len(titles) > 1:
            opts = ", ".join(f"{p['t']} on {n}" for n, p in hits[:5])
            return {"ok": False, "message": f"More than one show matches {call.data['channel']}: {opts}. Which one?"}
        if hits:
            def rate(ch: str) -> float:
                s = hdhr_api.feed_stats(co.feed["samples"].get(ch, []))
                return s["rate"] if s["tries"] >= hdhr_api.FEED_MIN_SAMPLES else -1.0
            # the same show on several channels (a simulcast): the copy whose feed tests delivered best, else the
            # lowest channel number
            hits.sort(key=lambda h: (-rate(h[0]), hdhr_api.channel_key(h[0])))
            target = hits[0][0]
    if not target:
        return {"ok": False, "message": f"I couldn't find a channel or a show on now called {call.data['channel']}."}
    remote = parse_remotes(co.config_entry.options.get(CONF_PLAYER_REMOTES, "")).get(name)
    if not remote:
        return {"ok": False, "message": f"{name} has no remote set up. Add it in HDHR Local's Configure screen."}
    pid = next(pid for pid, p in players.items() if p["name"] == name)
    label = f"{target} {lineup.get(target, '')}".strip()
    on_now = co.program_on(target)
    if on_now:
        label += f" ({on_now['t']})"

    gen = _TUNE_GEN[pid] = _TUNE_GEN.get(pid, 0) + 1

    def check() -> None:
        if _TUNE_GEN[pid] != gen:
            raise _Superseded

    async def key(data: dict) -> None:
        check()
        await hass.services.async_call("remote", "send_command", {"entity_id": remote, **data}, blocking=True)

    async def run() -> ServiceResponse:
        t0 = time.monotonic()

        def cur() -> str:
            return co.player_state(pid)["channel"].split(" ")[0]

        def on_target() -> bool:
            return cur() == target

        major = target.split(".")[0]

        def on_major() -> bool:
            return cur().split(".")[0] == major

        async def settle(done, seconds: float) -> bool:
            # every half second ask only the DVR which channel each TV is on (one fast request); stop as soon as `done`
            for _ in range(int(seconds / SETTLE_POLL)):
                await asyncio.sleep(SETTLE_POLL)
                check()
                await co.async_quick_sessions()
                if done():
                    break
            return done()

        def log(step: str) -> None:
            _LOGGER.info("tune %s -> %s: %s at %.1f s (showing %s)", name, target, step, time.monotonic() - t0, cur())

        if on_target():
            return {"ok": True, "message": f"{name} is already on {label}."}
        if not co.player_state(pid)["playing"]:
            app = co.config_entry.options.get(CONF_TV_APP) or DEFAULT_TV_APP
            check()
            await hass.services.async_call("remote", "turn_on", {"entity_id": remote, "activity": app}, blocking=True)
            await asyncio.sleep(8)          # the app starts and resumes its last channel
            log("app opened")

        # 1) The HDHomeRun app's way: typing a number opens its "Search Channels" box and jumps to the first
        #    matching channel; Enter tunes it. The remote can't send a decimal point, so type the MAIN number only
        #    (6.2 -> "6") and step to the subchannel below. The number goes first, then Enter separately once the box is
        #    open (an Enter sent too soon gets lost and leaves the box stuck). The on-screen keyboard DOES pop up
        #    with the box (on an NVIDIA Shield; usually gone again in under half a second, Enter closes it otherwise).
        #    Wait only until it lands on that MAIN number (6.x); if it lands anywhere else, back out once and retype.
        #    The first digit opens the box AND Android's on-screen keyboard. The keyboard hides itself (and the list filters)
        #    only when a digit reaches it AFTER it has opened; if every digit beats it, it stays up and swallows the Enter,
        #    and the app tunes the top of the list (tested on an NVIDIA Shield: digits
        #    0.1 s apart -> the top channel 3 times out of 3; last digit after the box opened -> right channel 10/10;
        #    screenshots: keyboard up + unfiltered list vs. no keyboard + list on the channel). So: type the number, wait
        #    for the box, then Delete and retype the last digit (works for one-digit numbers too: "9" -> 9.1).
        async def type_number() -> None:
            await key({"command": list(major), "delay_secs": KEY_DELAY})
            await asyncio.sleep(BOX_OPEN)
            await key({"command": ["DEL", major[-1]], "delay_secs": KEY_DELAY})
            await asyncio.sleep(ENTER_PAUSE)
            await key({"command": "ENTER"})

        landed = on_major()
        if not landed:
            await type_number()
            landed = await settle(on_major, LAND_WAIT)
            log("number sent" if landed else "number did not take")
        if not landed:
            # A menu, overlay or stuck search box had focus: back out once,
            # then type again. Never sent up front - on plain live TV it could leave the app.
            await key({"command": BACK_OUT, "delay_secs": 0.5})
            await asyncio.sleep(1.0)
            await type_number()
            landed = await settle(on_major, LAND_WAIT)
            log("after back-out + number" if landed else "back-out + number did not take")
        if on_target():
            hass.async_create_task(co.async_request_refresh())
            return {"ok": True, "message": f"{name} is now on {label}.", "method": "number",
                    "seconds": round(time.monotonic() - t0, 1)}
        # 2) Channel Up / Down to the subchannel (or to a nearby channel if the number didn't take)
        here = cur()
        order = sorted(lineup, key=hdhr_api.channel_key)
        if here in order and target in order and 0 < abs(order.index(target) - order.index(here)) <= MAX_STEPS:
            diff = order.index(target) - order.index(here)
            for i in range(abs(diff)):     # one press at a time, so a newer change can stop it in between
                if i:
                    await asyncio.sleep(STEP_DELAY)
                await key({"command": "CHANNEL_UP" if diff > 0 else "CHANNEL_DOWN"})
            ok = await settle(on_target, STEP_WAIT)
            log(f"{abs(diff)} channel step(s)" + ("" if ok else " did not land"))
            hass.async_create_task(co.async_request_refresh())
            if ok:
                return {"ok": True, "message": f"{name} is now on {label}.", "method": "steps",
                        "seconds": round(time.monotonic() - t0, 1)}
        hass.async_create_task(co.async_request_refresh())
        now = co.player_state(pid)["channel"]
        return {"ok": False, "message": f"I couldn't get {name} to {label}; it's showing {now}."}


    async with _TUNE_LOCKS.setdefault(pid, asyncio.Lock()):
        try:
            check()
            return await run()
        except _Superseded:
            _LOGGER.info("tune %s -> %s: stopped, a newer change took over", name, target)
            return {"ok": False, "superseded": True, "message": f"Stopped: a newer channel change for {name} took over."}

async def _signal_test(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Start an interference test that follows `watch_entity` (the dryer), or stop the running one (stop: true)."""
    co = _coordinator(hass)
    if call.data["stop"]:
        if co.sigtest is None:
            return {"ok": False, "message": "No signal test is running."}
        res = co.sigtest.finish("stopped by hand")
        return {"ok": True, "message": res["summary"]}
    if co.sigtest is not None:
        return {"ok": False, "message": f"A {co.sigtest.name} test is already running."}
    if not call.data.get("watch_entity"):
        return {"ok": False, "message": "Say which entity to follow (watch_entity), e.g. binary_sensor.dryer_power."}
    t = SignalTest(hass, co, call.data["name"], call.data["watch_entity"], call.data["channels"],
                   call.data["after_minutes"], call.data["max_hours"], call.data.get("notify"))
    co.sigtest = t
    t.start()
    if co.data:
        t.tick(co.data)                    # take the spare tuners now, not at the next poll
    co.async_update_listeners()
    return {"ok": True, "message": f"{t.name} test started ({t.phase}); channels {', '.join(t.channels) or 'already in use only'}."}


def async_setup_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "record"):
        return

    async def record(call: ServiceCall) -> ServiceResponse:
        return await _record(hass, call)

    async def tune(call: ServiceCall) -> ServiceResponse:
        return await _tune(hass, call)

    hass.services.async_register(DOMAIN, "record", record, schema=RECORD_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "tune", tune, schema=TUNE_SCHEMA, supports_response=SupportsResponse.OPTIONAL)

    async def signal_test(call: ServiceCall) -> ServiceResponse:
        return await _signal_test(hass, call)

    hass.services.async_register(DOMAIN, "signal_test", signal_test, schema=SIGTEST_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
