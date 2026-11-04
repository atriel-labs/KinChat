"""IMAP + SMTP as a MailTransport, for mailboxes outside Microsoft 365 (stdlib imaplib/smtplib only).

Results are read ONLY from the Sent folder. A [<TAG>-POST] result is written by the agent, which sends
as the assistant's own account, so a genuine one is always in Sent; anyone on the internet can put a
message in the Inbox, with any From and any Authentication-Results header they like. That header is
logged for diagnosis and never trusted.

Each message is identified by (UIDVALIDITY, UID). The highest UID handled is kept in the store; when
UIDVALIDITY changes, old UIDs mean nothing, so the bookmark restarts at the newest message rather than
re-reading the folder (a duplicate post is worse than a missed one).
"""
from __future__ import annotations

import imaplib
import logging
import re
import smtplib
import ssl
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from ..store import Store
from .mail import ResultMail

log = logging.getLogger(__name__)

VALIDITY_KEY = "imap_uidvalidity"
LAST_UID_KEY = "imap_last_uid"
QUOTE_HEADER_RE = re.compile(r"^\s*On .+ wrote:\s*$")


def _quote_mailbox(name: str) -> str:
    if name.startswith('"') or not re.search(r'[\s"\\()\[\]{}]', name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def reply_text(msg) -> str:
    """The plain-text body without the quoted message it replies to."""
    part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
    try:
        text = part.get_content() if part is not None else ""
    except (KeyError, LookupError):
        text = ""
    lines = []
    for line in str(text).replace("\r\n", "\n").split("\n"):
        if QUOTE_HEADER_RE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines)


class ImapMail:
    name = "imap"

    def __init__(self, cfg, password: str, store: Store, imap_factory=None, smtp_factory=None):
        self.cfg = cfg
        self.address = cfg.address or cfg.username
        self._password = password
        self._store = store
        self._imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL(cfg.host, cfg.port,
                                                                         ssl_context=ssl.create_default_context()))
        self._smtp_factory = smtp_factory or self._default_smtp
        self._pending: tuple[str, int] | None = None

    # --- sending --------------------------------------------------------------

    def _default_smtp(self):
        host = self.cfg.smtp_host or self.cfg.host
        if self.cfg.smtp_port == 465:
            return smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=30)
        smtp = smtplib.SMTP(host, self.cfg.smtp_port, timeout=30)
        smtp.starttls(context=ssl.create_default_context())
        return smtp

    def send(self, subject: str, body: str) -> str | None:
        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = self.address
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.address.rpartition("@")[2] or None)
        msg.set_content(body)
        smtp = self._smtp_factory()
        try:
            smtp.login(self.cfg.username, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return msg["Message-ID"]

    # --- results ---------------------------------------------------------------

    def fetch_results(self, subject_re: re.Pattern, tag: str) -> list[ResultMail]:
        imap = self._imap_factory()
        try:
            imap.login(self.cfg.username, self._password)
            typ, _ = imap.select(_quote_mailbox(self.cfg.sent_folder), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"IMAP: cannot open the Sent folder {self.cfg.sent_folder!r}")
            validity = str(int(imap.response("UIDVALIDITY")[1][0]))
            last = self._bookmark(imap, validity)
            uids = [u for u in self._search(imap, f"UID {last + 1}:*") if u > last]
            out = []
            for uid in sorted(uids):
                msg = self._fetch(imap, uid)
                if msg is None:
                    continue
                m = subject_re.match(str(msg.get("Subject", "")))
                if not m:
                    continue
                key = f"imap:{validity}:{uid}"
                log.info("[%s-POST] %s Authentication-Results (logged, not trusted): %s",
                         tag, key, " | ".join(str(h) for h in msg.get_all("Authentication-Results", [])) or "none")
                sender = parseaddr(str(msg.get("From", "")))[1].lower()
                if sender != self.address.lower():
                    log.warning("rejected [%s-POST] %s: sender %s is not %s", tag, key, sender or "?", self.address)
                    continue
                out.append(ResultMail(key, m.group(1).upper(), reply_text(msg)))
            self._pending = (validity, max(uids, default=last))
            return out
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def commit(self) -> None:
        if self._pending:
            validity, last = self._pending
            self._store.kv_set(VALIDITY_KEY, validity)
            self._store.kv_set(LAST_UID_KEY, str(last))
            self._pending = None

    def _bookmark(self, imap, validity: str) -> int:
        stored_validity, stored_last = self._store.kv_get(VALIDITY_KEY), self._store.kv_get(LAST_UID_KEY)
        if stored_validity == validity and stored_last is not None:
            return int(stored_last)
        newest = max(self._search(imap, "UID *"), default=0)
        if stored_validity is not None:
            log.warning("IMAP UIDVALIDITY of %s changed (%s -> %s); starting again from the newest message",
                        self.cfg.sent_folder, stored_validity, validity)
        self._store.kv_set(VALIDITY_KEY, validity)
        self._store.kv_set(LAST_UID_KEY, str(newest))
        return newest

    @staticmethod
    def _search(imap, criteria: str) -> list[int]:
        typ, data = imap.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        return [int(x) for x in b" ".join(d for d in data if d).split()]

    @staticmethod
    def _fetch(imap, uid: int):
        typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            return None
        raw = next((d[1] for d in data if isinstance(d, tuple) and len(d) > 1), None)
        return message_from_bytes(raw, policy=policy.default) if raw else None
