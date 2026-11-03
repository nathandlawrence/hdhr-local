"""HDHR Local: HDHomeRun tuners and DVR engines in Home Assistant, plus a built-in dashboard (sidebar panel).

Works with HDHomeRun devices on the local network; not made by or affiliated with SiliconDust.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import timedelta
from pathlib import Path

import voluptuous as vol
from aiohttp import web

from homeassistant.components import frontend, panel_custom, webhook, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import hdhr_api
from .const import (CONF_PANEL_TITLE, CONF_PLAYER_REMOTES, DEFAULT_PANEL_TITLE, DOMAIN, PANEL_URL, STATIC_URL,
                    VERSION)
from .coordinator import HdhrCoordinator
from .services import async_setup_services, parse_remotes

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CALENDAR, Platform.SWITCH]
CONF_WEBHOOK_ID = "webhook_id"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not entry.data.get(CONF_WEBHOOK_ID):      # entries made before 0.4 get one now (no listener registered yet)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_WEBHOOK_ID: secrets.token_hex(16)})
    coordinator = HdhrCoordinator(hass, entry)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_stale_players(hass, entry, coordinator)
    async_setup_services(hass)
    await _async_setup_panel(hass, entry)
    # External freeze detectors (any script watching a player) POST {"player": ip, "event": "freeze"|"recovered"|"ping"} here.
    # Local network only.
    webhook.async_register(hass, DOMAIN, "HDHR Local player events", entry.data[CONF_WEBHOOK_ID], _handle_webhook,
                           local_only=True, allowed_methods=["POST"])
    entry.async_on_unload(lambda: webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID]))
    # best-feed tests for simulcast recordings: every 10 minutes, first run 90 s after start
    entry.async_on_unload(async_track_time_interval(hass, coordinator.async_feed_cycle, timedelta(minutes=10)))
    entry.async_on_unload(async_call_later(hass, 90, coordinator.async_feed_cycle))
    entry.async_on_unload(lambda: coordinator.sigtest and coordinator.sigtest.abort())   # frees held tuners
    # the 2-week guide (XMLTV): now in the background, then every 6 hours
    entry.async_create_background_task(hass, coordinator.async_refresh_guide(), "hdhr guide")
    entry.async_on_unload(async_track_time_interval(hass, coordinator.async_refresh_guide,
                                                    timedelta(seconds=coordinator.GUIDE_EVERY_S)))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


@callback
def _remove_stale_players(hass: HomeAssistant, entry: ConfigEntry, coordinator: HdhrCoordinator) -> None:
    """A renamed or removed player leaves its old device behind; detach those from this entry (HA then deletes them)."""
    reg = dr.async_get(hass)
    current = set(coordinator.players())
    for dev in dr.async_entries_for_config_entry(reg, entry.entry_id):
        ids = {i[1] for i in dev.identifiers if i[0] == DOMAIN}
        if any(i.startswith("player-") for i in ids) and not ids & current:
            reg.async_update_device(dev.id, remove_config_entry_id=entry.entry_id)


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.Response(status=400, text="JSON body expected")
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_WEBHOOK_ID) == webhook_id and getattr(entry, "runtime_data", None):
            ok = entry.runtime_data.player_event(body if isinstance(body, dict) else {})
            return web.Response(status=200 if ok else 400, text="ok" if ok else "bad event")
    return web.Response(status=404)


# --- built-in dashboard ------------------------------------------------------------------------------------------

async def _async_setup_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)])
        websocket_api.async_register_command(hass, ws_layout)
        websocket_api.async_register_command(hass, ws_webhook)
        websocket_api.async_register_command(hass, ws_guide)
        websocket_api.async_register_command(hass, ws_guide_search)
        data["registered"] = True
    # The page file's content hash goes in its URL, so a browser always loads the current version after an update.
    js = Path(__file__).parent / "frontend" / "hdhr-panel.js"
    tag = hashlib.sha256(await hass.async_add_executor_job(js.read_bytes)).hexdigest()[:12]
    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name="hdhr-panel",
        module_url=f"{STATIC_URL}/hdhr-panel.js?v={VERSION}-{tag}",
        sidebar_title=entry.options.get(CONF_PANEL_TITLE) or DEFAULT_PANEL_TITLE,
        sidebar_icon="mdi:antenna",
        require_admin=False,
        config={},
    )


def _entry(hass: HomeAssistant) -> ConfigEntry | None:
    return next((e for e in hass.config_entries.async_entries(DOMAIN) if getattr(e, "runtime_data", None)), None)


def build_layout(hass: HomeAssistant) -> dict:
    """What the panel draws: every device and player with its entity ids by role, in a stable order."""
    entry = _entry(hass)
    if entry is None:
        return {"title": DEFAULT_PANEL_TITLE, "devices": [], "players": []}
    coordinator: HdhrCoordinator = entry.runtime_data
    reg = er.async_get(hass)
    by_dev: dict[str, dict] = {}
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):     # every platform
        dev_id, _, key = (ent.unique_id or "").partition("_")
        by_dev.setdefault(dev_id, {})[key] = ent.entity_id
    devices = []
    for dev_id, info in sorted((coordinator.data or {}).get("devices", {}).items(),
                               key=lambda kv: (kv[1].get("kind") != "tuner", kv[0])):
        devices.append({"id": dev_id, "name": coordinator.device_name(dev_id), "kind": info.get("kind"),
                        "model": info.get("model"), "count": info.get("count", 0), "entities": by_dev.get(dev_id, {})})
    remotes = parse_remotes(entry.options.get(CONF_PLAYER_REMOTES, ""))
    players = [{"id": pid, "name": p["name"], "entities": by_dev.get(pid, {}), "tunable": p["name"] in remotes}
               for pid, p in sorted(coordinator.players().items(), key=lambda kv: kv[1]["name"].lower())]
    return {"title": entry.options.get(CONF_PANEL_TITLE) or DEFAULT_PANEL_TITLE, "devices": devices, "players": players}


@websocket_api.websocket_command({vol.Required("type"): "hdhr/layout"})
@callback
def ws_layout(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    connection.send_result(msg["id"], build_layout(hass))


@websocket_api.websocket_command({vol.Required("type"): "hdhr/guide", vol.Optional("start"): vol.Coerce(float),
                                  vol.Optional("hours", default=6): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=24))})
@callback
def ws_guide(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The Guide tab's browse view (step a day or 6 hours at a time):
    every channel's programs that overlap [start, start + hours) (start defaults to now), who is watching
    what, and how far the guide reaches. The full two weeks stay here; searches go through hdhr/guide_search."""
    entry = _entry(hass)
    if entry is None:
        connection.send_result(msg["id"], {"channels": [], "fetched": None, "days": 0, "first": None, "last": None})
        return
    co: HdhrCoordinator = entry.runtime_data
    now = time.time()
    t0 = msg.get("start") or now
    t1 = t0 + msg["hours"] * 3600
    chans, first, last = [], None, None
    for n in sorted(co.guide, key=hdhr_api.channel_key):
        ps = co.guide[n]["programs"]
        live = [p for p in ps if p["e"] > now]
        if live:
            first = min(first or live[0]["s"], live[0]["s"])
            last = max(last or 0, ps[-1]["e"])
        chans.append({"number": n, "name": co.guide[n]["name"],
                      "programs": [p for p in live if p["s"] < t1 and p["e"] > t0]})
    connection.send_result(msg["id"], {"channels": chans, "fetched": co.guide_at or None, "days": co.guide_days(),
                                       "first": first, "last": last, "watching": _watching(co)})


@websocket_api.websocket_command({vol.Required("type"): "hdhr/guide_search", vol.Required("query"): str,
                                  vol.Optional("limit", default=300): vol.All(int, vol.Range(min=1, max=1000))})
@websocket_api.async_response
async def ws_guide_search(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Search the whole guide (~2 weeks, ~53,000 listings): title, episode, description or channel, in time order.
    Runs off the event loop; the guide dict is only ever replaced whole, never changed in place."""
    entry = _entry(hass)
    if entry is None:
        connection.send_result(msg["id"], {"results": [], "watching": {}})
        return
    co: HdhrCoordinator = entry.runtime_data
    results = await hass.async_add_executor_job(hdhr_api.search_guide, co.guide, msg["query"], time.time(), msg["limit"])
    connection.send_result(msg["id"], {"results": results, "watching": _watching(co)})


def _watching(co: HdhrCoordinator) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for pid, p in co.players().items():
        ps = co.player_state(pid)
        if ps["playing"]:
            out.setdefault(ps["channel"].split(" ")[0], []).append(p["name"])
    return out


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hdhr/webhook"})
@callback
def ws_webhook(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Admin only: the local path a freeze detector posts to (for setting the detector up)."""
    entry = _entry(hass)
    connection.send_result(msg["id"], {"path": f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}"} if entry else None)
