# KinChat
A Claude-powered assistant for your family’s iMessage group chat. Answers in seconds, hands real tasks to an agent, and runs on a Mac you own with no incoming connections.

> **Status:** being prepared for open source. The assistant is called Mia by default; set `assistant.name`,
> `aliases`, `tag` and `handles` in `config.yaml` to give it another name (see `config.example.yaml`).

Puts Mia in the family iMessage group. The bridge reads new messages straight from the Messages
database (`chat.db`), routes each one (Claude Haiku), replies instantly (Claude Sonnet) by scripting
Messages, hands real work to Mia in Duet by `[MIA-CHAT]` email, and posts `[MIA-POST]` results back.
A password-protected local dashboard controls who Mia talks to. Nothing listens beyond 127.0.0.1.

```
kinchat/
  backends/     imessage.py (chat.db + AppleScript), fake.py router.py    dashboard rules → filters → controls → cues → window → classifier
  settings.py   features, contacts, chats, audit           outbound.py  can-speak check, quiet hours outbox, 6/10 min rate limit, shadow mode
  dashboard.py  Flask + plain HTML forms on 127.0.0.1:8770 tasks.py     [MIA-CHAT] handoff, [MIA-POST] acceptance rules
  llm.py        classifier + replier (structured output)   graph.py     app-only cert auth, inbox delta, mail, calendar snapshot
  app.py        chat.db watcher, outbox ticker, inbox poll, calendar refresh, hourly heartbeat, 30-day prune
```

## Setup

Install with `pip install -e '.[graph,ics]'`: `[graph]` (msal) is needed for Microsoft 365 task mail or calendar,
the default, and `[ics]` expands recurring events in ICS calendars. `scripts/install.sh` installs both. New chat networks: see `docs/writing-a-transport.md`.

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
keeps working. `scripts/install.sh` stops the old `ai.atriel.mia-bridge` launchd agent, moves its plist aside to
`ai.atriel.mia-bridge.plist.disabled` and loads `io.kinchat.bridge`. To roll back in one command:
`launchctl bootout gui/$(id -u)/io.kinchat.bridge; mv ~/Library/LaunchAgents/ai.atriel.mia-bridge.plist.disabled ~/Library/LaunchAgents/ai.atriel.mia-bridge.plist; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.atriel.mia-bridge.plist`

## Dashboard rules

- Senders who aren't approved contacts are ignored and logged, including inside an approved group.
- A group is engaged only when approved; new groups appear as Pending. Modes: Respond, Listen only, Off.
- One-on-one chats are answered only when the toggle is on and that contact has one-on-one allowed.
- Tasks are created only when Task handoff is on and the contact can assign tasks.
- Every change goes to the audit list. Settings are re-read on every message, so changes apply at once.
- Login is a password from the Keychain; forms carry a CSRF token and only `127.0.0.1`/`localhost` Host headers are served.

## Behaviour notes (choices the spec left open)

- **Quiet hours (default 10 PM–7 AM, `quiet_hours` in `config.yaml`):** outgoing replies and results are held in
  `state.db` and sent combined when they end. Times use `timezone`, which defaults to the Mac's own zone.
  A message counts as urgent, and is answered right away, if it contains "urgent", "asap" or "emergency".
  Tasks are still handed to Duet overnight.
- **Instant replies off:** Mia still runs the replier to detect tasks, then posts only "On it, I'll let you know."
- **Heartbeat:** an hourly `[MIA-HEARTBEAT]` email from the assistant's mailbox to itself with last `chat.db` read,
  last inbox poll and open-task count. Add to Duet's standing instruction: *"If no [MIA-HEARTBEAT] email has
  arrived in the last 2 hours, tell the owner the iMessage bridge may be down. Never reply to heartbeat emails."*
- **Controls** ("sit this one out" / "you're back") get a fixed one-line ack. **Closing phrases** close the window, no reply.
- **Phone numbers** without a `+` are read as national numbers in `default_country` (US by default; also CA, GB,
  AU, NZ, DE), so `800-555-0100` and `+1 800 555 0100` match, but a foreign number ending in the same digits does not.
- **Health:** each part (chat.db watcher, Messages sending, Claude API, Graph task mail, calendar, dashboard
  password) starts on its own. A missing secret, Graph config or permission turns that part off and shows as
  *failed* in the dashboard's Health section (and in the heartbeat); a later error shows as *degraded* while it
  retries. Without a dashboard password the dashboard still starts, but sign-in is disabled until one is added.
  At startup the bridge asks Messages for its chat count once, so the Automation prompt appears.
- **Chat transports:** chat IDs are namespaced `<transport>:<native id>` (e.g. `imessage:iMessage;+;chat123`), and
  replies go out through the transport named by the prefix. `chat.transports` picks the transports (default
  `[imessage]`). An existing `state.db` is upgraded in place by prefixing its stored IDs with `imessage:`.
- **Task mail over IMAP:** with `tasks.mail: imap` the bridge sends task mail by SMTP and reads `[MIA-POST]` results
  only from the account's Sent folder (forged mail lands in the Inbox, never read). Each result is posted once per
  `(UIDVALIDITY, UID)`; `Authentication-Results` is logged, never trusted. The app password is the Keychain
  account `imap-app-password`.
- **Managed Agents tasks:** with `tasks.backend: managed_agents`, `kinchat agent setup` creates (or updates in place)
  one agent and environment and records their IDs in `config.yaml`. Each task runs in its own session with a
  spending cap (`managed_agents.budget_usd`) and the configured vaults; updates and answers go to the same
  session. The agent can only reach the chat through its `post_to_chat` tool, which the bridge checks (task open,
  right session), posts through the usual dashboard rules and quiet hours, and always answers at once with what
  happened. A session that hits its cap pauses; the chat is told once and the next update starts a fresh
  attempt. Tool calls that would need approval are denied (nobody watches these sessions live). Finished
  sessions are archived after 24 hours.
- **Calendar sources:** `calendar.source` is `graph` (default), `ics` or `none`. ICS feed URLs are credentials, so
  they live in the Keychain account `ics-urls` (one per line) and are never logged. Invites that any
  `calendar.owner_emails` attendee hasn't accepted or has declined are left out, as with Graph, for every
  occurrence. With the `[ics]` extra, recurring events are expanded (RRULE, RDATE, EXDATE, moved instances);
  without it they aren't, and calendar health shows *degraded* until the extra is installed.
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

`.venv/bin/python -m pytest` (90 tests) covers dashboard rules and auth, routing, quiet mode and quiet hours,
dedup, rate limit, `chat.db` parsing against a synthetic database, task handoff and result acceptance.
Acceptance tests 1, 4 and 10 still need the live group, keys and a Mac restart.

**Personal-data checks.** CI also runs a `scrub` job: [gitleaks](https://github.com/gitleaks/gitleaks) (a pinned,
checksum-verified release) over the full git history, and `scripts/scrub-denylist.sh`, which checks the tree and
every commit message against a private list of terms kept in the `SCRUB_DENYLIST` repository secret. A hit fails
the job and reports only the file and line or the commit hash, never the term or the matching text. Without the
secret (for example on pull requests from forks) the denylist step is skipped with a warning.
