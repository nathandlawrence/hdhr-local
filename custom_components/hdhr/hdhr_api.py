"""HDHomeRun local device access: discovery, tuner and DVR (RECORD engine) polling, plus the recording schedule from
SiliconDust's cloud API.

No Home Assistant imports, so it is unit-tested on its own (tests/test_hdhr_api.py). Everything here is READ-ONLY
toward the devices: HTTP discover/status/lineup JSON and the tuner control port's "get". DeviceAuth is read fresh for
each schedule fetch, used only in that HTTPS request, and never stored, logged or returned.
"""
from __future__ import annotations

import gzip
import http.client
import io
import json
import logging
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# --- HDHomeRun UDP discovery + TCP control protocol -------------------------------------------------------------

DISCOVER_REQ, DISCOVER_RPY, GETSET_REQ, GETSET_RPY = 0x0002, 0x0003, 0x0004, 0x0005
TAG_DEVICE_TYPE, TAG_DEVICE_ID, TAG_NAME, TAG_VALUE, TAG_ERROR = 0x01, 0x02, 0x03, 0x04, 0x05
TAG_BASE_URL, TAG_MULTI_TYPE = 0x2A, 0x2D
TYPE_TUNER, TYPE_STORAGE, TYPE_WILDCARD = 0x00000001, 0x00000005, 0xFFFFFFFF


def _tlv(tag: int, value: bytes) -> bytes:
    n = len(value)
    ln = bytes([n]) if n < 128 else bytes([(n & 0x7F) | 0x80, n >> 7])
    return bytes([tag]) + ln + value


def _packet(ptype: int, payload: bytes) -> bytes:
    body = struct.pack(">HH", ptype, len(payload)) + payload
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def _parse_packet(data: bytes, want_type: int):
    """-> {tag: [value bytes, ...]} for a valid packet of want_type, else None."""
    if len(data) < 8:
        return None
    ptype, plen = struct.unpack(">HH", data[:4])
    if ptype != want_type or len(data) < 8 + plen:
        return None
    body = data[:4 + plen]
    if zlib.crc32(body) & 0xFFFFFFFF != struct.unpack("<I", data[4 + plen:8 + plen])[0]:
        return None
    tags: dict[int, list[bytes]] = {}
    i = 4
    while i + 2 <= len(body):
        tag, ln = body[i], body[i + 1]
        i += 2
        if ln & 0x80:
            if i >= len(body):
                break
            ln = (ln & 0x7F) | (body[i] << 7)
            i += 1
        tags.setdefault(tag, []).append(body[i:i + ln])
        i += ln
    return tags


def discover_request() -> bytes:
    wild = struct.pack(">I", TYPE_WILDCARD)
    return _packet(DISCOVER_REQ, _tlv(TAG_DEVICE_TYPE, wild) + _tlv(TAG_DEVICE_ID, wild))


def parse_discover_reply(data: bytes, ip: str):
    """-> (base_url, {device types}) or None."""
    tags = _parse_packet(data, DISCOVER_RPY)
    if tags is None:
        return None
    types = set()
    for v in tags.get(TAG_DEVICE_TYPE, []) + tags.get(TAG_MULTI_TYPE, []):
        for k in range(0, len(v) - 3, 4):
            types.add(struct.unpack(">I", v[k:k + 4])[0])
    base = tags.get(TAG_BASE_URL, [b""])[0].split(b"\0")[0].decode(errors="replace").rstrip("/")
    return (base or f"http://{ip}"), types


def scan_lan(timeout: float = 2.0) -> dict[str, set[int]]:
    """Broadcast one discover request; -> {base_url: {types}} for every device that answers."""
    found: dict[str, set[int]] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
        s.sendto(discover_request(), ("255.255.255.255", 65001))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (ip, _) = s.recvfrom(2048)
            except socket.timeout:
                continue
            r = parse_discover_reply(data, ip)
            if r:
                found.setdefault(r[0], set()).update(r[1])
    except OSError as e:
        _LOGGER.warning("HDHomeRun discovery broadcast failed: %s", e)
    finally:
        s.close()
    return found


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def ctl_get(ip: str, names: list[str], timeout: float = 4.0) -> dict[str, str | None]:
    """Read-only 'get' of several control-port variables over one TCP connection."""
    out: dict[str, str | None] = {}
    try:
        with socket.create_connection((ip, 65001), timeout=timeout) as s:
            for name in names:
                s.sendall(_packet(GETSET_REQ, _tlv(TAG_NAME, name.encode() + b"\0")))
                head = _recv_exact(s, 4)
                plen = struct.unpack(">HH", head)[1]
                tags = _parse_packet(head + _recv_exact(s, plen + 4), GETSET_RPY) or {}
                val = tags.get(TAG_VALUE)
                out[name] = val[0].split(b"\0")[0].decode(errors="replace") if val else None
    except (OSError, struct.error) as e:
        _LOGGER.debug("control port %s: %s", ip, e)
    return out


_KV = re.compile(r"(\w+)=(\S+)")


def parse_debug(text: str | None) -> dict[str, str]:
    """'/tunerN/debug' text -> {'tun.ss': '100', 'ts.te': '0', 'net.err': '0', ...}."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        sect, sep, rest = line.partition(":")
        if sep:
            for k, v in _KV.findall(rest):
                out[f"{sect.strip()}.{k}"] = v
    return out


def http_json(url: str, timeout: float = 6.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def host_of(base: str) -> str:
    return re.sub(r"^https?://", "", base).split(":")[0].split("/")[0]


# --- devices -------------------------------------------------------------------------------------------------

class Tuner:
    kind = "tuner"

    def __init__(self, base: str, disc: dict):
        self.base = base
        self.ip = host_of(base)
        self.id = str(disc["DeviceID"])
        self.model = disc.get("ModelNumber", "")
        self.label = f"{disc.get('FriendlyName', 'HDHomeRun')} {self.id}"
        self.firmware = disc.get("FirmwareVersion", "")
        self.count = _int(disc.get("TunerCount"), 0)
        self.prev: dict[int, tuple] = {}      # tuner n -> (time, te+crc, tune key)
        self.lineup: dict[str, str] = {}      # guide number -> guide name, refreshed on each scan

    def refresh_lineup(self) -> None:
        rows = http_json(f"{self.base}/lineup.json")
        self.lineup = {r["GuideNumber"]: r.get("GuideName", "") for r in rows if r.get("GuideNumber")}

    def poll(self, sessions: list[tuple], client_name) -> dict:
        disc = http_json(f"{self.base}/discover.json")
        self.firmware = disc.get("FirmwareVersion", self.firmware)
        rows = {r.get("Resource"): r for r in http_json(f"{self.base}/status.json")}
        dbg = ctl_get(self.ip, [f"/tuner{n}/debug" for n in range(self.count)])
        now, in_use = time.time(), 0
        state: dict = {"firmware": self.firmware}
        for n in range(self.count):
            r, d = rows.get(f"tuner{n}", {}), parse_debug(dbg.get(f"/tuner{n}/debug"))
            vct = r.get("VctNumber")
            active = bool(vct or r.get("TargetIP"))
            in_use += active
            errors = _int(d.get("ts.te")) + _int(d.get("ts.crc"))
            key = (vct, d.get("tun.ch"))
            prev = self.prev.get(n)
            # new_err = errors since the last poll, for the running tally. The device's te/crc counters restart at
            # every tune, so after a retune the whole new count is new; the very first sample is only a baseline
            # (those errors may already be in a tally restored after an HA restart).
            if not active:
                epm, new_err = 0.0, 0
            elif prev is None:
                epm, new_err = None, 0
            elif prev[2] == key and errors >= prev[1] and now > prev[0]:
                epm, new_err = round((errors - prev[1]) * 60.0 / (now - prev[0]), 1), errors - prev[1]
            else:
                epm, new_err = None, errors
            self.prev[n] = (now, errors, key)
            state[f"t{n}"] = {
                "channel": f"{vct} {r.get('VctName', '')}".strip() if vct else ("busy" if active else "idle"),
                "vct": vct,
                "ss": r.get("SignalStrengthPercent") if active else None,
                "snq": r.get("SignalQualityPercent") if active else None,
                "seq": r.get("SymbolQualityPercent") if active else None,
                "mbps": round(_int(d.get("ts.bps")) / 1e6, 2) if active else 0.0,
                "epm": epm,
                "new_err": new_err,
                "viewer": viewer_for(vct, r.get("TargetIP"), sessions, client_name) if active else "none",
                "target": r.get("TargetIP"),
                "freq_mhz": round(r["Frequency"] / 1e6, 1) if active and r.get("Frequency") else None,
            }
        state["in_use"] = in_use
        return state


def viewer_for(vct, target_ip, sessions, client_name) -> str:
    """Who a tuner serves: the DVR's live/recording sessions on that channel, or a client tuned directly."""
    dvr_ips = {s[0] for s in sessions}
    if target_ip and target_ip not in dvr_ips:
        return client_name(target_ip)
    who = []
    for dvr_ip, ch, client_ip, resource in sessions:
        if ch == vct and (not target_ip or dvr_ip == target_ip):
            who.append(client_name(client_ip) if resource == "live" else f"DVR {resource}")
    return ", ".join(sorted(set(who))) or ("DVR" if target_ip else "none")


class Dvr:
    kind = "dvr"

    def __init__(self, base: str, disc: dict):
        self.base = base
        self.ip = host_of(base)
        sid = str(disc["StorageID"])
        self.id = re.sub(r"[^A-Za-z0-9]", "", sid.split("-")[0])[:16] or "dvr"
        self.model = "RECORD engine"
        self.label = f"{disc.get('FriendlyName', 'HDHomeRun RECORD')} {self.ip}"
        self.firmware = disc.get("Version", "")
        self.recordings: dict | None = None
        self.sessions: list[tuple] = []

    def poll_sessions(self) -> list[tuple]:
        rows = http_json(f"{self.base}/status.json")
        out = []
        for r in rows if isinstance(rows, list) else []:
            m = re.search(r"(\d+\.\d+)", r.get("Name", ""))
            out.append((self.ip, m.group(1) if m else None, r.get("TargetIP"), r.get("Resource", "")))
        self.sessions = out
        return out

    def refresh_recordings(self) -> None:
        total, last = 0, None
        for series in http_json(f"{self.base}/recorded_files.json"):
            url = series.get("EpisodesURL")
            if not url:
                continue
            for ep in http_json(url):
                total += 1
                if last is None or ep.get("RecordStartTime", 0) > last.get("RecordStartTime", 0):
                    last = ep
        self.recordings = {"count": total, "last": None if last is None else {
            "title": (last.get("Title", "") + (" - " + last["EpisodeTitle"] if last.get("EpisodeTitle") else ""))[:250],
            "channel": last.get("ChannelNumber"),
            "started": datetime.fromtimestamp(last.get("RecordStartTime", 0), timezone.utc).isoformat()}}

    def poll(self, sessions: list[tuple], client_name) -> dict:
        disc = http_json(f"{self.base}/discover.json")
        self.firmware = disc.get("Version", self.firmware)
        total, free = _int(disc.get("TotalSpace")), _int(disc.get("FreeSpace"))
        live = [s for s in self.sessions if s[3] == "live"]
        other = [s for s in self.sessions if s[3] != "live"]
        rec = self.recordings or {}
        return {
            "firmware": self.firmware,
            "free_gb": round(free / 1e9, 1) if total else None,
            "used_pct": round(100.0 * (total - free) / total, 1) if total else None,
            "live": len(live),
            "live_list": [f"{ch} -> {client_name(ip)}" for _, ch, ip, _ in live],
            "recording": bool(other),
            "recording_list": [f"{res} {ch}" for _, ch, _, res in other],
            "recordings": rec.get("count"),
            "last": (rec.get("last") or {}).get("title"),
            "last_attrs": rec.get("last") or {},
        }


def classify(base: str):
    """-> Tuner / Dvr for a base URL by its own discover.json, or None."""
    disc = http_json(f"{base}/discover.json")
    if "StorageID" in disc:
        return Dvr(base, disc)
    if "DeviceID" in disc and _int(disc.get("TunerCount")) > 0:
        return Tuner(base, disc)
    return None


class Network:
    """Every HDHomeRun tuner and DVR engine found on the LAN (plus any static base URLs)."""

    def __init__(self, static: list[str] | None = None):
        self.static = [s.rstrip("/") if "://" in s else f"http://{s}" for s in (static or []) if s]
        self.devices: dict[str, Tuner | Dvr] = {}       # base URL -> device

    def scan(self) -> list:
        """Find new devices; refresh DVR recording lists. -> newly found devices."""
        new = []
        for base in sorted((set(self.static) | set(scan_lan())) - set(self.devices)):
            try:
                dev = classify(base)
            except (OSError, ValueError) as e:
                _LOGGER.warning("could not read %s: %s", base, e)
                continue
            if dev and dev.id not in {d.id for d in self.devices.values()}:
                self.devices[base] = dev
                new.append(dev)
                _LOGGER.info("found HDHomeRun %s %s at %s", dev.kind, dev.id, base)
        for dev in self.devices.values():
            try:
                if dev.kind == "dvr":
                    dev.refresh_recordings()
                else:
                    dev.refresh_lineup()
            except (OSError, ValueError) as e:
                _LOGGER.warning("refresh %s: %s", dev.base, e)
        return new

    def lineup(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for dev in self.devices.values():
            out.update(getattr(dev, "lineup", {}) or {})
        return out

    def tuner_count(self) -> int:
        return sum(getattr(d, "count", 0) for d in self.devices.values() if d.kind == "tuner")

    def schedule(self, days: float = 14.0) -> list[dict]:
        """Upcoming recordings (cloud). Blocking. -> [{start, end, title, episode, channel, ...}] sorted by start;
        start/end include each rule's padding. Raises on failure (the caller keeps the last good schedule)."""
        if not any(d.kind == "tuner" for d in self.devices.values()):
            return []
        return fetch_schedule(self.device_auth(), time.time(), days)

    def guide(self) -> dict[str, dict]:
        """The cloud guide (read only). Blocking."""
        return fetch_guide(self.device_auth())

    def record_exact(self, series_id: str, mode: str, start: int | None, channel: str | None) -> dict:
        """Record from a guide listing (exact airing or the whole series) and tell every DVR engine. WRITES."""
        out = record_airing(self.device_auth(), series_id, mode, start, channel)
        if out.get("ok"):
            for d in self.devices.values():
                if d.kind == "dvr":
                    try:
                        poke_engine(d.base)
                    except (OSError, ValueError) as e:
                        _LOGGER.warning("record engine %s did not take the sync: %s", d.base, e)
        return out

    def free_tuners(self) -> tuple[int, str | None]:
        """-> (free tuners across all tuner devices, IP of a device that has one)."""
        free, ip = 0, None
        for d in self.devices.values():
            if d.kind != "tuner":
                continue
            rows = http_json(f"{d.base}/status.json")
            n = sum(1 for r in rows if r.get("Resource", "").startswith("tuner")
                    and not (r.get("VctNumber") or r.get("TargetIP")))
            free += n
            if n and ip is None:
                ip = d.ip
        return free, ip

    def probe(self, ch: str, min_free: int = 2) -> dict | None:
        """One feed test on a FREE tuner, only if at least `min_free` tuners are free (so live TV and recordings keep
        theirs). -> result dict, or None if skipped."""
        free, ip = self.free_tuners()
        if free < min_free or not ip:
            return None
        return probe_channel(ip, ch)

    def rules(self) -> list[dict]:
        return http_json(f"{API}/recording_rules?DeviceAuth={_q(self.device_auth())}", timeout=20) or []

    def pin_rule(self, series_id: str, channel: str) -> None:
        """Limit a series rule to one channel (Cmd=change ... ChannelOnly) and tell every DVR engine. WRITES."""
        rule_cmd(self.device_auth(), {"Cmd": "change", "SeriesID": series_id, "ChannelOnly": channel})
        for d in self.devices.values():
            if d.kind == "dvr":
                try:
                    poke_engine(d.base)
                except (OSError, ValueError) as e:
                    _LOGGER.warning("record engine %s did not take the sync: %s", d.base, e)

    def quick_sessions(self) -> list[list]:
        """Who is watching what, fast (one HTTP call per DVR engine and per tuner) - for confirming a channel change.
        Includes clients streaming straight from a tuner: when one TV already watches a channel through the DVR, a
        second TV on the same channel may take a tuner directly, and the DVR's list never shows it."""
        out: list[list] = []
        dvr_ips = {d.ip for d in self.devices.values() if d.kind == "dvr"}
        for d in self.devices.values():
            try:
                if d.kind == "dvr":
                    out += [list(s) for s in d.poll_sessions()]
                elif d.kind == "tuner":
                    rows = http_json(f"{d.base}/status.json")
                    out += [["", r.get("VctNumber"), r["TargetIP"], "direct"] for r in rows if isinstance(rows, list)
                            and r.get("TargetIP") and r["TargetIP"] not in dvr_ips]
            except (OSError, ValueError) as e:
                _LOGGER.debug("quick sessions %s: %s", d.base, e)
        return out

    def device_auth(self) -> str:
        """All tuners' DeviceAuth strings concatenated (SiliconDust's rule); read fresh, never stored."""
        auth = "".join(http_json(f"{d.base}/discover.json").get("DeviceAuth", "")
                       for d in sorted(self.devices.values(), key=lambda d: d.id) if d.kind == "tuner")
        if not auth:
            raise ValueError("no tuner gave a DeviceAuth")
        return auth

    def record(self, text: str, mode: str = "series", channel: str | None = None) -> dict:
        """Add a recording rule (cloud) and tell every DVR engine to recompute. Blocking. WRITES the schedule."""
        out = record(self.device_auth(), text, mode, channel, time.time())
        if out.get("ok"):
            for d in self.devices.values():
                if d.kind == "dvr":
                    try:
                        poke_engine(d.base)
                    except (OSError, ValueError) as e:
                        _LOGGER.warning("record engine %s did not take the sync: %s", d.base, e)
        return out

    def poll(self, client_name) -> dict:
        """-> {"devices": {id: {...}}, "sessions": [...], "clients": [ip, ...]} - one snapshot of everything."""
        sessions: list[tuple] = []
        for dev in self.devices.values():
            if dev.kind == "dvr":
                try:
                    sessions += dev.poll_sessions()
                except (OSError, ValueError) as e:
                    _LOGGER.debug("sessions %s: %s", dev.base, e)
        out = {}
        for dev in self.devices.values():
            info = {"kind": dev.kind, "model": dev.model, "label": dev.label, "base": dev.base,
                    "firmware": dev.firmware, "count": getattr(dev, "count", 0)}
            try:
                info["state"], info["ok"] = dev.poll(sessions, client_name), True
            except (OSError, ValueError) as e:
                _LOGGER.debug("poll %s: %s", dev.base, e)
                info["state"], info["ok"] = {}, False
            out[dev.id] = info
        clients = sorted({s[2] for s in sessions if s[2]})
        # direct tuning (a client streaming from a tuner, not via a DVR) counts as a session too
        dvr_ips = {d.ip for d in self.devices.values() if d.kind == "dvr"}
        direct = []
        for dev_id, info in out.items():
            if info["kind"] != "tuner":
                continue
            for n in range(info.get("count", 0)):
                t = (info["state"] or {}).get(f"t{n}") or {}
                if t.get("target") and t["target"] not in dvr_ips:
                    direct.append(["", t.get("vct"), t["target"], "direct"])
        return {"devices": out, "sessions": [list(s) for s in sessions] + direct,
                "clients": sorted(set(clients) | {d[2] for d in direct}), "lineup": self.lineup(),
                "tuner_count": self.tuner_count()}


API = "https://api.hdhomerun.com/api"


def fetch_schedule(auth: str, now: float, days: float, get=None) -> list[dict]:
    """Rule-flagged episodes ending after now and starting within `days`, padded by their rule. `get` is injectable
    for tests; the DeviceAuth only ever goes into these HTTPS query strings."""
    get = get or (lambda url: http_json(url, timeout=20))
    rules = get(f"{API}/recording_rules?DeviceAuth={auth}") or []
    pads: dict[str, tuple[int, int]] = {}
    chans: dict[str, set | None] = {}         # SeriesID -> channels its rules allow (None = any channel)
    for r in rules:
        sid = r.get("SeriesID")
        if sid:
            p = pads.get(sid, (0, 0))
            pads[sid] = (max(p[0], _int(r.get("StartPadding"))), max(p[1], _int(r.get("EndPadding"))))
            only = {c for c in str(r.get("ChannelOnly") or "").split("|") if c}
            if not only or chans.get(sid, set()) is None:
                chans[sid] = None
            else:
                chans[sid] = chans.get(sid, set()) | only
    until = now + days * 86400
    out = []
    for sid in sorted(pads):
        # A simulcast (e.g. 4.1 and its NextGen copy on 104.1) is ONE recording: same program, same time.
        # Group every airing of the series by (program, start) first; a group is scheduled if any airing in it is
        # rule-flagged. `airs_on` keeps every channel it airs on (so a pinned rule can be re-checked later);
        # `channels` is what the rule's ChannelOnly allows.
        groups: dict[tuple, list] = {}
        for ep in get(f"{API}/episodes?DeviceAuth={auth}&SeriesID={sid}") or []:
            key = (ep.get("ProgramID") or f"{ep.get('Title')}|{ep.get('EpisodeTitle')}", _int(ep.get("StartTime")))
            groups.setdefault(key, []).append(ep)
        for eps in groups.values():
            if not any(ep.get("RecordingRule") == 1 for ep in eps):
                continue
            ep0 = eps[0]
            start, end = _int(ep0.get("StartTime")) - pads[sid][0], _int(ep0.get("EndTime")) + pads[sid][1]
            if not (end > now and start < until):
                continue
            airs_on = sorted({ep.get("ChannelNumber", "") for ep in eps if ep.get("ChannelNumber")}, key=channel_key)
            allowed = [c for c in airs_on if chans.get(sid) is None or c in chans[sid]]
            if not allowed:
                continue                      # the rule's ChannelOnly leaves every airing of it out
            out.append({"start": start, "end": end, "title": ep0.get("Title", ""),
                        "episode": ep0.get("EpisodeTitle", ""), "channel": " / ".join(allowed), "channels": allowed,
                        "airs_on": airs_on, "channel_name": ep0.get("ChannelName", ""), "series_id": sid,
                        "rule_channels": None if chans.get(sid) is None else sorted(chans[sid], key=channel_key),
                        "program_id": ep0.get("ProgramID", ""), "synopsis": (ep0.get("Synopsis") or "")[:500]})
    out.sort(key=lambda x: (x["start"], x["channel"]))
    return out


# --- best feed for simulcasts: pick the channel to record from by measured results, not guesses --------------
# A simulcast (e.g. an ATSC 3.0 copy) can fail tunes with "807 No Video Data" at perfect signal, so signal
# numbers alone can't decide - the deciding number is how often a tune actually delivers video. The integration
# measures that itself with short test streams on FREE tuners and pins a simulcast series to the better channel.
FEED_MIN_SAMPLES = 12          # results per channel before any decision
FEED_MARGIN = 0.05             # the better channel must win by at least 5 percentage points


def hold_channel(tuner_ip: str, ch: str, stop, on_open=None, port: int = 5004) -> str:
    """Keep one tuner on `ch` until `stop` (a threading.Event) is set: stream from the tuner and throw the data away
    (the signal test). on_open(this machine's address as the tuner sees it) once streaming. -> why it ended:
    "stopped", "HTTP 503" (no tuner free), "stream ended", or the network error."""
    conn = http.client.HTTPConnection(tuner_ip, port, timeout=10)
    try:
        conn.request("GET", f"/auto/v{ch}")
        local = conn.sock.getsockname()[0] if conn.sock else None   # before getresponse hands the socket over
        r = conn.getresponse()
        try:
            if r.status != 200:
                return f"HTTP {r.status}"
            if on_open and local:
                on_open(local)
            while not stop.is_set():
                if not r.read(65536):
                    return "stream ended"
            return "stopped"
        finally:
            r.close()
    except (OSError, http.client.HTTPException) as e:
        return f"{type(e).__name__}: {e}"[:120]
    finally:
        conn.close()


def probe_channel(tuner_ip: str, ch: str, secs: int = 8, max_bytes: int = 1_500_000) -> dict:
    """One feed test: ask the tuner for `secs` of the channel (the same request the DVR makes). Delivered video =
    ok. Reads at most max_bytes, then hangs up so the tuner is free again."""
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"http://{tuner_ip}:5004/auto/v{_q(ch)}?duration={secs}", timeout=secs + 12) as r:
            n = 0
            while n < max_bytes:
                b = r.read(min(65536, max_bytes - n))
                if not b:
                    break
                n += len(b)
        ok = n >= 100_000
        return {"ok": ok, "bytes": n, "secs": round(time.time() - t0, 1), "error": None if ok else "too little data"}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(120).decode(errors="replace").strip()
        except OSError:
            body = ""
        return {"ok": False, "bytes": 0, "secs": round(time.time() - t0, 1), "error": f"{e.code} {body}"[:80]}
    except OSError as e:
        return {"ok": False, "bytes": 0, "secs": round(time.time() - t0, 1), "error": type(e).__name__}


def feed_stats(samples: list) -> dict:
    """samples: [[unix time, ok], ...] -> {"tries", "ok", "rate"}."""
    tries = len(samples)
    ok = sum(1 for s in samples if s[1])
    return {"tries": tries, "ok": ok, "rate": (ok / tries) if tries else None}


def choose_feed(channels: list[str], samples: dict[str, list], current: str | None = None) -> tuple[str | None, str]:
    """-> (channel to pin or None, why). Decides only with FEED_MIN_SAMPLES results per channel and a clear winner;
    otherwise keeps `current` (or nothing)."""
    stats = {c: feed_stats(samples.get(c, [])) for c in channels}
    short = [c for c, s in stats.items() if s["tries"] < FEED_MIN_SAMPLES]
    if short:
        return current, "collecting data: " + ", ".join(f"{c} {stats[c]['tries']}/{FEED_MIN_SAMPLES}" for c in short)
    ranked = sorted(channels, key=lambda c: (-stats[c]["rate"], channel_key(c)))
    best, second = ranked[0], ranked[1] if len(ranked) > 1 else None
    desc = ", ".join(f"{c} {stats[c]['ok']}/{stats[c]['tries']}" for c in ranked)
    if second is not None and stats[best]["rate"] - stats[second]["rate"] < FEED_MARGIN:
        return current, f"no clear winner ({desc})"
    if current and current in stats and stats[best]["rate"] - stats[current]["rate"] < FEED_MARGIN:
        return current, f"keeping {current} ({desc})"
    return best, f"best: {best} ({desc})"


def conflicts(schedule: list[dict], tuners: int) -> tuple[int, list[dict]]:
    """-> (peak recordings at once, [windows where recordings need MORE tuners than exist]). Each recording takes its
    own tuner (a recording and live TV on the same channel use two)."""
    edges = sorted([(s["start"], 1, i) for i, s in enumerate(schedule)] + [(s["end"], -1, i) for i, s in enumerate(schedule)],
                   key=lambda e: (e[0], e[1]))          # an end at time t frees its tuner before a start at t
    active: set[int] = set()
    peak, out, cur = 0, [], None
    for t, kind, i in edges:
        if kind == 1:
            active.add(i)
        else:
            active.discard(i)
        peak = max(peak, len(active))
        over = len(active) > tuners if tuners else False
        if over and cur is None:
            cur = {"start": t, "needed": len(active), "shows": set(active)}
        elif over:
            cur["needed"] = max(cur["needed"], len(active))
            cur["shows"] |= active
        elif cur is not None:
            cur["end"] = t
            out.append(cur)
            cur = None
    for c in out:
        c["shows"] = [f"{schedule[i]['title']} (ch {schedule[i]['channel']})" for i in sorted(c["shows"])]
    return peak, out


# --- recording rules (cloud; WRITES to the account's DVR schedule) ------------------------------------------------
# SiliconDust docs (info.hdhomerun.com/info/dvr_api:dvr_recording_rules + :poke_record_engine): recording_rules with
# Cmd=add / delete; DeviceAuth = all tuners' DeviceAuth strings concatenated; after any change POST
# <record engine>/recording_events.post?sync so it recomputes its tasks.

def _q(v) -> str:
    return urllib.parse.quote(str(v), safe="")


def _post(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace").strip()
    return json.loads(body) if body and body[0] in "[{" else body


def search_series(auth: str, text: str, get=None) -> list[dict]:
    get = get or (lambda url: http_json(url, timeout=20))
    return get(f"{API}/search?DeviceAuth={_q(auth)}&Search={_q(text)}") or []


def series_episodes(auth: str, series_id: str, get=None) -> list[dict]:
    get = get or (lambda url: http_json(url, timeout=20))
    return get(f"{API}/episodes?DeviceAuth={_q(auth)}&SeriesID={_q(series_id)}") or []


def rule_cmd(auth: str, params: dict, post=None):
    post = post or _post
    query = "&".join(f"{k}={_q(v)}" for k, v in params.items())
    return post(f"{API}/recording_rules?DeviceAuth={_q(auth)}&{query}")


def best_match(results: list[dict], text: str) -> dict | None:
    """Exact title (ignoring case) first, then a title that starts with the words, then the first result."""
    t = text.casefold().strip()
    for test in (lambda r: r.get("Title", "").casefold() == t,
                 lambda r: r.get("Title", "").casefold().startswith(t)):
        hit = next((r for r in results if test(r)), None)
        if hit:
            return hit
    return results[0] if results else None


def record(auth: str, text: str, mode: str, channel: str | None, now: float, get=None, post=None) -> dict:
    """mode "series": every new airing (a Series rule, optionally ChannelOnly); mode "next": only the next airing
    (a DateTimeOnly + ChannelOnly rule, which expires by itself). -> summary dict (no secrets)."""
    results = search_series(auth, text, get)
    show = best_match(results, text)
    if not show:
        return {"ok": False, "error": f"No show found matching '{text}'."}
    sid = show["SeriesID"]
    eps = [e for e in series_episodes(auth, sid, get) if _int(e.get("EndTime")) > now
           and (not channel or e.get("ChannelNumber") == channel)]
    eps.sort(key=lambda e: _int(e.get("StartTime")))
    nxt = eps[0] if eps else None
    if mode == "next":
        if not nxt:
            return {"ok": False, "title": show.get("Title"), "error": f"No upcoming airing of {show.get('Title')} found."}
        params = {"Cmd": "add", "SeriesID": sid, "DateTimeOnly": _int(nxt["StartTime"]),
                  "ChannelOnly": nxt.get("ChannelNumber", "")}
    else:
        params = {"Cmd": "add", "SeriesID": sid}
        if channel:
            params["ChannelOnly"] = channel
    rule_cmd(auth, params, post)
    return {"ok": True, "title": show.get("Title"), "series_id": sid, "mode": mode,
            "next_airing": None if not nxt else {"start": _int(nxt.get("StartTime")), "channel": nxt.get("ChannelNumber"),
                                                 "episode": nxt.get("EpisodeTitle", "")}}


def record_airing(auth: str, series_id: str, mode: str, start: int | None, channel: str | None,
                  post=None) -> dict:
    """Record straight from a guide listing (no title search): mode "next" = exactly this airing (DateTimeOnly +
    ChannelOnly), mode "series" = every new airing of the series (optionally ChannelOnly)."""
    if mode == "next":
        if not (start and channel):
            return {"ok": False, "error": "That listing has no time or channel."}
        params = {"Cmd": "add", "SeriesID": series_id, "DateTimeOnly": int(start), "ChannelOnly": channel}
    else:
        params = {"Cmd": "add", "SeriesID": series_id}
        if channel:
            params["ChannelOnly"] = channel
    rule_cmd(auth, params, post)
    return {"ok": True, "series_id": series_id, "mode": mode}


def poke_engine(dvr_base: str) -> None:
    _post(f"{dvr_base}/recording_events.post?sync", timeout=10)


# --- guide (cloud; read only) ----------------------------------------------------------------------------------------
# The full two-week guide comes from SiliconDust's XMLTV Guide Data API: their Guide API documentation asks
# third-party apps that keep guide data to use XMLTV. One request (gzip), refreshed every few hours.
def http_bytes(url: str, timeout: float = 120.0) -> bytes:
    """GET `url` and return the body, gunzipped if the server compressed it."""
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def _xmltv_time(text: str | None) -> int:
    """XMLTV time '20261101163000 +0000' -> unix seconds (the offset is honoured); 0 if it can't be read."""
    s = (text or "").strip()
    try:
        t = int(datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0
    off = s[14:].strip()
    if len(off) == 5 and off[0] in "+-" and off[1:].isdigit():
        t -= (1 if off[0] == "+" else -1) * (int(off[1:3]) * 3600 + int(off[3:5]) * 60)
    return t


def parse_xmltv(data: bytes, now: float, days: float = 15.0) -> dict[str, dict]:
    """XMLTV -> {guide number: {"name", "programs": [...]}}, programs = {"s", "e", "t" title, "ep" episode,
    "sid" series id, "syn" synopsis (short)} sorted by start, keeping only programs that are not over and start
    within `days`. A channel's guide number is its <lcn> (or a display-name that is just a number); its name is the
    first other display-name. Streams through the document (two weeks is ~35 MB) and keeps each start time once."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    now = int(now)
    until = now + int(days * 86400)
    ids: dict[str, str] = {}                 # XMLTV channel id -> guide number
    out: dict[str, dict] = {}
    seen: dict[str, set] = {}
    for _ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if el.tag == "channel":
            names = [(d.text or "").strip() for d in el.findall("display-name")]
            num = (el.findtext("lcn") or "").strip() or next(
                (n for n in names if re.fullmatch(r"\d+(\.\d+)?", n)), "")
            if num:
                ids[el.get("id", "")] = num
                name = next((n for n in names if n and n != num and not n.startswith(num + " ")), "")
                out.setdefault(num, {"name": name, "programs": []})
            el.clear()
        elif el.tag == "programme":
            num = ids.get(el.get("channel", ""))
            s, e = _xmltv_time(el.get("start")), _xmltv_time(el.get("stop"))
            if num and e > s and e > now and s < until and s not in seen.setdefault(num, set()):
                seen[num].add(s)
                out[num]["programs"].append({
                    "s": s, "e": e, "t": el.findtext("title") or "", "ep": el.findtext("sub-title") or "",
                    "sid": (el.findtext("series-id") or "").strip(), "syn": (el.findtext("desc") or "")[:200]})
            el.clear()
    for g in out.values():
        g["programs"].sort(key=lambda p: p["s"])
    return out


def fetch_guide(auth: str, get=None, now: float | None = None, days: float = 15.0) -> dict[str, dict]:
    """SiliconDust's guide for the next `days` (the DVR service carries ~2 weeks), from the XMLTV Guide Data API
    -> parse_xmltv(). The DeviceAuth only goes into this HTTPS request."""
    get = get or http_bytes
    return parse_xmltv(get(f"{API}/xmltv?DeviceAuth={_q(auth)}"), now if now is not None else time.time(), days)


def search_guide(guide: dict, query: str, now: float, limit: int = 300) -> list[dict]:
    """Upcoming (and current) programs whose title, episode, description or channel matches, in time order."""
    q = (query or "").casefold().strip()
    if not q:
        return []
    out = []
    for num in sorted(guide or {}, key=channel_key):
        g = guide[num]
        ch_hit = num == q or q in (g.get("name") or "").casefold()
        for p in g["programs"]:
            if p["e"] <= now:
                continue
            if ch_hit or q in p["t"].casefold() or q in (p["ep"] or "").casefold() or q in (p["syn"] or "").casefold():
                out.append({"number": num, "name": g.get("name", ""), **p})
    out.sort(key=lambda r: (r["s"], channel_key(r["number"])))
    return out[:limit]


def programs_now(guide: dict, text: str, at: float) -> list[tuple[str, dict]]:
    """Shows airing now whose title matches the spoken words (exact, then starts-with, then contains) ->
    [(guide number, program)], best match level only, in channel order."""
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", (x or "").casefold()).strip()  # noqa: E731
    t = norm(text)
    if not t:
        return []
    hits = []
    for num in guide or {}:
        p = now_playing(guide, num, at)
        if not p:
            continue
        title = norm(p["t"])
        score = 3 if title == t else 2 if title.startswith(t) else 1 if t in title else 0
        if score:
            hits.append((score, num, p))
    if not hits:
        return []
    top = max(h[0] for h in hits)
    return [(num, p) for s, num, p in sorted(hits, key=lambda h: channel_key(h[1])) if s == top]


def now_playing(guide: dict, channel: str | None, at: float) -> dict | None:
    """The program on `channel` at time `at` from a fetch_guide() result, or None."""
    if not channel or channel not in (guide or {}):
        return None
    return next((p for p in guide[channel]["programs"] if p["s"] <= at < p["e"]), None)


# --- spoken channels / players ------------------------------------------------------------------------------------
_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _words_to_number(words: list[str]) -> int | None:
    if not words:
        return None
    total = 0
    for w in words:
        if w.isdigit():
            total += int(w)
        elif w in _UNITS:
            total += _UNITS[w]
        elif w in _TENS:
            total += _TENS[w]
        else:
            return None
    return total


def channel_key(guide_number: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", guide_number))


def resolve_channel(text: str, lineup: dict[str, str]) -> str | None:
    """'6.1', '6 1', 'six point one', 'thirty six two', 'channel 4', 'Summit', 'star' -> a guide number in the lineup."""
    t = re.sub(r"\bchannel\b", " ", (text or "").casefold()).replace("-", " ").strip()
    words = re.sub(r"[^a-z0-9. ]", " ", t).replace(".", " point ").split()
    if words and all(w.isdigit() or w in _UNITS or w in _TENS or w in ("point", "dot") for w in words):
        parts, cur = [], []
        for w in words:
            if w in ("point", "dot"):
                parts.append(cur)
                cur = []
            else:
                cur.append(w)
        parts.append(cur)
        cands: list[str] = []
        if len(parts) > 1:                                   # "six point one", "36.2"
            major, minor = _words_to_number(parts[0]), _words_to_number(parts[1])
            if major is not None and minor is not None:
                cands.append(f"{major}.{minor}")
        else:
            if len(cur) >= 2 and (cur[-1].isdigit() or cur[-1] in _UNITS):
                # "6 1", "thirty six two": the last word may be the subchannel
                major, minor = _words_to_number(cur[:-1]), _words_to_number(cur[-1:])
                if major is not None and minor is not None:
                    cands.append(f"{major}.{minor}")
            whole = _words_to_number(cur)                    # "three", "twenty one", "10"
            if whole is not None:
                cands += [f"{whole}.1", str(whole)]
        for cand in cands:
            if cand in lineup:
                return cand
    # by name: exact, then the spoken words inside the station name (STAR -> STAR6HD), then the other way round
    flat = re.sub(r"[^a-z0-9]", "", t)
    if not flat:
        return None
    names = sorted(lineup.items(), key=lambda kv: channel_key(kv[0]))
    for test in (lambda n: n == flat, lambda n: n.startswith(flat), lambda n: flat in n, lambda n: n and n in flat):
        hit = next((g for g, name in names if test(re.sub(r"[^a-z0-9]", "", name.casefold()))), None)
        if hit:
            return hit
    return None


def resolve_player(text: str, names: list[str]) -> str | None:
    """Spoken TV name -> one of the player names (exact, then all spoken words present, then any shared word)."""
    t = (text or "").casefold()
    words = set(re.findall(r"[a-z0-9]+", t)) - {"the", "tv", "in", "on", "room"}
    if not names:
        return None
    for n in names:
        if n.casefold() == t.strip():
            return n
    scored = sorted(((len(words & set(re.findall(r"[a-z0-9]+", n.casefold()))), n) for n in names), reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return names[0] if len(names) == 1 else None

