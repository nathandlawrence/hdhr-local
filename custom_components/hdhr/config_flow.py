"""Setup: find devices on the network, then name them (names decide the entity ids, so they come first)."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from . import hdhr_api
from .const import (CONF_CLIENT_NAMES, CONF_DEVICE_NAMES, CONF_HOSTS, CONF_PANEL_TITLE, CONF_PLAYER_REMOTES, CONF_TV_APP,
                    DEFAULT_PANEL_TITLE, DEFAULT_TV_APP, DOMAIN)

CLIENT_PREFIX = "player "      # form keys for players: "player 192.168.1.41"
MORE_PLAYERS = "more_players"  # free text "IP=Name, IP=Name" for players that aren't watching right now


def _probe(hosts: list[str]) -> dict:
    """Blocking: find devices and the players watching right now."""
    net = hdhr_api.Network(hosts)
    net.scan()
    return net.poll(lambda ip: ip)


def _found_text(devices: dict) -> str:
    lines = [f"- {i}: {d.get('label', '')} ({d.get('kind')}, {d.get('base')})" for i, d in devices.items()]
    return "\n".join(lines) or "- none"


def _names_schema(devices: dict, clients: list[str], dev_names: dict, client_names: dict, title: str) -> vol.Schema:
    fields: dict = {vol.Optional(CONF_PANEL_TITLE, default=title): str}
    for dev_id, info in devices.items():
        fields[vol.Optional(dev_id, default=dev_names.get(dev_id) or info.get("label") or dev_id)] = str
    for ip in sorted(set(clients) | set(client_names)):
        fields[vol.Optional(CLIENT_PREFIX + ip, default=client_names.get(ip, ip))] = str
    fields[vol.Optional(MORE_PLAYERS, default="")] = str
    return vol.Schema(fields)


def _split_names(user_input: dict, devices: dict) -> tuple[dict, dict, str]:
    dev_names = {k: v.strip() for k, v in user_input.items() if k in devices and v.strip()}
    client_names = {k[len(CLIENT_PREFIX):]: v.strip() for k, v in user_input.items()
                    if k.startswith(CLIENT_PREFIX) and v.strip() and v.strip() != k[len(CLIENT_PREFIX):]}
    for pair in re.split(r"[,;\n]", user_input.get(MORE_PLAYERS, "")):
        ip, sep, name = pair.partition("=")
        if sep and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip.strip()) and name.strip():
            client_names[ip.strip()] = name.strip()
    title = (user_input.get(CONF_PANEL_TITLE) or DEFAULT_PANEL_TITLE).strip()
    return dev_names, client_names, title


class HdhrConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._hosts: list[str] = []
        self._snapshot: dict = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            self._hosts = [h.strip() for h in user_input.get(CONF_HOSTS, "").split(",") if h.strip()]
            self._snapshot = await self.hass.async_add_executor_job(_probe, self._hosts)
            if self._snapshot["devices"]:
                return await self.async_step_names()
            errors["base"] = "no_devices"
        return self.async_show_form(step_id="user", errors=errors,
                                    data_schema=vol.Schema({vol.Optional(CONF_HOSTS, default=""): str}))

    async def async_step_names(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        devices = self._snapshot["devices"]
        if user_input is not None:
            dev_names, client_names, title = _split_names(user_input, devices)
            return self.async_create_entry(
                title="HDHomeRun network", data={CONF_HOSTS: self._hosts},
                options={CONF_DEVICE_NAMES: dev_names, CONF_CLIENT_NAMES: client_names, CONF_PANEL_TITLE: title})
        return self.async_show_form(
            step_id="names", description_placeholders={"found": _found_text(devices)},
            data_schema=_names_schema(devices, self._snapshot.get("clients", []), {}, {}, DEFAULT_PANEL_TITLE))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HdhrOptionsFlow()


class HdhrOptionsFlow(OptionsFlow):
    """Rename devices / players and the sidebar page. Saving reloads the integration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        coordinator = self.config_entry.runtime_data
        data = coordinator.data or {}
        devices = data.get("devices", {})
        opts = self.config_entry.options
        if user_input is not None:
            dev_names, client_names, title = _split_names(user_input, devices)
            return self.async_create_entry(data={
                CONF_DEVICE_NAMES: dev_names, CONF_CLIENT_NAMES: client_names, CONF_PANEL_TITLE: title,
                CONF_PLAYER_REMOTES: user_input.get(CONF_PLAYER_REMOTES, "").strip(),
                CONF_TV_APP: user_input.get(CONF_TV_APP, "").strip() or DEFAULT_TV_APP})
        schema = _names_schema(devices, data.get("clients", []), opts.get(CONF_DEVICE_NAMES, {}),
                               opts.get(CONF_CLIENT_NAMES, {}), opts.get(CONF_PANEL_TITLE) or DEFAULT_PANEL_TITLE)
        schema = schema.extend({
            vol.Optional(CONF_PLAYER_REMOTES, default=opts.get(CONF_PLAYER_REMOTES, "")): str,
            vol.Optional(CONF_TV_APP, default=opts.get(CONF_TV_APP) or DEFAULT_TV_APP): str,
        })
        return self.async_show_form(step_id="init", description_placeholders={"found": _found_text(devices)},
                                    data_schema=schema)
