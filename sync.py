#!/usr/bin/env python3
"""Filmakademie ev3nts -> schedule.json + .ics (Google Calendar) + live-artifact payload.

Logs into https://ev3nts.filmakademie.de (Keycloak), pulls the personal student
schedule month by month, and writes into this folder:

  schedule.json               full normalized schedule
  Filmakademie-alle.ics       every event (optional ones marked)
  Filmakademie-Pflicht.ics    mandatory events only
  db_payload.json             compact document for the live artifact

Credentials: macOS Keychain item "filmakademie-ev3nts" (account = username),
or the FA_USER / FA_PASS environment variables.

Exit codes: 0 ok, 2 login failed, 3 fetch failed, 4 suspicious empty result.
Stdlib only (runs with the macOS system python3).
"""
import argparse
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = "https://ev3nts.filmakademie.de"
START_URL = BASE + "/user/student/"
API = BASE + "/rs/user/student/"
KEYCHAIN_SERVICE = "filmakademie-ev3nts"
DEFAULT_USER = ""  # set FA_USER (and FA_PASS) in the environment / GitHub secrets
TZ = "Europe/Berlin"
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_GROUPS = 8  # longer "mandatory for" lists mean "everyone" and are dropped
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) FilmakademieKalenderSync/1.0"


# ---------------------------------------------------------------- credentials
def get_credentials():
    user = os.environ.get("FA_USER") or DEFAULT_USER
    if not user:
        sys.exit("No username: set the FA_USER environment variable")
    pw = os.environ.get("FA_PASS")
    if not pw:
        try:
            pw = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
                capture_output=True, text=True, check=True,
            ).stdout.rstrip("\n")
        except (subprocess.CalledProcessError, FileNotFoundError):
            sys.exit(f"No password: add it with\n  security add-generic-password -s {KEYCHAIN_SERVICE} -a {user} -w")
    return user, pw


# ---------------------------------------------------------------- http/login
class _FormFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "kc-form-login":
            self.action = a.get("action")


def make_opener():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "de,en;q=0.8")]
    return op


def login(op, user, pw):
    with op.open(START_URL, timeout=60) as r:
        page = r.read().decode("utf-8", "replace")
        url = r.geturl()
    if url.startswith(BASE):
        return  # already logged in
    f = _FormFinder()
    f.feed(page)
    if not f.action:
        raise RuntimeError("Keycloak login form not found")
    body = urllib.parse.urlencode({"username": user, "password": pw, "credentialId": ""}).encode()
    with op.open(html.unescape(f.action), data=body, timeout=60) as r:
        r.read()
        if not r.geturl().startswith(BASE):
            raise RuntimeError("Login rejected (wrong password?)")


def api_get(op, path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    with op.open(req, timeout=120) as r:
        if not r.geturl().startswith(BASE):
            raise RuntimeError("Session lost (redirected to login)")
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- date range
def month_windows(start, end):
    """Yield (first_day, last_day) per calendar month covering [start, end]."""
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield max(cur, start), min(nxt - dt.timedelta(days=1), end)
        cur = nxt


def default_range(today):
    # From the start of the current academic year (1 Sep) to 13 months ahead.
    year = today.year if today.month >= 9 else today.year - 1
    start = dt.date(year, 9, 1)
    end_month = today.month + 13
    end = dt.date(today.year + (end_month - 1) // 12, (end_month - 1) % 12 + 1, 1) - dt.timedelta(days=1)
    return start, end


# ---------------------------------------------------------------- normalize
_TAG = re.compile(r"<[^>]+>")


def html_to_text(s):
    if not s:
        return ""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = html.unescape(_TAG.sub("", s))
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return "\n".join(line.strip() for line in s.splitlines()).strip()


def normalize(raw_items, lookups):
    locs, spk, aud, tags = (lookups.get(k, {}) for k in ("locations", "speakers", "audiences", "tags"))
    out = {}
    for it in raw_items:
        if it.get("type") != "event":
            continue
        e, o = it["item"]["e"], it["item"]["eo"]
        speakers = sorted(e.get("speakers") or [], key=lambda s: (not s.get("mainPresenter"), s.get("displayOrder") or 0))
        names = []
        for s in speakers:
            p = spk.get(str(s["id"]))
            if p:
                names.append(" ".join(x for x in (p.get("firstName"), p.get("lastName")) if x))
        groups = sorted({aud.get(str(a["id"]), {}).get("abb") or "" for a in e.get("audiences") or [] if a.get("mandatory")} - {""})
        ev = {
            "id": o["id"],
            "eventId": e["id"],
            "title": (e.get("title") or e.get("preliminaryTitle") or "Termin").strip(),
            "subTitle": (e.get("subTitle") or "").strip(),
            "start": o["start"][:16],
            "end": o["end"][:16],
            "locations": [locs.get(str(l), {}).get("name", f"Raum {l}") for l in o.get("locations") or []],
            "speakers": names if e.get("speakersPublished", True) else [],
            "mandatory": bool(e.get("mandatoryForStudent")),
            "cancelled": o.get("status") == "CANCELLED",
            "description": html_to_text((e.get("description") or {}).get("text")),
            "audiences": groups if len(groups) <= MAX_GROUPS else [],
            "tags": [tags.get(str(t if isinstance(t, int) else t.get("id")), {}).get("name", "") for t in e.get("tags") or []],
            "notes": html_to_text(e.get("notes")) if isinstance(e.get("notes"), str) else "",
            "slots": 1,
        }
        out[ev["id"]] = ev  # dedupe across overlapping windows
    return sorted(out.values(), key=lambda x: (x["start"], x["end"], x["title"]))


def _minutes(a, b):
    return int((dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds() // 60)


def merge_slots(events):
    """Merge back-to-back occurrences of the same event (e.g. 30-min office-hour slots)."""
    merged = []
    open_by_event = {}
    for ev in events:
        key = (ev["eventId"], tuple(ev["locations"]), ev["cancelled"])
        prev = open_by_event.get(key)
        # Only bookable office-hour style slots: optional, back-to-back, same day, same length.
        if (prev and not ev["mandatory"] and prev["end"] == ev["start"] and prev["start"][:10] == ev["start"][:10]
                and _minutes(ev["start"], ev["end"]) * prev["slots"] == _minutes(prev["start"], prev["end"])):
            prev["end"] = ev["end"]
            prev["slots"] += 1
            continue
        ev = dict(ev)
        merged.append(ev)
        open_by_event[key] = ev
    return merged


# ---------------------------------------------------------------- iCalendar
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Berlin
X-LIC-LOCATION:Europe/Berlin
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


def ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def ics_fold(line):
    """Fold to 75 octets per RFC 5545 without splitting UTF-8 sequences."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def ics_dt(s):
    return s.replace("-", "").replace(":", "") + "00"  # 2026-10-05T10:00 -> 20261005T100000


def event_description(ev):
    parts = []
    if ev["subTitle"]:
        parts.append(ev["subTitle"])
    parts.append("Pflicht" if ev["mandatory"] else "Optional")
    if ev["slots"] > 1:
        parts.append(f"{ev['slots']} Termine à {slot_minutes(ev)} Min. (einzeln buchbar)")
    if ev["speakers"]:
        parts.append("Dozent*innen: " + ", ".join(ev["speakers"]))
    if ev["audiences"]:
        parts.append("Pflicht für: " + ", ".join(ev["audiences"]))
    if ev["description"]:
        parts.append("\n" + ev["description"])
    parts.append("\n" + START_URL + "?from=" + ev["start"][:10] + "&to=" + ev["start"][:10])
    return "\n".join(parts)


def slot_minutes(ev):
    a = dt.datetime.fromisoformat(ev["start"])
    b = dt.datetime.fromisoformat(ev["end"])
    return int((b - a).total_seconds() // 60 // max(ev["slots"], 1))


def build_ics(events, name, stamp):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Filmakademie Kalender Sync//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + ics_escape(name),
        "X-WR-TIMEZONE:" + TZ,
        *VTIMEZONE.split("\n"),
    ]
    for ev in events:
        summary = ev["title"]
        if ev["cancelled"]:
            summary = "ABGESAGT: " + summary
        elif not ev["mandatory"]:
            summary = summary + " (optional)"
        lines += [
            "BEGIN:VEVENT",
            f"UID:fa-{ev['id']}@ev3nts.filmakademie.de",
            "DTSTAMP:" + stamp,
            f"DTSTART;TZID={TZ}:" + ics_dt(ev["start"]),
            f"DTEND;TZID={TZ}:" + ics_dt(ev["end"]),
            "SUMMARY:" + ics_escape(summary),
        ]
        if ev["locations"]:
            lines.append("LOCATION:" + ics_escape(", ".join(ev["locations"])))
        lines.append("DESCRIPTION:" + ics_escape(event_description(ev)))
        lines.append("CATEGORIES:" + ("Pflicht" if ev["mandatory"] else "Optional"))
        lines.append("STATUS:" + ("CANCELLED" if ev["cancelled"] else "CONFIRMED"))
        lines.append("TRANSP:" + ("OPAQUE" if ev["mandatory"] else "TRANSPARENT"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


# ---------------------------------------------------------------- payload for the artifact db
def compact(ev):
    c = {"i": ev["id"], "s": ev["start"], "e": ev["end"], "t": ev["title"]}
    if ev["subTitle"]:
        c["u"] = ev["subTitle"]
    if ev["locations"]:
        c["l"] = ev["locations"]
    if ev["speakers"]:
        c["p"] = ev["speakers"]
    if ev["mandatory"]:
        c["m"] = 1
    if ev["cancelled"]:
        c["x"] = 1
    if ev["slots"] > 1:
        c["n"] = ev["slots"]
    if ev["description"]:
        c["d"] = ev["description"][:1500]
    if ev["audiences"]:
        c["a"] = ev["audiences"]
    return c


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--out", default=HERE)
    ap.add_argument("--prev", help="previous db document (JSON) to carry hash/changedAt from; defaults to <out>/schedule.json")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = dt.date.today()
    start, end = default_range(today)
    if args.frm:
        start = dt.date.fromisoformat(args.frm)
    if args.to:
        end = dt.date.fromisoformat(args.to)

    user, pw = get_credentials()
    op = make_opener()
    try:
        login(op, user, pw)
    except Exception as e:  # noqa: BLE001
        print("LOGIN FAILED:", e, file=sys.stderr)
        return 2

    try:
        lookups = api_get(op, "lookups")
        raw = []
        for a, b in month_windows(start, end):
            data = api_get(op, "schedule", {"from": a.isoformat(), "to": b.isoformat(), "showWeekend": "true", "mine": "true"})
            for page in data.get("pages", []):
                for series in page.get("series", []):
                    raw.extend(series.get("items", []))
    except Exception as e:  # noqa: BLE001
        print("FETCH FAILED:", e, file=sys.stderr)
        return 3

    events = merge_slots(normalize(raw, lookups))

    sched_path = os.path.join(args.out, "schedule.json")
    prev = {}
    prev_path = args.prev or sched_path
    if os.path.exists(prev_path):
        try:
            prev = json.load(open(prev_path, encoding="utf-8"))
            if isinstance(prev.get("data"), dict):  # ArtifactData export wraps the body
                prev = prev["data"]
        except Exception:  # noqa: BLE001
            prev = {}
    prev_count = prev.get("count") or len(prev.get("events") or [])
    if not events and prev_count > 10:
        print(f"Got 0 events but had {prev_count} before; keeping old files.", file=sys.stderr)
        return 4

    content_hash = hashlib.sha256(json.dumps(events, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    iso_now = now.isoformat().replace("+00:00", "Z")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    prev_hash, prev_synced = prev.get("hash"), prev.get("changedAt")
    changed = content_hash != prev_hash
    changed_at = iso_now if changed or not prev_synced else prev_synced

    meta = {"checkedAt": iso_now, "changedAt": changed_at, "from": start.isoformat(), "to": end.isoformat(), "hash": content_hash, "tz": TZ}
    write_atomic(sched_path, json.dumps({**meta, "events": events}, ensure_ascii=False, indent=1))
    write_atomic(os.path.join(args.out, "Filmakademie-alle.ics"), build_ics(events, "Filmakademie", stamp))
    write_atomic(os.path.join(args.out, "Filmakademie-Pflicht.ics"), build_ics([e for e in events if e["mandatory"]], "Filmakademie (Pflicht)", stamp))
    payload = {**meta, "count": len(events), "events": [compact(e) for e in events]}
    payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(payload_text.encode()) > 250_000:
        print("WARNING: db payload exceeds 250 KB; descriptions will be trimmed", file=sys.stderr)
        for c in payload["events"]:
            c.pop("d", None)
        payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    write_atomic(os.path.join(args.out, "db_payload.json"), payload_text)

    mand = sum(e["mandatory"] for e in events)
    print(f"OK {len(events)} events ({mand} Pflicht) {start}..{end} changed={str(changed).lower()} payload={len(payload_text.encode())}B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
