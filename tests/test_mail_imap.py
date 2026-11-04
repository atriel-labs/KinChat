import logging
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default

from conftest import CHAT

from kinchat.backends.mail_imap import ImapMail, _quote_mailbox, reply_text
from kinchat.backends.mailbox import MailboxBackend
from kinchat.config import Imap

ME = "assistant@example.com"
PASSING_AUTH = "mx.example.com; dkim=pass header.d=example.com; spf=pass smtp.mailfrom=example.com; dmarc=pass"


def raw(subject, body, sender=ME, auth=None, html=False):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, ME, subject
    if auth:
        m["Authentication-Results"] = auth
    m.set_content(body)
    if html:
        m.add_alternative(f"<p>{body}</p>", subtype="html")
    return m.as_bytes()


class FakeImap:
    """Just enough of imaplib.IMAP4 for ImapMail: folders map to (UIDVALIDITY, {uid: raw message})."""

    def __init__(self, folders):
        self.folders = folders
        self.selected = None
        self.logins = []

    def login(self, user, password):
        self.logins.append((user, password))

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        assert readonly
        self.selected = name
        return "OK", [str(len(self.folders[name][1])).encode()]

    def response(self, code):
        return code, [str(self.folders[self.selected][0]).encode()]

    def uid(self, command, *args):
        validity, msgs = self.folders[self.selected]
        if command == "SEARCH":
            crit = args[1]
            if crit == "UID *":
                return "OK", [str(max(msgs)).encode() if msgs else b""]
            lo = int(crit.split()[1].split(":")[0])
            hits = [u for u in sorted(msgs) if u >= lo] or ([max(msgs)] if msgs else [])  # IMAP: n:* includes the last
            return "OK", [" ".join(map(str, hits)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), msgs[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        pass


class FakeSmtp:
    def __init__(self):
        self.sent = []

    def login(self, user, password):
        self.user = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def setup(cfg, store, folders, posts):
    imap_cfg = Imap(host="imap.example.com", username=ME, sent_folder="Sent Items")
    server = FakeImap(folders)
    smtp = FakeSmtp()
    mail = ImapMail(imap_cfg, "app-pw", store, imap_factory=lambda: server, smtp_factory=lambda: smtp)
    backend = MailboxBackend(cfg, store, mail, lambda chat, text: posts.append((chat, text)))
    return backend, server, smtp


def baseline(store, validity=7, last=0):
    store.kv_set("imap_uidvalidity", str(validity))
    store.kv_set("imap_last_uid", str(last))


def test_forged_inbox_message_rejected_sent_message_accepted(cfg, store, caplog):
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    store.create_task("T-0002", CHAT, "s", None, "Alex")
    forged = raw("RE: [MIA-POST] T-0001", "Booked! (not really)\nSTATUS: done", auth=PASSING_AUTH)
    genuine = raw("RE: [MIA-POST] T-0002", "Table for 4 at 7.\nSTATUS: done\n---\nnotes", auth=PASSING_AUTH)
    folders = {"INBOX": (7, {1: forged}), "Sent Items": (7, {1: genuine})}
    posts = []
    backend, server, _ = setup(cfg, store, folders, posts)
    baseline(store)
    with caplog.at_level(logging.INFO):
        assert backend.poll() == 1
    assert posts == [(CHAT, "Table for 4 at 7.")]
    assert store.task_is_open("T-0001") and store.get_task("T-0002").status == "done"
    assert server.selected == "Sent Items"  # the Inbox is never opened
    assert "not trusted" in caplog.text and "dkim=pass" in caplog.text


def test_uid_dedup_and_bookmark(cfg, store):
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    sent = {1: raw("[MIA-POST] T-0001", "first"), 2: raw("Lunch plans", "not a result")}
    folders = {"Sent Items": (7, sent)}
    posts = []
    backend, _, _ = setup(cfg, store, folders, posts)
    baseline(store)
    assert backend.poll() == 1 and backend.poll() == 0
    assert store.kv_get("imap_last_uid") == "2" and store.already_posted("imap:7:1")
    store.kv_set("imap_last_uid", "0")  # even if the bookmark is lost, the (UIDVALIDITY, UID) key stops a repost
    assert backend.poll() == 0 and posts == [(CHAT, "first")]
    sent[3] = raw("Re: [MIA-POST] T-0001", "second")
    assert backend.poll() == 1 and posts[-1] == (CHAT, "second")


def test_uidvalidity_change_restarts_from_newest(cfg, store):
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    folders = {"Sent Items": (7, {1: raw("[MIA-POST] T-0001", "first")})}
    posts = []
    backend, _, _ = setup(cfg, store, folders, posts)
    baseline(store)
    assert backend.poll() == 1
    # The folder is rebuilt: new UIDVALIDITY, UIDs reused. The old message is not posted again...
    folders["Sent Items"] = (8, {1: raw("[MIA-POST] T-0001", "first"), 2: raw("[MIA-POST] T-0001", "old copy")})
    assert backend.poll() == 0 and len(posts) == 1
    assert (store.kv_get("imap_uidvalidity"), store.kv_get("imap_last_uid")) == ("8", "2")
    # ...and new results after the change are.
    folders["Sent Items"][1][3] = raw("[MIA-POST] T-0001", "after the change")
    assert backend.poll() == 1 and posts[-1] == (CHAT, "after the change")


def test_first_run_starts_from_now(cfg, store):
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    folders = {"Sent Items": (7, {1: raw("[MIA-POST] T-0001", "from last week")})}
    posts = []
    backend, _, _ = setup(cfg, store, folders, posts)
    assert backend.poll() == 0 and posts == []
    assert store.kv_get("imap_last_uid") == "1"


def test_sent_message_from_other_address_rejected(cfg, store):
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    folders = {"Sent Items": (7, {1: raw("[MIA-POST] T-0001", "hi", sender="someone@example.com")})}
    posts = []
    backend, _, _ = setup(cfg, store, folders, posts)
    baseline(store)
    assert backend.poll() == 0 and posts == []


def test_send_uses_smtp_to_self(cfg, store):
    backend, _, smtp = setup(cfg, store, {"Sent Items": (7, {})}, [])
    cfg.shadow_mode = False
    from test_tasks import req
    t = backend.hand_off(req(), CHAT, "Alex")
    (msg,) = smtp.sent
    assert msg["From"] == msg["To"] == ME and msg["Subject"] == "[MIA-CHAT] T-0001: Book dinner Sat 7pm for 4"
    assert smtp.user == (ME, "app-pw") and t.email_msg_id == msg["Message-ID"]


def test_reply_text_drops_quoted_original():
    msg = BytesParser(policy=default).parsebytes(raw(
        "x", "Booked for 7.\nSTATUS: done\n\nOn Tue, Nov 3, 2026 at 9:00 AM Assistant wrote:\n> [MIA-CHAT] book it",
        html=True))
    assert reply_text(msg).strip() == "Booked for 7.\nSTATUS: done"


def test_quote_mailbox():
    assert _quote_mailbox("Sent") == "Sent"
    assert _quote_mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'
