"""ICS calendar feeds (e.g. a calendar's "secret address in iCal format") as a CalendarSource.

The feed URLs carry access tokens, so they live in the Keychain (account ics-urls, one URL per line) and
are never logged.

With the optional extra (`pip install 'kinchat[ics]'`: icalendar + recurring-ical-events), recurring events
are expanded inside the snapshot window: RRULE, RDATE and EXDATE, and a moved instance (RECURRENCE-ID)
replaces its original slot. Without it a small stdlib parser is used, which does NOT expand recurring
events (only a series' first occurrence is seen, if it falls in the window); calendar health then reads
degraded with "recurring events not expanded; install kinchat[ics]".

Events are dropped when any address in calendar.owner_emails is an attendee whose PARTSTAT is NEEDS-ACTION
or DECLINED, mirroring the Graph rule that unanswered invites stay out of the prompt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..health import Health
from .calendar import clean, format_event, section

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 5_000_000
NO_EXPANSION = "recurring events not expanded; install kinchat[ics]"


def expansion_available() -> bool:
    try:
        import icalendar  # noqa: F401
        import recurring_ical_events  # noqa: F401
    except ImportError:
        return False
    return True
UNANSWERED = {"NEEDS-ACTION", "DECLINED"}


@dataclass
class Prop:
    name: str
    params: dict[str, str]
    value: str


@dataclass
class Event:
    start: datetime
    end: datetime
    all_day: bool
    summary: str
    location: str
    busy: str
    cancelled: bool
    recurring: bool
    attendees: dict[str, str] = field(default_factory=dict)  # lower-case address -> PARTSTAT


def unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def parse_line(line: str) -> Prop:
    # NAME;PARAM=a;PARAM="b:c":value  (a colon inside a quoted parameter is not the separator)
    in_quotes, split = False, None
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            split = i
            break
    head, value = (line, "") if split is None else (line[:split], line[split + 1:])
    name, *params = head.split(";")
    pd = {}
    for p in params:
        k, _, v = p.partition("=")
        pd[k.upper()] = v.strip('"')
    return Prop(name.upper(), pd, value)


def unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace(r"\;", ";").replace("\\\\", "\\")


def _zone(name: str | None, default: ZoneInfo) -> ZoneInfo:
    if not name:
        return default
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return default


def parse_dt(prop: Prop, default_tz: ZoneInfo) -> tuple[datetime, bool]:
    """(aware datetime, is a date). Dates are midnight in default_tz; floating times are taken as default_tz."""
    v = prop.value.strip()
    if prop.params.get("VALUE", "").upper() == "DATE" or (len(v) == 8 and v.isdigit()):
        d = date(int(v[:4]), int(v[4:6]), int(v[6:8]))
        return datetime.combine(d, time(0), default_tz), True
    utc = v.endswith("Z")
    v = v.rstrip("Z")
    dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    if utc:
        return dt.replace(tzinfo=ZoneInfo("UTC")), False
    return dt.replace(tzinfo=_zone(prop.params.get("TZID"), default_tz)), False


def parse_duration(v: str) -> timedelta:
    sign = -1 if v.startswith("-") else 1
    v = v.lstrip("+-").lstrip("P")
    days = secs = 0
    num = ""
    in_time = False
    for ch in v:
        if ch.isdigit():
            num += ch
        elif ch == "T":
            in_time = True
        else:
            n = int(num or 0)
            num = ""
            if ch == "W":
                days += 7 * n
            elif ch == "D":
                days += n
            elif ch == "H" and in_time:
                secs += 3600 * n
            elif ch == "M" and in_time:
                secs += 60 * n
            elif ch == "S" and in_time:
                secs += n
    return sign * timedelta(days=days, seconds=secs)


def parse_calendar(text: str, default_tz: ZoneInfo) -> tuple[str | None, list[Event]]:
    """(calendar name, events). Unparseable events are skipped."""
    name, events, cur, depth = None, [], None, 0
    cal_tz = default_tz
    for line in unfold(text):
        p = parse_line(line)
        if p.name == "BEGIN":
            if p.value.upper() == "VEVENT" and depth == 0:
                cur = []
            elif cur is not None:
                depth += 1  # VALARM etc. inside an event
            continue
        if p.name == "END":
            if cur is not None and depth:
                depth -= 1
            elif cur is not None and p.value.upper() == "VEVENT":
                ev = _event(cur, cal_tz)
                if ev:
                    events.append(ev)
                cur = None
            continue
        if cur is not None:
            if not depth:
                cur.append(p)
        elif p.name == "X-WR-CALNAME":
            name = unescape(p.value)
        elif p.name == "X-WR-TIMEZONE":
            cal_tz = _zone(p.value, default_tz)
    return name, events


def _event(props: list[Prop], tz: ZoneInfo) -> Event | None:
    by = {}
    attendees = {}
    for p in props:
        if p.name == "ATTENDEE":
            addr = p.value.split(":", 1)[1] if p.value.lower().startswith("mailto:") else p.value
            attendees[addr.strip().lower()] = p.params.get("PARTSTAT", "NEEDS-ACTION").upper()
        else:
            by.setdefault(p.name, p)
    if "DTSTART" not in by:
        return None
    try:
        start, all_day = parse_dt(by["DTSTART"], tz)
        if "DTEND" in by:
            end, _ = parse_dt(by["DTEND"], tz)
        elif "DURATION" in by:
            end = start + parse_duration(by["DURATION"].value)
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(0))
    except (ValueError, IndexError):
        return None
    transp = by.get("TRANSP")
    return Event(start=start, end=end, all_day=all_day,
                 summary=unescape(by["SUMMARY"].value) if "SUMMARY" in by else "",
                 location=unescape(by["LOCATION"].value) if "LOCATION" in by else "",
                 busy="free" if transp and transp.value.upper() == "TRANSPARENT" else "busy",
                 cancelled=by.get("STATUS") is not None and by["STATUS"].value.upper() == "CANCELLED",
                 recurring="RRULE" in by, attendees=attendees)


def owned(ev: Event, owner_emails: list[str]) -> bool:
    """False when any owner is an attendee who hasn't accepted (NEEDS-ACTION) or declined."""
    return not any(ev.attendees.get(e.lower()) in UNANSWERED for e in owner_emails)


def snapshot_lines(events: list[Event], owner_emails: list[str], tz: ZoneInfo, start: datetime,
                   end: datetime) -> list[str]:
    keep = [e for e in events if not e.cancelled and owned(e, owner_emails) and e.start < end and e.end > start]
    keep.sort(key=lambda e: e.start)
    lines = []
    for e in keep:
        s = e.start if e.all_day else e.start.astimezone(tz)
        f = e.end if e.all_day else e.end.astimezone(tz)
        lines.append(format_event(s, f, e.all_day, e.summary, e.location, e.busy))
    return lines


def _expanded_events(text: str, default_tz: ZoneInfo, start: datetime, end: datetime) -> tuple[str | None, list[Event]]:
    """Every occurrence overlapping [start, end), using icalendar + recurring-ical-events."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(text)
    name = cal.get("X-WR-CALNAME")
    cal_tz = _zone(str(cal.get("X-WR-TIMEZONE")) if cal.get("X-WR-TIMEZONE") else None, default_tz)

    def aware(value) -> tuple[datetime, bool]:
        if isinstance(value, datetime):
            return (value if value.tzinfo else value.replace(tzinfo=cal_tz)), False
        return datetime.combine(value, time(0), cal_tz), True  # a DATE: all day

    events = []
    query = recurring_ical_events.of(cal, skip_bad_series=True)
    for comp in query.between(start, end):
        try:
            s, all_day = aware(comp.decoded("DTSTART"))
            if comp.get("DTEND") is not None:
                e, _ = aware(comp.decoded("DTEND"))
            elif comp.get("DURATION") is not None:
                e = s + comp.decoded("DURATION")
            else:
                e = s + (timedelta(days=1) if all_day else timedelta(0))
        except (KeyError, ValueError, TypeError):
            continue
        atts = comp.get("ATTENDEE") or []
        if not isinstance(atts, list):
            atts = [atts]
        attendees = {}
        for a in atts:
            addr = str(a)
            addr = addr.split(":", 1)[1] if addr.lower().startswith("mailto:") else addr
            attendees[addr.strip().lower()] = str(a.params.get("PARTSTAT", "NEEDS-ACTION")).upper()
        transp, status = comp.get("TRANSP"), comp.get("STATUS")
        events.append(Event(start=s, end=e, all_day=all_day, summary=str(comp.get("SUMMARY") or ""),
                            location=str(comp.get("LOCATION") or ""),
                            busy="free" if transp and str(transp).upper() == "TRANSPARENT" else "busy",
                            cancelled=bool(status) and str(status).upper() == "CANCELLED",
                            recurring=comp.get("RRULE") is not None, attendees=attendees))
    return (str(name) if name else None), events


class IcsCalendar:
    name = "ics"

    def __init__(self, urls: list[str], owner_emails: list[str], tz: ZoneInfo, days: int, fetch=None,
                 expand: bool | None = None):
        self.urls = [u.strip() for u in urls if u.strip()]
        self.owner_emails = owner_emails
        self.tz = tz
        self.days = days
        self._fetch = fetch or self._http_get
        self.expand = expansion_available() if expand is None else expand
        # Shown as calendar health after a successful refresh without the [ics] extra.
        self.limitation = None if self.expand else NO_EXPANSION
        self.health = Health("calendar", "Calendar (ICS)")

    @staticmethod
    def _http_get(url: str) -> str:
        import httpx
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            if len(r.content) > MAX_FEED_BYTES:
                raise ValueError("calendar feed is too large")
            return r.text

    def snapshot(self, now: datetime | None = None) -> str:
        start = (now or datetime.now(self.tz)).astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=self.days)
        sections = []
        for i, url in enumerate(self.urls, 1):
            try:
                text = self._fetch(url)
                name, events = (_expanded_events(text, self.tz, start, end) if self.expand
                                else parse_calendar(text, self.tz))
            except Exception as e:
                # Never log the URL: it is a credential.
                raise RuntimeError(f"calendar feed {i}: {type(e).__name__}") from None
            sections.append(section(clean(name) or f"Calendar {i}",
                                    snapshot_lines(events, self.owner_emails, self.tz, start, end)))
        return "\n\n".join(sections)
