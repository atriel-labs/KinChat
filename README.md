# KinChat
A Claude-powered assistant for your family’s iMessage group chat. Answers in seconds, hands real tasks to an agent, and runs on a Mac you own with no incoming connections.

> **Status:** being prepared for open source. The assistant is still called Mia by default; the name becomes configurable next.

Puts Mia in the family iMessage group. The bridge reads new messages straight from the Messages
database (`chat.db`), routes each one (Claude Haiku), replies instantly (Claude Sonnet) by scripting
Messages, hands real work to Mia in Duet by `[MIA-CHAT]` email, and posts `[MIA-POST]` results back.
A password-protected local dashboard controls who Mia talks to. Nothing listens beyond 127.0.0.1.

```
kinchat/
  messages.py   chat.db reader + AppleScript sender       router.py    dashboard rules → filters → controls → cues → window → classifier
  settings.py   features, contacts, chats, audit           outbound.py  can-speak check, quiet hours outbox, 6/10 min rate limit, shadow mode
  dashboard.py  Flask + plain HTML forms on 127.0.0.1:8770 tasks.py     [MIA-CHAT] handoff, [MIA-POST] acceptance rules
  llm.py        classifier + replier (structured output)   graph.py     app-only cert auth, inbox delta, mail, calendar snapshot
  app.py        chat.db watcher, outbox ticker, inbox poll, calendar refresh, hourly heartbeat, 30-day prune
```

## Setup

1. **Mac account.** Log in as the dedicated `mia` macOS user. Sign in to Messages with the assistant's own Apple ID.
   Turn on FileVault and the firewall ("Block all incoming connections"), and disable sleep.
2. **Install:** `scripts/install.sh`. It creates the venv, copies `config.yaml` and `family.md` into
   `~/Library/Application Support/kinchat/`, loads the launchd agent `io.kinchat.bridge`, and prints
   the Python binary that needs Full Disk Access. The agent restarts repeatedly until steps 3–5 are done.
3. **Permissions.** In System Settings → Privacy & Security → Full Disk Access, add the Python binary the
   installer printed (needed to read `chat.db`). Then run `.venv/bin/python -m kinchat check` once from
   Terminal and allow the "control Messages" Automation prompt.
4. **Entra "Mia Bridge" app.** Certificate auth, `Mail.ReadWrite` / `Mail.Send` / `Calendars.Read`, scoped
   with Exchange RBAC for Applications, verified with `Test-ServicePrincipalAuthorization`.
   Put `graph.client_id` and `graph.cert_thumbprint` in `config.yaml`.
5. **Secrets in the login Keychain:**
   ```
   security add-generic-password -s kinchat -a anthropic-api-key -w 'sk-ant-...'
   security add-generic-password -s kinchat -a dashboard-password -w '...'
   security add-generic-password -s kinchat -a graph-cert-key-b64 -w "$(base64 -i key.pem | tr -d '\n')"
   ```
6. **Verify:** `.venv/bin/python -m kinchat check`, then `launchctl kickstart -k gui/$(id -u)/io.kinchat.bridge`.
7. **Dashboard:** open http://127.0.0.1:8770. Add the family members as contacts. From a family member's phone, start a
   group with the others and the assistant's Apple ID and send a message. The group appears under Approved chats as
   **Pending**; set it to **Respond**.

`shadow_mode: true` in `config.yaml` is the default: decisions and draft replies are logged
(`~/Library/Logs/kinchat/bridge.log`, and the dashboard's "Recent routing decisions") but nothing is sent.
Review 2–3 days, tune thresholds, then set it to `false` and restart.

**Upgrading from mia-bridge:** an existing `~/Library/Application Support/mia-bridge/` directory, Keychain
items under `ai.atriel.mia-bridge` and `MIA_BRIDGE_*` environment variables are still found, so a live install
keeps working. `scripts/install.sh` replaces the old `ai.atriel.mia-bridge` launchd agent with `io.kinchat.bridge`.

## Dashboard rules

- Senders who aren't approved contacts are ignored and logged, including inside an approved group.
- A group is engaged only when approved; new groups appear as Pending. Modes: Respond, Listen only, Off.
- One-on-one chats are answered only when the toggle is on and that contact has one-on-one allowed.
- Tasks are created only when Task handoff is on and the contact can assign tasks.
- Every change goes to the audit list. Settings are re-read on every message, so changes apply at once.
- Login is a password from the Keychain; forms carry a CSRF token and only `127.0.0.1`/`localhost` Host headers are served.

## Behaviour notes (choices the spec left open)

- **Quiet hours (10 PM–7 AM):** outgoing replies and results are held in `state.db` and sent combined at 7 AM.
  A message counts as urgent, and is answered right away, if it contains "urgent", "asap" or "emergency".
  Tasks are still handed to Duet overnight.
- **Instant replies off:** Mia still runs the replier to detect tasks, then posts only "On it, I'll let you know."
- **Heartbeat:** an hourly `[MIA-HEARTBEAT]` email from the assistant's mailbox to itself with last `chat.db` read,
  last inbox poll and open-task count. Add to Duet's standing instruction: *"If no [MIA-HEARTBEAT] email has
  arrived in the last 2 hours, tell the owner the iMessage bridge may be down. Never reply to heartbeat emails."*
- **Controls** ("sit this one out" / "you're back") get a fixed one-line ack. **Closing phrases** close the window, no reply.
- **Restart:** the last processed `chat.db` row is saved; messages more than 5 minutes old when first seen
  are stored for context but not answered. The first run starts from the newest row, not from history.
- **Results** post to the chat the task came from, never another chat; no STATUS line keeps the task open.
- **Reply model** sends the server-side refusal fallback (`fallbacks: "default"`); set `replier.fallbacks: false` to drop it.

## Verify before going live

- **`chat.db` fields** on this macOS version (`thread_originator_guid`, `attributedBody` decoding, chat styles)
  and that AppleScript sends to the group's chat ID work (spec open question).
- **`X-MS-Exchange-Organization-AuthAs`**: Graph sometimes omits it for internal mail; the bridge then rejects
  every result. Send a test `[MIA-POST]` early.
- Full Disk Access is granted to the Python binary, so any Python script run as the `mia` user can read that
  account's files. Keep the account dedicated to the bridge.

## Tests

`.venv/bin/python -m pytest` (57 tests) covers dashboard rules and auth, routing, quiet mode and quiet hours,
dedup, rate limit, `chat.db` parsing against a synthetic database, task handoff and result acceptance.
Acceptance tests 1, 4 and 10 still need the live group, keys and a Mac restart.
