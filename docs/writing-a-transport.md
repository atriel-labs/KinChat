# Writing a chat transport

A transport connects kinchat to one chat network. The bridge only ever talks to transports through the
`ChatTransport` interface in `kinchat/backends/base.py`, so adding a network means writing one class and
registering it; the router, outbound rules, dashboard and task backends don't change.

The in-memory transport in `kinchat/backends/fake.py` is the worked example below. It is also what the
end-to-end tests drive (`tests/test_transports.py`).

## Chat IDs

Every chat ID kinchat stores is namespaced `<transport>:<native id>`: `imessage:iMessage;+;chat123`,
`fake:family`. Pick a short lowercase name for your transport. It is the prefix, and the
`TransportRegistry` uses it to send each reply to the right transport. Use `make_id(name, native)` and
`split_id(chat_id)` rather than building or parsing IDs by hand.

## The interface

```python
class ChatTransport(Protocol):
    name: str          # the chat ID prefix
    health: Health     # shown in the dashboard's Health section

    def poll(self) -> list[Incoming]: ...
    def ack(self, inc: Incoming) -> None: ...
    def send(self, chat_id: str, text: str) -> None: ...
    def participants(self, chat_id: str) -> list[str]: ...
    def is_group(self, chat_id: str) -> bool: ...
```

### `poll()` and `ack()`

`poll()` returns new messages, oldest first, as `Incoming` objects. Every message the bridge handles is
then passed to `ack()`, and only after that may the transport stop returning it. The transport owns its
bookmark: keep it in memory and persist it (the iMessage transport stores the last chat.db ROWID in the
store's `kv` table) so a restart resumes where it stopped. If the bridge crashes between `poll()` and
`ack()`, the message comes back; the store deduplicates by message GUID, so that's safe.

```python
def poll(self) -> list[Incoming]:
    return [m for m in self._inbox if m.rowid > self._acked]

def ack(self, inc: Incoming) -> None:
    self._acked = max(self._acked, inc.rowid)
```

`poll()` may raise. The bridge logs the error, sets nothing else, and calls again after a pause, so put
connection errors in `self.health` (`health.degraded(e)`) and re-raise.

Fill in `Incoming` like this:

| Field | Meaning |
|---|---|
| `rowid` | Your position marker for the message (any increasing int) |
| `guid` | A message ID unique across your network; used for dedup and inline replies |
| `chat_guid` | The namespaced chat ID |
| `is_group` | Whether the chat is a group (groups need approval in the dashboard) |
| `chat_name` | Display name, or None |
| `handle` | The sender's address (phone number or email), or None for your own messages |
| `text` | Plain text; empty for attachments-only messages |
| `is_from_me` | True for messages the assistant itself sent (they come back through the network) |
| `ts` | Unix time the message was sent |
| `thread_originator_guid` | The message this one replies to inline, if your network has threads |
| `is_tapback` | Reactions and similar non-text events |
| `transport` | Your transport's name |

Handles are compared with `norm_handle`, so return phone numbers as the network gives them (`+1 800...`
or national format), not pre-normalised.

### `send()`

Post `text` to the chat and raise on failure. The bridge has already checked the dashboard rules, quiet
hours, rate limit and shadow mode, so don't repeat them here. If your network echoes sent messages back
(as Messages does), return the echo from `poll()` with `is_from_me=True`; the router stores it as the
assistant's own message, which inline replies and follow-ups rely on.

```python
def send(self, chat_id: str, text: str) -> None:
    if self.fail_sends:
        raise RuntimeError("fake send failure")
    self.sent.append((chat_id, text))
    self.deliver(split_id(chat_id)[1], None, text, from_me=True)  # the echo
```

### `participants()` and `is_group()`

`participants()` lists the handles of a group's other members. The router compares them with approved
contacts and sends a group back to Pending when a stranger joins, so it must be accurate and current.
`is_group()` must agree with `Incoming.is_group`; `Settings.can_speak_in` uses it as the final check
before anything is posted.

## Registering it

Add a branch to `Bridge._make_transports` in `kinchat/app.py` and the name to the known transports in
`config.load` (`kinchat/config.py`), then list it in `config.yaml`:

```yaml
chat:
  transports: [imessage, yournetwork]
```

Import your module inside the branch so its dependencies load only when the transport is selected, and put
any credentials in the Keychain (`secrets.get("your-account")`), never in `config.yaml`.

## Testing

Copy the pattern in `tests/test_transports.py::test_bridge_end_to_end_with_fake_transport`: build a
`Bridge` with your transport, approve a contact and a group in `bridge.settings`, deliver a message, call
`bridge.poll_once(transport)`, and assert what was sent. Use fictional names, `example.com` addresses
and 555-01xx numbers only. The repository is public, and CI checks every commit for personal data.
