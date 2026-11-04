"""Microsoft Graph client: app-only certificate auth, the assistant's inbox delta, task mail, calendar snapshot."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import msal

from .backends.calendar import MAX_EVENT_FIELD, clean, format_event, section  # noqa: F401

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


class Graph:
    def __init__(self, tenant: str, client_id: str, cert_thumbprint: str, private_key_pem: str,
                 mailbox: str, timezone: str):
        self.mailbox = mailbox
        self._tz = timezone
        self._app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            client_credential={"thumbprint": cert_thumbprint, "private_key": private_key_pem},
        )
        self._http = httpx.Client(base_url=GRAPH, timeout=30)

    def _headers(self, **extra) -> dict:
        tok = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in tok:
            raise RuntimeError(f"Graph auth failed: {tok.get('error')}: {tok.get('error_description')}")
        return {"Authorization": f"Bearer {tok['access_token']}", **extra}

    def _get(self, url: str, params: dict | None = None, **headers) -> dict:
        r = self._http.get(url, params=params, headers=self._headers(**headers))
        r.raise_for_status()
        return r.json()

    # --- inbox ------------------------------------------------------------

    def inbox_delta(self, delta_link: str | None, since: datetime) -> tuple[list[dict], str]:
        """Return (new or changed inbox messages, next delta link)."""
        if delta_link:
            url, params = delta_link, None
        else:
            url = f"/users/{self.mailbox}/mailFolders/inbox/messages/delta"
            params = {
                "$select": "subject,from,receivedDateTime,conversationId,internetMessageId",
                "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            }
        items: list[dict] = []
        while True:
            data = self._get(url, params, Prefer="odata.maxpagesize=50")
            items.extend(m for m in data.get("value", []) if "@removed" not in m)
            if "@odata.nextLink" in data:
                url, params = data["@odata.nextLink"], None
                continue
            return items, data["@odata.deltaLink"]

    def get_message_for_post(self, message_id: str) -> dict:
        """Fetch the fields the result acceptance rules need, with the reply-only body as plain text."""
        return self._get(
            f"/users/{self.mailbox}/messages/{message_id}",
            {"$select": "subject,from,sender,uniqueBody,internetMessageHeaders,conversationId"},
            Prefer='outlook.body-content-type="text"',
        )

    def send_to_self(self, subject: str, body: str) -> str | None:
        """Send mail from the assistant's mailbox to itself. Returns the internetMessageId of the sent message."""
        msg = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": self.mailbox}}],
        }
        r = self._http.post(f"/users/{self.mailbox}/messages", json=msg, headers=self._headers())
        r.raise_for_status()
        draft = r.json()
        r = self._http.post(f"/users/{self.mailbox}/messages/{draft['id']}/send", headers=self._headers())
        r.raise_for_status()
        return draft.get("internetMessageId")

    # --- calendar ---------------------------------------------------------

    def calendar_snapshot(self, users: list[str], days: int) -> str:
        tz = ZoneInfo(self._tz)
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=days)
        sections = []
        for user in users:
            url = f"/users/{user}/calendarView"
            params = {
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$select": "subject,start,end,isAllDay,showAs,location,isCancelled,isOrganizer,responseStatus",
                "$orderby": "start/dateTime",
                "$top": "100",
            }
            lines = []
            while url:
                data = self._get(url, params, Prefer=f'outlook.timezone="{self._tz}"')
                for ev in data.get("value", []):
                    if ev.get("isCancelled") or not _owned_event(ev):
                        continue
                    lines.append(_format_event(ev))
                url, params = data.get("@odata.nextLink"), None
            sections.append(section(user, lines))
        return "\n\n".join(sections)


def _owned_event(ev: dict) -> bool:
    """Only events the calendar owner made or accepted. Anyone can drop an invite on a calendar, and its
    title and location go into the reply prompt, so unanswered invites are left out."""
    response = (ev.get("responseStatus") or {}).get("response", "none")
    return bool(ev.get("isOrganizer")) or response in ("organizer", "accepted", "tentativelyAccepted")


_clean = clean  # kept for callers of the old private name


def _format_event(ev: dict) -> str:
    s = datetime.fromisoformat(ev["start"]["dateTime"][:19])
    e = datetime.fromisoformat(ev["end"]["dateTime"][:19])
    return format_event(s, e, bool(ev.get("isAllDay")), ev.get("subject"), (ev.get("location") or {}).get("displayName"),
                        ev.get("showAs", "busy"))
