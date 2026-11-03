"""Offline tests for custom_components/hdhr/hdhr_api.py (no network, no Home Assistant). Run under pytest."""
import importlib.util
import json
import time
import pathlib
import struct
import types

_p = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "hdhr" / "hdhr_api.py"
_spec = importlib.util.spec_from_file_location("hdhr_api_standalone", _p)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

DBG = ("tun: ch=8vsb:207000000 lock=8vsb:207000000 ss=100 snq=100 seq=100 dbg=52-177/-444\n"
       "dev: bps=19251200 resync=0 overflow=0\nts:  bps=5512160 te=7 crc=2\nnet: bps=5512960 pps=472 err=0 stop=0\n")


def test_parse_debug():
    d = h.parse_debug(DBG)
    assert d["tun.ss"] == "100" and (d["ts.te"], d["ts.crc"]) == ("7", "2") and d["tun.ch"] == "8vsb:207000000"
    assert h.parse_debug(None) == {} and h.parse_debug("none") == {}


def test_discovery_packets():
    assert h._parse_packet(h.discover_request(), h.DISCOVER_REQ) is not None
    reply = h._packet(h.DISCOVER_RPY, h._tlv(h.TAG_DEVICE_TYPE, struct.pack(">I", h.TYPE_STORAGE))
                      + h._tlv(h.TAG_BASE_URL, b"http://192.168.1.30:59090\0"))
    base, types = h.parse_discover_reply(reply, "192.168.1.30")
    assert base == "http://192.168.1.30:59090" and h.TYPE_STORAGE in types
    bad = bytearray(reply)
    bad[-1] ^= 0xFF
    assert h.parse_discover_reply(bytes(bad), "x") is None
    plain = h._packet(h.DISCOVER_RPY, h._tlv(h.TAG_DEVICE_TYPE, struct.pack(">I", h.TYPE_TUNER)))
    assert h.parse_discover_reply(plain, "192.168.1.20")[0] == "http://192.168.1.20"
    assert h._parse_packet(h._packet(9, h._tlv(4, b"x" * 300)), 9)[4][0] == b"x" * 300


def test_dvr_id_is_short_block():
    d = h.Dvr("http://192.168.1.30:59090", {"StorageID": "70000001-8BA8-DAFC-C83A-94B7B4D3DAE3"})
    assert d.id == "70000001"


def test_viewer_mapping():
    names = {"192.168.1.41": "Shield"}.get
    cn = lambda ip: names(ip, ip)  # noqa: E731
    S = [("192.168.1.30", "6.1", "192.168.1.41", "live"), ("192.168.1.30", "4.1", "192.168.1.42", "live")]
    assert h.viewer_for("6.1", "192.168.1.30", S, cn) == "Shield"
    assert h.viewer_for("4.1", "192.168.1.30", S, cn) == "192.168.1.42"
    assert h.viewer_for("9.3", "192.168.1.41", S, cn) == "Shield"            # tuned directly
    assert h.viewer_for("7.1", "192.168.1.30", S, cn) == "DVR"
    assert h.viewer_for("7.1", "192.168.1.30", S + [("192.168.1.30", "7.1", None, "record")], cn) == "DVR record"


def test_real_network_scan_and_poll(monkeypatch):
    """The real Network class end to end, with fake devices (a misplaced function once hid Network.poll while only
    the stand-in network in the HA tests was being exercised)."""
    pages = {
        "http://t/discover.json": {"DeviceID": "AB", "TunerCount": 2, "ModelNumber": "HDFX-4K", "FriendlyName": "FLEX",
                                   "FirmwareVersion": "1", "DeviceAuth": "SECRET"},
        "http://t/status.json": [{"Resource": "tuner0", "VctNumber": "6.1", "VctName": "STAR", "TargetIP": "10.0.0.25",
                                  "SignalStrengthPercent": 90, "SignalQualityPercent": 80, "SymbolQualityPercent": 100},
                                 {"Resource": "tuner1", "VctNumber": "9.3", "TargetIP": "10.0.0.21",
                                  "SignalStrengthPercent": 70, "SignalQualityPercent": 60, "SymbolQualityPercent": 100}],
        "http://t/lineup.json": [{"GuideNumber": "6.1", "GuideName": "STAR"}, {"GuideNumber": "9.3", "GuideName": "Canyon"}],
        "http://d:59090/discover.json": {"StorageID": "7332-AA", "FriendlyName": "RECORD", "Version": "2",
                                         "TotalSpace": 100, "FreeSpace": 40},
        "http://d:59090/status.json": [{"Resource": "live", "Name": "Live channel 6.1", "TargetIP": "10.0.0.22"}],
        "http://d:59090/recorded_files.json": [],
    }
    monkeypatch.setattr(h, "http_json", lambda url, timeout=6.0: pages[url])
    monkeypatch.setattr(h, "scan_lan", lambda timeout=2.0: {})
    monkeypatch.setattr(h, "ctl_get", lambda ip, names, timeout=4.0: {n: "ts: bps=1000000 te=0 crc=0" for n in names})
    monkeypatch.setattr(h, "host_of", lambda base: {"http://t": "10.0.0.26", "http://d:59090": "10.0.0.25"}[base])
    net = h.Network(["http://t", "http://d:59090"])
    new = net.scan()
    assert sorted(d.kind for d in new) == ["dvr", "tuner"]
    snap = net.poll({"10.0.0.22": "Bedroom"}.get)
    assert set(snap) >= {"devices", "sessions", "clients", "lineup", "tuner_count"}
    assert snap["tuner_count"] == 2 and snap["lineup"] == {"6.1": "STAR", "9.3": "Canyon"}
    t = snap["devices"]["AB"]["state"]
    assert t["t0"]["viewer"] == "Bedroom" and t["t1"]["target"] == "10.0.0.21"
    assert ["", "9.3", "10.0.0.21", "direct"] in snap["sessions"]         # tuned directly, not via the DVR
    assert snap["clients"] == ["10.0.0.21", "10.0.0.22"]
    assert "SECRET" not in json.dumps(snap)
    # every method the HA side calls on the network must exist on the REAL class (a stand-in can hide a gap)
    # the fast check sees the direct-from-tuner client too (the DVR's list alone misses it)
    assert sorted(net.quick_sessions()) == [["", "9.3", "10.0.0.21", "direct"], ["10.0.0.25", "6.1", "10.0.0.22", "live"]]
    for name in ("scan", "poll", "schedule", "device_auth", "record", "quick_sessions", "lineup", "tuner_count",
                 "free_tuners", "probe", "rules", "pin_rule", "guide", "record_exact"):
        assert callable(getattr(h.Network, name, None)), name


def test_spoken_channels_and_players():
    L = {"4.1": "SUMMIT4", "6.1": "STAR6HD", "6.2": "ARENA", "36.2": "BEACON", "40.4": "RETRO", "11.1": "QUILL", "30.1": "TIDE",
         "22.1": "X"}
    cases = {"6.1": "6.1", "6 1": "6.1", "six point one": "6.1", "channel six point two": "6.2",
             "thirty six two": "36.2", "thirty six point two": "36.2", "channel 4": "4.1", "four": "4.1",
             "twenty two": "22.1", "Summit": "4.1", "star": "6.1", "retro": "40.4", "beacon": "36.2", "eleven": "11.1",
             "tide": "30.1", "nope": None}
    for text, want in cases.items():
        assert h.resolve_channel(text, L) == want, (text, h.resolve_channel(text, L))
    names = ["Living Room TV", "Bedroom TV", "Den TV"]
    assert h.resolve_player("living room", names) == names[0]
    assert h.resolve_player("the bedroom tv", names) == names[1]
    assert h.resolve_player("den", names) == names[2]
    assert h.resolve_player("tv", names) is None and h.resolve_player("kitchen", names) is None


def test_record_series_and_next():
    now = 1_000_000
    calls = []

    def get(url):
        if "/search?" in url:
            return [{"Title": "Night Report Extra", "SeriesID": "X"}, {"Title": "Night Report", "SeriesID": "D1"}]
        return [{"StartTime": now + 7200, "EndTime": now + 10800, "ChannelNumber": "4.1", "EpisodeTitle": "B"},
                {"StartTime": now + 3600, "EndTime": now + 7000, "ChannelNumber": "4.1", "EpisodeTitle": "A"},
                {"StartTime": now - 9000, "EndTime": now - 5000, "ChannelNumber": "4.1"}]

    def post(url):
        calls.append(url)
        return []

    r = h.record("AU TH", "night report", "series", None, now, get, post)
    assert r["ok"] and r["title"] == "Night Report" and r["series_id"] == "D1"            # exact title beats "Extra"
    assert r["next_airing"]["start"] == now + 3600 and r["next_airing"]["episode"] == "A"
    assert calls[-1] == "https://api.hdhomerun.com/api/recording_rules?DeviceAuth=AU%20TH&Cmd=add&SeriesID=D1"
    r = h.record("AU TH", "night report", "next", None, now, get, post)
    assert r["ok"] and calls[-1].endswith(f"Cmd=add&SeriesID=D1&DateTimeOnly={now + 3600}&ChannelOnly=4.1")
    assert not h.record("A", "night report", "next", "9.9", now, get, post)["ok"]          # no airing on 9.9
    assert not h.record("A", "zzz", "series", None, now, lambda u: [] if "/search?" in u else [], post)["ok"]


def test_fetch_schedule_padding_and_filter():
    now = 1_000_000
    rules = [{"SeriesID": "S1", "StartPadding": 60, "EndPadding": 300}, {"SeriesID": "S2"}]
    eps = {
        "S1": [{"RecordingRule": 1, "StartTime": now + 3600, "EndTime": now + 7200, "Title": "News", "ChannelNumber": "4.1"},
               {"RecordingRule": 0, "StartTime": now + 3600, "EndTime": now + 7200, "Title": "not flagged"},
               {"RecordingRule": 1, "StartTime": now - 7200, "EndTime": now - 3600, "Title": "already over"}],
        "S2": [{"RecordingRule": 1, "StartTime": now + 20 * 86400, "EndTime": now + 20 * 86400 + 60, "Title": "too far"},
               {"RecordingRule": 1, "StartTime": now + 60, "EndTime": now + 120, "Title": "Soon", "ChannelNumber": "6.1"}],
    }
    seen = []

    def get(url):
        seen.append(url)
        if "recording_rules" in url:
            return rules
        return eps[url.rsplit("SeriesID=", 1)[1]]

    out = h.fetch_schedule("AUTH", now, 14, get)
    assert [s["title"] for s in out] == ["Soon", "News"]
    news = out[1]
    assert (news["start"], news["end"]) == (now + 3600 - 60, now + 7200 + 300)      # padded by the rule
    assert all(u.startswith("https://api.hdhomerun.com/api/") for u in seen)


def test_schedule_merges_simulcasts_and_respects_channel_only():
    """Night Report shows twice (4.1 and its NextGen simulcast 104.1) - one recording, so one entry; a team rule's
    ChannelOnly list must keep out airings on channels it doesn't name."""
    now = 1_000_000
    rules = [{"SeriesID": "DL", "RecentOnly": 1}, {"SeriesID": "NFL", "ChannelOnly": "4.1|6.1"}]
    eps = {"DL": [{"RecordingRule": 1, "ProgramID": "SH1", "StartTime": now + 3600, "EndTime": now + 7200,
                   "Title": "Night Report Live", "ChannelNumber": ch} for ch in ("104.1", "4.1")],
           "NFL": [{"RecordingRule": 1, "ProgramID": "EP9", "StartTime": now + 9000, "EndTime": now + 20000,
                    "Title": "Pro Football", "ChannelNumber": "104.1"},
                   {"RecordingRule": 1, "ProgramID": "EP8", "StartTime": now + 30000, "EndTime": now + 40000,
                    "Title": "Pro Football", "ChannelNumber": "6.1"}]}
    get = lambda url: rules if "recording_rules" in url else eps[url.rsplit("SeriesID=", 1)[1]]  # noqa: E731
    out = h.fetch_schedule("A", now, 14, get)
    assert [(s["title"], s["channel"]) for s in out] == [("Night Report Live", "4.1 / 104.1"), ("Pro Football", "6.1")]
    assert out[0]["channels"] == ["4.1", "104.1"]
    assert h.conflicts(out, 4)[0] == 1          # the simulcast no longer counts as two tuners


def test_choose_feed_uses_data_only():
    ok = lambda n, good: [[i, i < good] for i in range(n)]  # noqa: E731
    # not enough results yet: no decision, keep what is there
    assert h.choose_feed(["4.1", "104.1"], {"4.1": ok(12, 12), "104.1": ok(5, 5)})[0] is None
    # clear winner (12/12 vs 8/12 - like a NextGen simulcast's 807 failures)
    ch, why = h.choose_feed(["4.1", "104.1"], {"4.1": ok(12, 12), "104.1": ok(12, 8)})
    assert ch == "4.1" and "4.1 12/12" in why and "104.1 8/12" in why
    # tie: no data-based reason to pick either - nothing changes
    assert h.choose_feed(["4.1", "104.1"], {"4.1": ok(12, 12), "104.1": ok(12, 12)})[0] is None
    # the pinned channel stays unless the other is clearly better ...
    assert h.choose_feed(["4.1", "104.1"], {"4.1": ok(40, 39), "104.1": ok(40, 40)}, current="4.1")[0] == "4.1"
    # ... and moves when it is
    assert h.choose_feed(["4.1", "104.1"], {"4.1": ok(20, 14), "104.1": ok(20, 20)}, current="4.1")[0] == "104.1"


def test_probe_channel(monkeypatch):
    class Resp:
        def __init__(self, n):
            self.left = n

        def read(self, k):
            k = min(k, self.left)
            self.left -= k
            return b"x" * k

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    urls = []
    monkeypatch.setattr(h.urllib.request, "urlopen", lambda url, timeout=0: urls.append(url) or Resp(3_000_000))
    r = h.probe_channel("10.0.0.26", "4.1")
    assert r["ok"] and r["bytes"] == 1_500_000 and urls == ["http://10.0.0.26:5004/auto/v4.1?duration=8"]

    def fail(url, timeout=0):
        import io
        raise h.urllib.error.HTTPError(url, 503, "x", {}, io.BytesIO(b"807 No Video Data"))

    monkeypatch.setattr(h.urllib.request, "urlopen", fail)
    r = h.probe_channel("10.0.0.26", "104.1")
    assert not r["ok"] and r["error"] == "503 807 No Video Data"


def test_schedule_keeps_all_simulcast_channels_after_a_pin():
    """Once a rule is pinned to 4.1 the cloud may stop flagging the 104.1 copy; airs_on must still list it so the
    choice can be re-checked later."""
    now = 1_000_000
    rules = [{"SeriesID": "DL", "ChannelOnly": "4.1"}]
    eps = [{"RecordingRule": 1, "ProgramID": "SH1", "StartTime": now + 3600, "EndTime": now + 7200,
            "Title": "Night Report Live", "ChannelNumber": "4.1"},
           {"ProgramID": "SH1", "StartTime": now + 3600, "EndTime": now + 7200, "Title": "Night Report Live",
            "ChannelNumber": "104.1"}]
    get = lambda url: rules if "recording_rules" in url else eps  # noqa: E731
    out = h.fetch_schedule("A", now, 14, get)
    assert len(out) == 1 and out[0]["channels"] == ["4.1"] and out[0]["airs_on"] == ["4.1", "104.1"]
    assert out[0]["rule_channels"] == ["4.1"]


def _xmltv(channels, programmes) -> bytes:
    """A small XMLTV document shaped like SiliconDust's feed. channels = [(id, lcn, [display names])],
    programmes = [(channel id, start, stop, title, episode, series id, description)] with unix-second times."""
    ts = lambda sec: time.strftime("%Y%m%d%H%M%S", time.gmtime(sec)) + " +0000"  # noqa: E731
    out = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for cid, lcn, names in channels:
        out.append(f'<channel id="{cid}">' + "".join(f"<display-name>{n}</display-name>" for n in names)
                   + (f"<lcn>{lcn}</lcn>" if lcn else "") + "</channel>")
    for cid, s, e, title, ep, sid, desc in programmes:
        out.append(f'<programme start="{ts(s)}" stop="{ts(e)}" channel="{cid}"><title>{title}</title>'
                   + (f"<sub-title>{ep}</sub-title>" if ep else "") + (f"<desc>{desc}</desc>" if desc else "")
                   + (f'<series-id system="cseries">{sid}</series-id>' if sid else "") + "</programme>")
    out.append("</tv>")
    return "".join(out).encode()


def test_guide_parse_now_playing_and_programs_now():
    doc = _xmltv([("c1", "4.1", ["SUMMIT4", "4.1 SUMMIT4", "4.1"]), ("c2", "104.1", ["NXTGN", "104.1"]),
                  ("c3", "", ["6.1", "STAR6HD"]), ("c4", "", ["no number"])],
                 [("c1", 200, 300, "Later Show", "", "L", ""),
                  ("c1", 100, 200, "Night Report Live", "Ep", "D", "x" * 500),
                  ("c1", 100, 200, "Night Report Live", "Ep", "D", ""),          # the same airing listed twice
                  ("c2", 100, 200, "Night Report Live", "", "", ""),
                  ("c3", 100, 200, "Night Report Extra", "", "", ""),
                  ("c9", 100, 200, "unknown channel", "", "", "")])
    g = h.parse_xmltv(doc, now=0)
    assert list(g) == ["4.1", "104.1", "6.1"] and g["4.1"]["name"] == "SUMMIT4" and g["6.1"]["name"] == "STAR6HD"
    assert [p["t"] for p in g["4.1"]["programs"]] == ["Night Report Live", "Later Show"]       # sorted, the double dropped
    p = g["4.1"]["programs"][0]
    assert (p["ep"], p["sid"], len(p["syn"])) == ("Ep", "D", 200)
    assert h.now_playing(g, "4.1", 150)["t"] == "Night Report Live" and h.now_playing(g, "4.1", 250)["t"] == "Later Show"
    assert h.now_playing(g, "9.9", 150) is None and h.now_playing(g, None, 150) is None
    # exact title wins over "contains"; both simulcast copies come back, in channel order
    assert [n for n, _ in h.programs_now(g, "night report live", 150)] == ["4.1", "104.1"]
    assert {p["t"] for _, p in h.programs_now(g, "night report", 150)} == {"Night Report Live", "Night Report Extra"}
    assert h.programs_now(g, "nothing like it", 150) == []


def test_guide_comes_from_the_xmltv_feed():
    """One request to SiliconDust's XMLTV Guide Data API (their documented way for apps that keep guide data), gzip
    handled, only programs that are not over and start inside the window, time-zone offsets honoured."""
    import gzip as _gz
    H = 3600
    doc = _xmltv([("c1", "4.1", ["A"])], [("c1", t, t + H, f"A{t // H}", "", "S", "") for t in range(-2 * H, 5 * 24 * H, H)])
    asked = []

    def get(url):
        asked.append(url)
        return _gz.compress(doc)

    g = h.fetch_guide("AU TH", get, now=0, days=3)
    assert len(asked) == 1 and asked[0].endswith("/api/xmltv?DeviceAuth=AU%20TH")
    assert [p["s"] for p in g["4.1"]["programs"]] == list(range(0, 72 * H, H))    # every hour of 3 days, once
    assert g["4.1"]["programs"][0]["sid"] == "S"
    assert h._xmltv_time("19700101010000 +0100") == 0 and h._xmltv_time("19700101000000 -0500") == 5 * H
    assert h._xmltv_time("not a time") == 0

def test_search_guide():
    g = {"6.1": {"name": "STAR6HD", "programs": [{"s": 0, "e": 50, "t": "Old News", "ep": "", "sid": "", "syn": ""},
                                                 {"s": 100, "e": 200, "t": "Judge Hour", "ep": "", "sid": "J", "syn": ""}]},
         "4.1": {"name": "SUMMIT4", "programs": [{"s": 300, "e": 400, "t": "Night Report Live", "ep": "Judgement Day",
                                               "sid": "D", "syn": ""}]}}
    assert [r["t"] for r in h.search_guide(g, "JUDGE", now=60)] == ["Judge Hour", "Night Report Live"]   # time order
    assert [r["t"] for r in h.search_guide(g, "news", now=60)] == []                                  # already over
    assert [r["t"] for r in h.search_guide(g, "summit4", now=60)] == ["Night Report Live"]                    # by channel
    assert h.search_guide(g, "  ", now=60) == []


def test_record_airing_params():
    sent = []
    h.record_airing("A", "JM", "next", 1794100000, "6.1", lambda url: sent.append(url))
    assert sent[-1].endswith("Cmd=add&SeriesID=JM&DateTimeOnly=1794100000&ChannelOnly=6.1")
    h.record_airing("A", "JM", "series", None, None, lambda url: sent.append(url))
    assert sent[-1].endswith("Cmd=add&SeriesID=JM")
    assert not h.record_airing("A", "JM", "next", None, None, lambda url: sent.append(url))["ok"]


def test_hold_channel_streams_until_stopped(socket_enabled):
    """The signal test's tuner hold: reads the stream until told to stop; a busy tuner (503) ends it at once."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/auto/v9.9":
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"G" * 188 * 64)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        stop, seen, out = threading.Event(), [], []
        th = threading.Thread(target=lambda: out.append(h.hold_channel("127.0.0.1", "9.1", stop, seen.append, port=port)))
        th.start()
        for _ in range(200):
            if seen:
                break
            stop.wait(0.01)
        assert seen == ["127.0.0.1"]
        stop.set()
        th.join(5)
        assert out == ["stopped"]
        assert h.hold_channel("127.0.0.1", "9.9", threading.Event(), port=port) == "HTTP 503"
    finally:
        srv.shutdown()


def test_conflicts():
    s = lambda a, b, t="x", ch="1.1": {"start": a, "end": b, "title": t, "channel": ch}  # noqa: E731
    sched = [s(0, 100, "A"), s(10, 50, "B"), s(20, 60, "C"), s(100, 200, "D")]
    peak, out = h.conflicts(sched, 2)
    assert peak == 3 and len(out) == 1 and out[0]["needed"] == 3 and (out[0]["start"], out[0]["end"]) == (20, 50)
    assert sorted(out[0]["shows"]) == ["A (ch 1.1)", "B (ch 1.1)", "C (ch 1.1)"]
    # back to back (A ends at 100, D starts at 100) is not an overlap
    assert h.conflicts([s(0, 100), s(100, 200)], 1) == (1, [])
    assert h.conflicts([], 4) == (0, [])


def test_error_rate(monkeypatch):
    status = [{"Resource": "tuner0", "VctNumber": "36.2", "VctName": "BEACON", "Frequency": 563000000,
               "SignalStrengthPercent": 100, "SignalQualityPercent": 63, "SymbolQualityPercent": 100,
               "TargetIP": "192.168.1.30"}, {"Resource": "tuner1"}]
    counters = iter([10, 40, 5])
    times = iter([100.0, 130.0, 160.0])
    monkeypatch.setattr(h, "http_json", lambda url, timeout=6.0: status if url.endswith("status.json") else {})
    monkeypatch.setattr(h, "ctl_get", lambda ip, names, timeout=4.0: {
        "/tuner0/debug": f"ts: bps=1000000 te={next(counters)} crc=0", "/tuner1/debug": "tun: ch=none lock=none"})
    # fake the clock for hdhr_api only (patching time.time itself breaks the HA test harness)
    monkeypatch.setattr(h, "time", types.SimpleNamespace(time=lambda: next(times)))
    t = h.Tuner("http://t", {"DeviceID": "AB", "TunerCount": 2})
    s1, s2 = t.poll([], str), t.poll([], str)
    status[0]["VctNumber"] = "30.1"
    s3 = t.poll([], str)
    assert s1["t0"]["epm"] is None and s2["t0"]["epm"] == 60.0 and s3["t0"]["epm"] is None
    # running tally: first sample = baseline (0), then +30, then a retune restarts the counter (all 5 are new)
    assert (s1["t0"]["new_err"], s2["t0"]["new_err"], s3["t0"]["new_err"]) == (0, 30, 5)
    assert s2["t1"]["new_err"] == 0
    assert s2["t1"]["channel"] == "idle" and s2["in_use"] == 1 and s2["t0"]["channel"] == "36.2 BEACON"
    assert json.loads(json.dumps(s2)) == s2
