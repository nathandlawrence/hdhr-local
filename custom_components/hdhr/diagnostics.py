"""Diagnostics download: the latest snapshot (no secrets are ever stored; DeviceAuth is not read)."""
from __future__ import annotations

from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict:
    coordinator = entry.runtime_data
    return {"options": dict(entry.options), "hosts": entry.data.get("hosts", []), "snapshot": coordinator.data}
