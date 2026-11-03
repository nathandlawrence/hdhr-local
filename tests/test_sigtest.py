"""The interference test (sigtest.py) in a test Home Assistant: phases follow the dryer, spare tuners only, the report."""
import threading
import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hdhr import sigtest
from custom_components.hdhr.const import DOMAIN

DRYER = "binary_sensor.dryer_power"
ME = "10.0.0.23"                  # this machine as the tuner sees it
HOLDS: list = []                   # (channel, stop event) per hold the test opened


def fake_hold(ip, ch, stop, on_open=None, port=5004):
    HOLDS.append((ch, stop))
    if on_open:
        on_open(ME)
    stop.wait(5)
    return "stopped"


class Co:
    def __init__(self):
        self.data, self.schedule, self.sigtest, self.sigtest_last, self.updates = None, [], None, None, 0

    def async_update_listeners(self):
        self.updates += 1


def snap(tuners: dict) -> dict:
    """tuners: {n: (vct, target, new_err, mhz)}; the rest idle."""
    st = {f"t{n}": {"vct": None, "target": None, "new_err": 0, "freq_mhz": None} for n in range(4)}
    for n, (vct, target, err, mhz) in tuners.items():
        st[f"t{n}"] = {"vct": vct, "target": target, "new_err": err, "freq_mhz": mhz}
    return {"devices": {"T": {"kind": "tuner", "ok": True, "base": "http://10.0.0.24", "count": 4, "state": st}}}


def _join_holds():
    for th in threading.enumerate():
        if th.name.startswith("hdhr-sigtest-"):
            th.join(2)


async def _wait(cond, secs=2.0):
    end = time.time() + secs
    while not cond() and time.time() < end:
        time.sleep(0.01)


async def test_dryer_run_end_to_end(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "persistent_notification", {})
    HOLDS.clear()
    clock = [1_000_000.0]
    co = Co()
    hass.states.async_set(DRYER, "on")
    with patch.object(sigtest, "_now", lambda: clock[0]), \
            patch("custom_components.hdhr.hdhr_api.hold_channel", fake_hold):
        t = sigtest.SignalTest(hass, co, "Dryer", DRYER, ["9.1", "4.1", "6.1"], 15, 3, None)
        co.sigtest = t
        t.start()
        assert t.phase == "on"
        tv = {3: ("17.1", "10.0.0.21", 0, 593.0)}                  # the Living Room on 17.1
        t.tick(snap(tv))                                            # 3 free: hold 9.1 and 4.1, leave one free
        assert sorted(t.holds) == ["4.1", "9.1"] and t.notes["6.1"] == "waiting for a spare tuner"
        await _wait(lambda: t.my_ip == ME)
        for err in (40, 0, 77, 3):                                  # dryer on: 9.1 (VHF) has bursts, 4.1 (UHF) none
            clock[0] += 30
            t.tick(snap({0: ("9.1", ME, err, 195.0), 1: ("4.1", ME, 0, 551.0), **tv}))
        hass.states.async_set(DRYER, "off")
        await hass.async_block_till_done()
        assert t.phase == "after"
        for _ in range(4):                                          # dryer off: clean
            clock[0] += 30
            t.tick(snap({0: ("9.1", ME, 0, 195.0), 1: ("4.1", ME, 0, 551.0), **tv}))
        t.finish("done")                                           # (what the 15-minute timer does)
        await hass.async_block_till_done()
    _join_holds()
    assert co.sigtest is None and co.sigtest_last["ended"] == "done"
    assert all(stop.is_set() for _ch, stop in HOLDS)                # every held tuner given back
    rows = {r["channel"]: r for r in co.sigtest_last["channels"]}
    assert rows["9.1"]["on"]["errors"] == 120 and rows["9.1"]["off"]["errors"] == 0 and rows["9.1"]["band"] == "VHF"
    assert rows["4.1"]["on"]["errors"] == 0 and rows["4.1"]["band"] == "UHF"
    assert rows["9.1"]["on"]["tuner_min"] == 2.0 and rows["9.1"]["off"]["tuner_min"] == 2.0
    v = co.sigtest_last["verdict"]
    assert "points to the dryer" in v and "Only VHF" in v, v
    assert "9.1 (VHF 195 MHz): 120 errors with the dryer on" in co.sigtest_last["summary"]
    assert "Not measured: 6.1" in co.sigtest_last["summary"]
    assert [b[1:] for b in co.sigtest_last["bursts"]] == [["9.1", 0, 40, "on"], ["9.1", 0, 77, "on"], ["9.1", 0, 3, "on"]]


async def test_gives_tuners_back(hass: HomeAssistant) -> None:
    """Never in the way: a hold goes when no tuner is left free, when a recording is due, or when a TV is on that
    channel anyway (then that TV's tuner is measured)."""
    await async_setup_component(hass, "persistent_notification", {})
    HOLDS.clear()
    clock = [2_000_000.0]
    co = Co()
    hass.states.async_set(DRYER, "on")
    with patch.object(sigtest, "_now", lambda: clock[0]), \
            patch("custom_components.hdhr.hdhr_api.hold_channel", fake_hold):
        t = sigtest.SignalTest(hass, co, "Dryer", DRYER, ["9.1", "4.1", "6.1"], 15, 3, None)
        co.sigtest = t
        t.start()
        t.tick(snap({}))                                            # 4 free: 9.1, 4.1, 6.1 held, one left
        assert sorted(t.holds) == ["4.1", "6.1", "9.1"]
        await _wait(lambda: t.my_ip == ME)
        held = {0: ("9.1", ME, 0, 195.0), 1: ("4.1", ME, 0, 551.0), 2: ("6.1", ME, 0, 207.0)}
        clock[0] += 30
        t.tick(snap({**held, 3: ("17.1", "10.0.0.21", 0, 593.0)}))  # a TV took the last tuner: drop the lowest
        assert sorted(t.holds) == ["4.1", "9.1"] and t.notes["6.1"] == "released: no tuner left free"
        clock[0] += 30
        co.schedule = [{"start": clock[0] + 60}]                    # a recording in a minute
        t.tick(snap({0: held[0], 1: held[1], 3: ("17.1", "10.0.0.21", 0, 593.0)}))
        assert sorted(t.holds) == ["9.1"]
        co.schedule = []
        clock[0] += 30
        t.tick(snap({0: held[0], 2: ("9.1", "10.0.0.22", 0, 195.0), 3: ("17.1", "10.0.0.21", 0, 593.0)}))
        assert "9.1" not in t.holds and t.notes["9.1"] == "measured from a TV's tuner"
        res = t.finish("stopped by hand")
    _join_holds()
    assert res["ended"] == "stopped by hand" and not t.holds


def test_verdicts() -> None:
    t = sigtest.SignalTest.__new__(sigtest.SignalTest)
    t.name = "Dryer"

    def row(ch, band, on, off, by_tuner=None):
        return {"channel": ch, "band": band, "mhz": 195.0, "on": {"errors": on, "tuner_min": 30, "by_tuner": by_tuner or {"0": on}},
                "off": {"errors": off, "tuner_min": 15, "by_tuner": {}}}
    assert "didn't disturb" in t._verdict([row("9.1", "VHF", 0, 0)])
    assert "not the dryer alone" in t._verdict([row("9.1", "VHF", 60, 40)])
    assert "points to that tuner" in t._verdict([row("9.1", "VHF", 90, 0, {"2": 90, "0": 0})])
    assert "UHF was hit too" in t._verdict([row("9.1", "VHF", 90, 0), row("4.1", "UHF", 30, 0)])


async def test_action_start_and_stop(hass: HomeAssistant) -> None:
    from tests.test_integration import _setup
    await async_setup_component(hass, "persistent_notification", {})
    HOLDS.clear()
    with patch("custom_components.hdhr.hdhr_api.hold_channel", fake_hold):
        await _setup(hass)
        hass.states.async_set(DRYER, "on")
        r = await hass.services.async_call(DOMAIN, "signal_test", {"watch_entity": DRYER, "channels": ["9.1", "4.1", "6.1"], "notify": "notify.nobody"},
                                           blocking=True, return_response=True)
        assert r["ok"] and r["message"] == "Dryer test started (on); channels 9.1, 4.1, 6.1.", r
        hass.config_entries.async_entries(DOMAIN)[0].runtime_data.async_update_listeners()
        await hass.async_block_till_done()
        assert hass.states.get("sensor.hdhomerun_tuner_signal_test").state == "running (dryer on)"
        again = await hass.services.async_call(DOMAIN, "signal_test", {"watch_entity": DRYER}, blocking=True,
                                               return_response=True)
        assert not again["ok"] and "already running" in again["message"]
        r = await hass.services.async_call(DOMAIN, "signal_test", {"stop": True}, blocking=True, return_response=True)
        assert r["ok"] and "(stopped by hand)" in r["message"], r
        await hass.async_block_till_done()
        assert hass.states.get("sensor.hdhomerun_tuner_signal_test").state == "done (stopped by hand)"
    _join_holds()
    assert all(stop.is_set() for _ch, stop in HOLDS)
