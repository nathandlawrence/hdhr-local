"""HDHR Local inside a real (test) Home Assistant 2026.10: setup flow, entity ids, values, options, layout, panel."""
import time
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.hdhr.const import DOMAIN

SNAP = {
    "devices": {
        "1080ABCD": {"kind": "tuner", "model": "HDFX-4K", "label": "HDHomeRun FLEX 4K 1080ABCD",
                     "base": "http://192.168.1.20:80", "firmware": "20250101", "count": 4, "ok": True,
                     "state": {"firmware": "20250101", "in_use": 1,
                               **{f"t{n}": {"channel": "idle", "vct": None, "ss": None, "snq": None, "seq": None,
                                            "mbps": 0.0, "epm": 0.0, "new_err": 0, "viewer": "none", "target": None,
                                            "freq_mhz": None} for n in range(3)},
                               "t3": {"channel": "6.1 STAR6HD", "vct": "6.1", "ss": 100, "snq": 97, "seq": 100,
                                      "mbps": 7.75, "epm": 1.5, "new_err": 4, "viewer": "Living Room TV",
                                      "target": "192.168.1.30", "freq_mhz": 207.0}}},
        "70000001": {"kind": "dvr", "model": "RECORD engine", "label": "HDHomeRun RECORD 192.168.1.30",
                     "base": "http://192.168.1.30:59090", "firmware": "20250102", "count": 0, "ok": True,
                     "state": {"firmware": "20250102", "free_gb": 6050.3, "used_pct": 23.6, "live": 1,
                               "live_list": ["6.1 -> Living Room TV"], "recording": False,
                               "recording_list": [], "recordings": 1, "last": "Pro Football",
                               "last_attrs": {"title": "Pro Football", "channel": "6.1"}}},
    },
    "sessions": [["192.168.1.30", "6.1", "192.168.1.41", "live"]],
    "clients": ["192.168.1.41"],
    "lineup": {"6.1": "STAR6HD", "4.1": "SUMMIT4"},
    "tuner_count": 4,
}

SCHEDULE: list = []      # set per test (seconds relative to "now" are filled in by FakeNetwork.schedule)


def _sched(rel):
    now = time.time()
    out = []
    for item in rel:
        a, b, t, ch = item[:4]
        extra = item[4] if len(item) > 4 else {}
        out.append({"start": now + a, "end": now + b, "title": t, "episode": "", "channel": ch, "channels": [ch],
                    "airs_on": [ch], "rule_channels": None, "channel_name": "", "series_id": "S", "program_id": "",
                    "synopsis": "", **extra})
    return out


class FakeNetwork:
    def __init__(self, static=None):
        self.static = static or []

    def scan(self):
        return []

    def poll(self, client_name):
        return SNAP

    def schedule(self):
        return _sched(SCHEDULE)

    def quick_sessions(self):
        return [list(x) for x in SNAP["sessions"]]

    def guide(self):
        now = int(time.time())
        return {"4.1": {"name": "SUMMIT4", "programs": [
                    {"s": now - 600, "e": now + 3000, "t": "Night Report Live", "ep": "The Last Call", "sid": "DL", "syn": ""}]},
                "104.1": {"name": "NXTGN", "programs": [
                    {"s": now - 600, "e": now + 3000, "t": "Night Report Live", "ep": "The Last Call", "sid": "DL", "syn": ""}]},
                "6.1": {"name": "STAR6HD", "programs": [
                    {"s": now - 900, "e": now + 900, "t": "Evening News", "ep": "", "sid": "FN", "syn": "Local news"},
                    {"s": now + 900, "e": now + 4500, "t": "Judge Hour", "ep": "", "sid": "JM", "syn": ""}]},
                "6.2": {"name": "ARENA", "programs": [
                    {"s": now - 900, "e": now + 900, "t": "Evening Newsbreak", "ep": "", "sid": "FB", "syn": ""}]}}

    def record_exact(self, series_id, mode, start, channel):
        RECORDED.append(("exact", series_id, mode, start, channel))
        return {"ok": True, "series_id": series_id, "mode": mode}

    def probe(self, ch, min_free=2):
        return PROBE(ch) if PROBE else None

    def pin_rule(self, series_id, channel):
        PINNED.append((series_id, channel))

    def record(self, text, mode="series", channel=None):
        RECORDED.append((text, mode, channel))
        return {"ok": True, "title": "Night Report", "series_id": "D1", "mode": mode,
                "next_airing": {"start": time.time() + 3600, "channel": "4.1", "episode": "A"}}


RECORDED: list = []
PINNED: list = []
PROBE = None


NAMES = {"panel_title": "Live TV", "1080ABCD": "HDHomeRun Tuner",
         "70000001": "HDHomeRun DVR", "player 192.168.1.41": "Living Room TV",
         "more_players": "192.168.1.42=Bedroom TV, 192.168.1.43 = Den TV; junk"}


async def _setup(hass: HomeAssistant):
    with patch("custom_components.hdhr.hdhr_api.Network", FakeNetwork):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "names"
        assert "1080ABCD" in r["description_placeholders"]["found"]
        r = await hass.config_entries.flow.async_configure(r["flow_id"], NAMES)
        assert r["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done(wait_background_tasks=True)    # the guide loads in the background
    return r["result"]


async def test_setup_entity_ids_and_values(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert entry.options["client_names"] == {"192.168.1.41": "Living Room TV",
                                             "192.168.1.42": "Bedroom TV",
                                             "192.168.1.43": "Den TV"}
    ents = [e for e in er.async_get(hass).entities.values() if e.platform == DOMAIN]
    assert len(ents) == 63   # ... + 3 players' now playing + 4 tuners' program + the signal test
    ids = {e.entity_id for e in ents}
    # the same entity ids the MQTT version used, so history and the existing dashboard carry over
    for eid in ("sensor.hdhomerun_tuner_tuner_3_channel", "sensor.hdhomerun_tuner_tuner_3_viewer",
                "sensor.hdhomerun_tuner_tuner_3_stream_errors", "sensor.hdhomerun_tuner_tuners_in_use",
                "sensor.hdhomerun_dvr_disk_used", "sensor.hdhomerun_dvr_free_space",
                "binary_sensor.hdhomerun_dvr_recording", "sensor.hdhomerun_dvr_last_recording"):
        assert eid in ids, eid
    assert hass.states.get("sensor.hdhomerun_tuner_tuner_3_channel").state == "6.1 STAR6HD"
    assert hass.states.get("sensor.hdhomerun_tuner_tuner_3_signal_quality").state == "97"
    assert hass.states.get("sensor.hdhomerun_tuner_tuner_0_signal_quality").state == "unknown"
    assert hass.states.get("binary_sensor.hdhomerun_dvr_recording").state == "off"
    assert hass.states.get("sensor.hdhomerun_dvr_live_streams").attributes["streams"] == \
        ["6.1 -> Living Room TV"]
    assert hass.states.get("sensor.hdhomerun_tuner_tuner_3_bitrate").attributes["unit_of_measurement"] == "Mbit/s"


async def test_layout_and_panel(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "hdhr/layout"})
    msg = await client.receive_json()
    assert msg["success"], msg
    layout = msg["result"]
    assert layout["title"] == "Live TV"
    tuner, dvr = layout["devices"]
    assert tuner["kind"] == "tuner" and tuner["count"] == 4 and tuner["name"] == "HDHomeRun Tuner"
    assert tuner["entities"]["tuner_3_channel"] == "sensor.hdhomerun_tuner_tuner_3_channel"
    assert tuner["entities"]["tuner_3_error_total"] == "sensor.hdhomerun_tuner_tuner_3_error_total"
    assert tuner["entities"]["error_total"] == "sensor.hdhomerun_tuner_error_total"
    assert tuner["entities"]["reset_errors"] == "button.hdhomerun_tuner_reset_error_totals"
    assert dvr["entities"]["recording"] == "binary_sensor.hdhomerun_dvr_recording"
    assert "hdhr" in hass.data.get("frontend_panels", {})


async def test_options_rename_keeps_ids(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    with patch("custom_components.hdhr.hdhr_api.Network", FakeNetwork):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert r["type"] is FlowResultType.FORM
        new = dict(NAMES, **{"player 192.168.1.41": "Living Room Shield", "panel_title": "TV"})
        r = await hass.config_entries.options.async_configure(r["flow_id"], new)
        assert r["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options["client_names"]["192.168.1.41"] == "Living Room Shield"
    assert hass.states.get("sensor.hdhomerun_tuner_tuner_3_channel") is not None


async def test_error_tally_and_reset(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    t3, total = "sensor.hdhomerun_tuner_tuner_3_error_total", "sensor.hdhomerun_tuner_error_total"
    assert hass.states.get(t3).state == "4"                 # first poll's 4 new errors
    assert hass.states.get(total).state == "4"
    assert hass.states.get(t3).attributes["since"]
    await entry.runtime_data.async_refresh()                # another poll: +4
    await hass.async_block_till_done()
    assert hass.states.get(t3).state == "8" and hass.states.get(total).state == "8"
    await hass.services.async_call("button", "press",
                                   {"entity_id": "button.hdhomerun_tuner_reset_error_totals"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(t3).state == "0" and hass.states.get(total).state == "0"


async def test_error_tally_survives_restart(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(hass, ((
        State("sensor.hdhomerun_tuner_tuner_3_error_total", "100", {"since": "2026-11-01T00:00:00+00:00"}),
        {"native_value": 100, "native_unit_of_measurement": "errors"}),))
    await _setup(hass)
    st = hass.states.get("sensor.hdhomerun_tuner_tuner_3_error_total")
    assert st.state == "104" and st.attributes["since"] == "2026-11-01T00:00:00+00:00"   # restored 100 + this poll's 4
    assert hass.states.get("sensor.hdhomerun_tuner_error_total").state == "104"


async def test_players_and_freeze_webhook(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _setup(hass)
    st = hass.states.get
    assert st("binary_sensor.living_room_tv_playing").state == "on"
    assert st("sensor.living_room_tv_channel").state == "6.1 STAR6HD"
    assert st("binary_sensor.bedroom_tv_playing").state == "off"
    assert st("sensor.den_tv_channel").state == "idle"
    assert st("binary_sensor.living_room_tv_frozen") is None          # no detector has reported yet
    client = await hass_client_no_auth()
    url = f"/api/webhook/{entry.data['webhook_id']}"
    r = await client.post(url, json={"player": "192.168.1.41:5555", "event": "freeze", "detail": "no frames 15 s"})
    assert r.status == 200
    await hass.async_block_till_done()
    assert st("binary_sensor.living_room_tv_frozen").state == "on"
    assert st("sensor.living_room_tv_freezes_today").state == "1"
    assert st("sensor.living_room_tv_last_freeze").attributes["detail"] == "no frames 15 s"
    r = await client.post(url, json={"player": "192.168.1.41", "event": "recovered"})
    assert r.status == 200
    await hass.async_block_till_done()
    assert st("binary_sensor.living_room_tv_frozen").state == "off"
    assert st("sensor.living_room_tv_freezes_today").state == "1"
    assert (await client.post(url, json={"player": "not-an-ip", "event": "freeze"})).status == 400
    assert (await client.post(url, json={"player": "192.168.1.41", "event": "explode"})).status == 400
    assert (await client.post(url, data="not json")).status == 400


async def test_schedule_calendar_and_conflict(hass: HomeAssistant) -> None:
    global SCHEDULE
    SCHEDULE = [(3600, 7200, "Show A", "6.1"), (5000, 9000, "Show B", "4.1")]
    await _setup(hass)
    st = hass.states.get
    assert st("sensor.hdhomerun_dvr_upcoming_recordings").state == "2"
    assert st("sensor.hdhomerun_dvr_next_recording").attributes["title"] == "Show A"
    assert st("sensor.hdhomerun_dvr_peak_tuners_needed").state == "2"
    assert st("binary_sensor.hdhomerun_dvr_tuner_conflict").state == "off"
    resp = await hass.services.async_call("calendar", "get_events",
                                          {"entity_id": "calendar.hdhomerun_dvr_recordings", "duration": {"hours": 24}},
                                          blocking=True, return_response=True)
    events = resp["calendar.hdhomerun_dvr_recordings"]["events"]
    assert [e["summary"] for e in events] == ["Show A", "Show B"] and events[0]["location"] == "ch 6.1"
    SCHEDULE = []


async def test_tuner_conflict_on(hass: HomeAssistant) -> None:
    global SCHEDULE
    SCHEDULE = [(86400, 90000, f"Show {i}", f"{i}.1") for i in range(5)]       # 5 at once, 4 tuners
    await _setup(hass)
    c = hass.states.get("binary_sensor.hdhomerun_dvr_tuner_conflict")
    assert c.state == "on" and c.attributes["tuners"] == 4 and c.attributes["conflicts"][0]["needed"] == 5
    assert len(c.attributes["conflicts"][0]["shows"]) == 5
    SCHEDULE = []


async def test_record_service(hass: HomeAssistant) -> None:
    await _setup(hass)
    RECORDED.clear()
    r = await hass.services.async_call(DOMAIN, "record", {"show": "night report", "channel": "Summit"},
                                       blocking=True, return_response=True)
    assert r["ok"] and RECORDED == [("night report", "series", "4.1")]          # "Summit" resolved to 4.1
    assert r["message"].startswith("Recording every new airing of Night Report on channel 4.1.")
    r = await hass.services.async_call(DOMAIN, "record", {"show": "night report", "mode": "next"},
                                       blocking=True, return_response=True)
    assert r["ok"] and r["message"].startswith("Recording the next airing of Night Report.")
    r = await hass.services.async_call(DOMAIN, "record", {"show": "x", "channel": "nowhere"},
                                       blocking=True, return_response=True)
    assert not r["ok"] and "couldn't find channel" in r["message"]


async def _tune_setup(hass: HomeAssistant, on_command):
    entry = await _setup(hass)
    with patch("custom_components.hdhr.hdhr_api.Network", FakeNetwork):     # the options change reloads the entry
        hass.config_entries.async_update_entry(entry, options={**entry.options,
                                                               "player_remotes": "Living Room TV=remote.shield"})
        await hass.async_block_till_done()
    calls = []

    async def send_command(call):
        calls.append(("send_command", dict(call.data)))
        on_command(call.data)

    async def turn_on(call):
        calls.append(("turn_on", dict(call.data)))

    hass.services.async_register("remote", "send_command", send_command)
    hass.services.async_register("remote", "turn_on", turn_on)
    return calls


async def _no_sleep(_):
    return None


async def test_tune_by_number(hass: HomeAssistant) -> None:
    saved = list(SNAP["sessions"])
    typed = []

    def on_command(data):
        typed.append(data["command"])
        if data["command"] == "ENTER" and typed[-2:-1] == [["DEL", "4"]]:     # main number, then Enter (Search Channels box)
            SNAP["sessions"] = [["192.168.1.30", "4.1", "192.168.1.41", "live"]]

    try:
        calls = await _tune_setup(hass, on_command)
        with patch("custom_components.hdhr.services.asyncio.sleep", _no_sleep):
            r = await hass.services.async_call(DOMAIN, "tune", {"player": "living room", "channel": "Summit"},
                                               blocking=True, return_response=True)
        assert r["ok"] and r["method"] == "number" and r["message"] == "Living Room TV is now on 4.1 SUMMIT4 (Night Report Live)."
        assert calls == [("send_command", {"entity_id": "remote.shield", "command": ["4"], "delay_secs": 0.4}),
                         ("send_command", {"entity_id": "remote.shield", "command": ["DEL", "4"], "delay_secs": 0.4}),
                         ("send_command", {"entity_id": "remote.shield", "command": "ENTER"})]
    finally:
        SNAP["sessions"] = saved


async def test_tune_falls_back_to_steps(hass: HomeAssistant) -> None:
    saved, saved_lineup = list(SNAP["sessions"]), dict(SNAP["lineup"])
    SNAP["lineup"]["6.2"] = "ARENA"

    def on_command(data):
        if data["command"] == "CHANNEL_UP":
            SNAP["sessions"] = [["192.168.1.30", "6.2", "192.168.1.41", "live"]]

    try:
        calls = await _tune_setup(hass, on_command)
        with patch("custom_components.hdhr.services.asyncio.sleep", _no_sleep):
            r = await hass.services.async_call(DOMAIN, "tune", {"player": "Living Room", "channel": "six point two"},
                                               blocking=True, return_response=True)
        assert r["ok"] and r["method"] == "steps", r
        assert [c[1]["command"] for c in calls] == ["CHANNEL_UP"]      # already on 6.x: no number, just one step
    finally:
        SNAP["sessions"], SNAP["lineup"] = saved, saved_lineup


async def test_tune_backs_out_of_a_menu_once(hass: HomeAssistant) -> None:
    saved = list(SNAP["sessions"])
    seen = []

    def on_command(data):
        seen.append(data["command"])
        if data["command"] == "ENTER" and ["DPAD_LEFT", "BACK"] in seen:   # only after Left + Back closed the box
            SNAP["sessions"] = [["192.168.1.30", "4.1", "192.168.1.41", "live"]]

    try:
        await _tune_setup(hass, on_command)
        with patch("custom_components.hdhr.services.asyncio.sleep", _no_sleep):
            r = await hass.services.async_call(DOMAIN, "tune", {"player": "living room", "channel": "4.1"},
                                               blocking=True, return_response=True)
        assert r["ok"] and seen == [["4"], ["DEL", "4"], "ENTER", ["DPAD_LEFT", "BACK"], ["4"], ["DEL", "4"], "ENTER"], (r, seen)
    finally:
        SNAP["sessions"] = saved


async def test_tune_number_then_step(hass: HomeAssistant) -> None:
    """4.1 -> 6.2: the number lands on 6.1 (the first 5.x), then one Channel Up - no waiting for 6.2 after the number."""
    saved, saved_lineup = list(SNAP["sessions"]), dict(SNAP["lineup"])
    SNAP["sessions"] = [["192.168.1.30", "4.1", "192.168.1.41", "live"]]
    SNAP["lineup"]["6.2"] = "ARENA"
    seen = []

    def on_command(data):
        seen.append(data["command"])
        if data["command"] == "ENTER" and seen[-2:-1] == [["DEL", "6"]]:
            SNAP["sessions"] = [["192.168.1.30", "6.1", "192.168.1.41", "live"]]
        if data["command"] == "CHANNEL_UP":
            SNAP["sessions"] = [["192.168.1.30", "6.2", "192.168.1.41", "live"]]

    try:
        await _tune_setup(hass, on_command)
        with patch("custom_components.hdhr.services.asyncio.sleep", _no_sleep):
            r = await hass.services.async_call(DOMAIN, "tune", {"player": "living room", "channel": "6.2"},
                                               blocking=True, return_response=True)
        assert r["ok"] and r["method"] == "steps" and seen == [["6"], ["DEL", "6"], "ENTER", "CHANNEL_UP"], (r, seen)
    finally:
        SNAP["sessions"], SNAP["lineup"] = saved, saved_lineup


async def test_newer_tune_stops_the_older_one(hass: HomeAssistant) -> None:
    """Watch on 27.x, then Watch on 17.1 thirteen seconds later - both used to type into the TV at once. Now the
    older change stops before its next key and the newer one runs alone (its message is the one that counts)."""
    saved = list(SNAP["sessions"])
    seen, second = [], []

    def on_command(data):
        seen.append(data["command"])
        if data["command"] == ["4"] and not second:          # the first change has typed its number: second press
            second.append(hass.async_create_task(hass.services.async_call(
                DOMAIN, "tune", {"player": "living room", "channel": "6.1"}, blocking=True, return_response=True)))

    try:
        await _tune_setup(hass, on_command)
        with patch("custom_components.hdhr.services.asyncio.sleep", _no_sleep):
            first = await hass.services.async_call(DOMAIN, "tune", {"player": "living room", "channel": "4.1"},
                                                   blocking=True, return_response=True)
            newer = await second[0]
        assert first["superseded"] and not first["ok"], first
        assert newer["ok"] and newer["message"].startswith("Living Room TV is already on 6.1"), newer
        assert ["DPAD_LEFT", "BACK"] not in seen, seen         # the older change sent nothing after the newer one came
    finally:
        SNAP["sessions"] = saved


async def test_tune_sees_a_direct_tuner_session(hass: HomeAssistant) -> None:
    """Another TV already has 9.1 through the DVR, so the Shield takes a tuner directly; the DVR's list
    never shows that. The confirmation must still see it land (no back-out, no false failure)."""
    saved = list(SNAP["sessions"])
    seen = []

    def on_command(data):
        seen.append(data["command"])
        if data["command"] == "ENTER":
            SNAP["sessions"] = [["192.168.1.30", "4.1", "192.168.1.42", "live"],     # Bedroom via the DVR
                                ["", "4.1", "192.168.1.41", "direct"]]                # the Shield, straight from a tuner

    try:
        await _tune_setup(hass, on_command)
        with patch("custom_components.hdhr.services.asyncio.sleep", _no_sleep):
            r = await hass.services.async_call(DOMAIN, "tune", {"player": "living room", "channel": "4.1"},
                                               blocking=True, return_response=True)
        assert r["ok"] and seen == [["4"], ["DEL", "4"], "ENTER"], (r, seen)
    finally:
        SNAP["sessions"] = saved


async def test_tune_errors(hass: HomeAssistant) -> None:
    await _tune_setup(hass, lambda data: None)
    call = lambda player, ch: hass.services.async_call(DOMAIN, "tune", {"player": player, "channel": ch},  # noqa: E731
                                                       blocking=True, return_response=True)
    assert "don't know a TV called" in (await call("kitchen", "4.1"))["message"]
    assert "couldn't find a channel or a show" in (await call("living room", "99.9"))["message"]
    assert "has no remote set up" in (await call("den", "4.1"))["message"]
    assert (await call("living room", "6.1"))["message"] == "Living Room TV is already on 6.1 STAR6HD (Evening News)."


SIMUL = {"airs_on": ["4.1", "104.1"], "channels": ["4.1", "104.1"], "channel": "4.1 / 104.1", "series_id": "DL"}


async def test_best_feed_pins_the_reliable_channel(hass: HomeAssistant) -> None:
    global SCHEDULE
    SCHEDULE = [(86400, 90000, "Night Report Live", "4.1 / 104.1", SIMUL)]
    PINNED.clear()
    entry = await _setup(hass)
    co = entry.runtime_data
    t = time.time() - 600
    co.feed["samples"] = {"4.1": [[t, True]] * 12, "104.1": [[t, i % 3 != 0] for i in range(12)]}   # 12/12 vs 8/12
    await co.async_feed_cycle()
    await hass.async_block_till_done()
    assert PINNED == [("DL", "4.1")] and co.feed["pinned"] == {"DL": "4.1"}
    st = hass.states.get("sensor.hdhomerun_dvr_feed_choices")
    assert st.state == "1" and "best: 4.1 (4.1 12/12, 104.1 8/12)" in st.attributes["decisions"][0]["why"]
    assert "4.1" in hass.states.get("sensor.hdhomerun_dvr_feed_reliability").attributes
    SCHEDULE = []


async def test_best_feed_leaves_manual_rules_and_ties_alone(hass: HomeAssistant) -> None:
    global SCHEDULE
    PINNED.clear()
    # a rule someone limited by hand to 104.1: not ours to change, whatever the data says
    SCHEDULE = [(86400, 90000, "Night Report Live", "104.1", dict(SIMUL, channels=["104.1"], rule_channels=["104.1"]))]
    entry = await _setup(hass)
    co = entry.runtime_data
    t = time.time() - 600
    co.feed["samples"] = {"4.1": [[t, True]] * 12, "104.1": [[t, False]] * 12}
    await co.async_feed_cycle()
    assert PINNED == [] and co.feed_groups() == {}
    # a tie: no decision
    co.schedule = [dict(s) for s in co.schedule]
    co.schedule[0].update(rule_channels=None, channels=["4.1", "104.1"])
    co.feed["samples"]["104.1"] = [[t, True]] * 12
    await co.async_feed_cycle()
    assert PINNED == [] and "no clear winner" in co.feed["decisions"]["DL"]["why"]
    # switched off: nothing runs
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.hdhomerun_dvr_pick_best_feed"},
                                   blocking=True)
    co.feed["samples"]["104.1"] = [[t, False]] * 12
    await co.async_feed_cycle()
    assert PINNED == [] and not co.feed["enabled"]
    SCHEDULE = []


async def test_now_playing_and_guide_ws(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    st = hass.states.get
    assert st("sensor.living_room_tv_now_playing").state == "Evening News"            # Shield on 6.1
    assert st("sensor.den_tv_now_playing").state == "Nothing"
    assert st("sensor.hdhomerun_tuner_tuner_3_program").state == "Evening News"
    assert st("sensor.hdhomerun_tuner_tuner_0_program").state == "idle"
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "hdhr/guide"})
    g = (await client.receive_json())["result"]
    assert [c["number"] for c in g["channels"]] == ["4.1", "6.1", "6.2", "104.1"]
    assert g["watching"] == {"6.1": ["Living Room TV"]}
    assert [p["t"] for p in g["channels"][1]["programs"]] == ["Evening News", "Judge Hour"]          # the next 6 hours
    # the browse arrows: a later window gets only what overlaps it
    await client.send_json({"id": 2, "type": "hdhr/guide", "start": time.time() + 3600, "hours": 1})
    w = (await client.receive_json())["result"]
    assert {c["number"]: [p["t"] for p in c["programs"]] for c in w["channels"]} == {
        "4.1": [], "6.1": ["Judge Hour"], "6.2": [], "104.1": []}
    assert w["last"] == max(p["e"] for c in g["channels"] for p in c["programs"])
    await client.send_json({"id": 3, "type": "hdhr/guide_search", "query": "judge"})
    sr = (await client.receive_json())["result"]
    assert [(r["number"], r["t"]) for r in sr["results"]] == [("6.1", "Judge Hour")]
    assert sr["watching"] == {"6.1": ["Living Room TV"]}
    await client.send_json({"id": 4, "type": "hdhr/layout"})
    lay = (await client.receive_json())["result"]
    assert {p["name"]: p["tunable"] for p in lay["players"]}["Den TV"] is False


async def test_tune_by_show_name(hass: HomeAssistant) -> None:
    saved = list(SNAP["sessions"])
    typed = []

    def on_command(data):
        typed.append(data["command"])
        if data["command"] == "ENTER" and typed[-2:-1] == [["DEL", "4"]]:
            SNAP["sessions"] = [["192.168.1.30", "4.1", "192.168.1.41", "live"]]

    try:
        await _tune_setup(hass, on_command)
        with patch("custom_components.hdhr.services.asyncio.sleep", _no_sleep):
            # Night Report airs on 4.1 AND 104.1 (simulcast): no feed data yet, so the lower channel number
            r = await hass.services.async_call(DOMAIN, "tune", {"player": "living room", "channel": "night report"},
                                               blocking=True, return_response=True)
            assert r["ok"] and r["message"] == "Living Room TV is now on 4.1 SUMMIT4 (Night Report Live).", r
            # "evening n" only partly matches two different shows on now (Evening News, Evening Newsbreak): ask which.
            # (An exact title like "evening news" would win outright.)
            SNAP["lineup"]["6.2"] = "ARENA"
            r = await hass.services.async_call(DOMAIN, "tune", {"player": "living room", "channel": "evening n"},
                                               blocking=True, return_response=True)
            assert not r["ok"] and "More than one show matches" in r["message"], r
    finally:
        SNAP["sessions"] = saved
        SNAP["lineup"].pop("6.2", None)


async def test_record_from_a_guide_listing(hass: HomeAssistant) -> None:
    await _setup(hass)
    RECORDED.clear()
    r = await hass.services.async_call(DOMAIN, "record", {"series_id": "JM", "mode": "next", "start": 1794100000,
                                                         "channel": "6.1", "show": "Judge Hour"},
                                       blocking=True, return_response=True)
    assert r["ok"] and RECORDED == [("exact", "JM", "next", 1794100000, "6.1")]
    assert r["message"].startswith("Recording the next airing of Judge Hour.")
    r = await hass.services.async_call(DOMAIN, "record", {"series_id": "JM", "mode": "series"},
                                       blocking=True, return_response=True)
    assert r["ok"] and r["message"].startswith("Recording every new airing of Judge Hour")    # title from the guide


async def test_single_instance(hass: HomeAssistant) -> None:
    await _setup(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
