# UNISI Wire Protocol

This is the protocol a UNISI server and its client speak over WebSocket — the
thing `config.web_client` (see README, "Custom web client") lets you replace
the bundled front end with your own implementation of. Every claim below was
checked against a live server (`unisi` 0.7.8) with a raw WebSocket client, not
just read off the source; the handful of places that are read-only (not
independently re-verified over the wire) say so explicitly.

## 1. Transport

```
GET ws://<host>:<port>/ws[?screen=<name>][&id=<session-id>]
```

- One WebSocket connection per browser tab/client. There is no separate
  request/response HTTP call for state changes — everything after the
  initial handshake goes over this one socket.
- `screen` (optional) — open directly on a named screen instead of the
  default (lowest `order`) one. An unknown name does not error; the server
  falls back to a generic "No screens" placeholder message (see 3.1).
- `id` (optional) — reattach to an existing session instead of starting a
  new one. Only takes effect when the server has `share = True` in
  `config.py` (see README, "Shared sessions"); this is the query-string
  equivalent of `Proxy(session=...)`.
- The very first thing the server sends, unprompted, is a full screen
  object (3.1) — there's no explicit "hello"/handshake message to wait for
  first.

## 2. Message Envelope

Every message in both directions is JSON. A single `send()` may also be a
JSON **array** of message objects, processed in order as a batch — the
client can coalesce several changes (e.g. multiple fields losing focus at
once) into one WebSocket frame.

The one non-JSON exception: sending the raw string `"close"` (not JSON,
just those 7 characters) closes the socket from the server side with a
normal 1000 close code. There is no equivalent server→client raw string.

## 3. Server → Client Messages

Every server→client message is a JSON object with a `"type"` field. The
types actually produced by the current server: `screen`, `update`, `get`,
`error`, `warning`, `info`. (`warning`/`info` share `error`'s exact shape
below, just with `"type"` swapped — confirmed from `unisi/common.py`, not
separately fired over the wire for this document.)

### 3.1 `screen` — full screen replace

Sent as the very first message on connect, and again any time the server
decides the whole screen needs replacing (navigating to a different screen,
or a handler returning `True`/`Redesign`).

```json
{
  "name": "Main", "type": "screen", "icon": null, "prepare": null,
  "header": null, "toolbar": [], "order": 0, "persist": false,
  "reload": false, "lang": "en-US", "voice": true, "image": null, "menu": [...],
  "blocks": [
    {"name": "B1", "type": "block", "value": [
      {"name": "Edit1", "value": "hello", "changed": true, "x": 0, "type": "string"},
      {"name": "Go", "value": null, "type": "command", "changed": true}
    ]}
  ]
}
```

- Every field a handler is set for (`changed`, `delete`, `append`, ...)
  serializes as the boolean `true`, never the function itself — the client
  only needs to know *that* an event is handled, not by what.
- `reload` is `false` on the very first connect, `true` on every screen
  object sent afterward as the result of an explicit screen switch (3.5).
- A `Table` that's DB-backed (constructed with `id=`) does not inline all
  its rows here if there are more than the page size — `rows` is instead
  `{"length": <total>, "limit": <page size>, "data": [...first page...]}`.
  Fetching further pages is the client's job — see 3.3.
- Connecting with `?screen=<unknown name>` does not error; the server sends
  a placeholder: `{"name": "", "blocks": [], "header": "No screens", "menu":
  [["You need to put at least 1 file in the 'screens' folder.",
  "exclamation"]], ...}` — the same fallback shown for a genuinely empty
  project, reused here for "can't resolve this screen name" too.

### 3.2 `update` — partial update

The normal response to a client message that changed something without
requiring a full screen replace:

```json
{"type": "update", "updates": [{"data": {...unit...}, "path": ["Edit1", "B1"]}]}
```

- `path` is the element's location from itself outward:
  `[unit_name, immediate_parent_block_name, ..., top_level_block_name]`.
- `updates` lists **side-effect** changes only — units other than the one
  the incoming message targeted that also changed as a result (UNISI's
  automatic change tracking, see the framework's own gotchas doc). The
  triggering unit itself is not echoed back: the client already has
  whatever it optimistically set locally, and the server only needs to
  correct or extend that.
- If nothing else changed, the server still sends a response — literal
  JSON `null`, not silence and not an empty object. A client waiting on
  exactly one reply per sent message can rely on this.
- If `path` can't be resolved against the *client's own current* screen
  (stale reference, or the element genuinely isn't there), that one update
  is silently dropped rather than erroring — each session only ever sees
  its own screen tree, so there's no cross-session leakage to guard
  against on the client side either.

### 3.3 `get` / lazy table paging

A DB-backed `Table`'s later pages are fetched with a `get` client message
(4.3) and answered with:

```json
{"type": "get", "updates": [], "value": {
  "update": "updates", "index": 0,
  "data": [["item0", 0, 1], ["item1", 1, 2], ...],
  "message": {"path": ["Items", "B1"], "event": "get", "value": 0}
}}
```

`value.index` is the requested chunk's start offset, `value.data` its rows
(as plain arrays, in field-declaration order, not objects). Requesting `get`
on a table that isn't DB-backed (no `id=`) — or any other unit that doesn't
declare a `get` handler — answers normally with an `error` (3.4):
`"<Name>@<Block> doesn't contain 'get' method type!"`.

> **A non-integer `get` value (`null` included) does not crash the
> connection** — `get_chunk` validates its input and rejects anything that
> isn't a real `int` (a `bool` counts as not-an-int here too) before it
> reaches the chunk-lookup math. The rejection is delivered a level deeper
> than a normal `error` message, though — nested under the `get`
> envelope's own `value`, not as a top-level `{"type": "error", ...}`:
> ```json
> {"type": "get", "updates": [], "value": {
>   "type": "error", "updates": [...], "value": "get requires an integer row index, got None"
> }, "message": {"path": [...], "event": "get", "value": null}}
> ```
> A client that only pattern-matches on a top-level `"type": "error"` will
> miss this — check `value.type` too when handling a `get` response.

### 3.4 `error` / `warning` / `info` — status messages

```json
{"type": "error", "updates": [{"data": {...rolled-back unit...}, "path": [...]}], "value": "rejected"}
```

Produced by a handler returning `Error(text, *units)` / `Warning(text,
*units)` / `Info(text, *units)`, or automatically for a message the server
can't process at all (unknown path, unknown event). `value` is always a
plain string. `updates` follows the same shape as 3.2 and, for a rejected
edit specifically, is how the client learns to roll the field back to its
pre-edit value — the unit comes back with its *old* value, since the
handler never accepted the new one.

### 3.5 Screen switch

Handled through the normal client→server envelope (4.1), not a distinct
message type — see 4.3. The server's reply, on success, is a full `screen`
message (3.1) with `"reload": true`.

## 4. Client → Server Messages

### 4.1 General shape

```json
{"path": ["Edit1", "B1"], "event": "changed", "value": "new text"}
```

- `path` — same convention as an update's `path` (3.2): the target
  element's name, then each enclosing block's name, outward.
- `event` — the handler name to invoke: `"changed"` covers the vast
  majority of interactions (any value edit, click, selection); `Table`
  units also accept `"get"` (3.3), `"append"`, `"delete"`, and others a
  given table was constructed with hooks for.
- `value` — whatever that event expects: the new field value for
  `"changed"`, a row index for `"get"`, etc.
- **`event` defaults to accepting the value directly if the target unit has
  no explicit handler for it** — `unit.value = value`, no error. A handler
  only needs to exist for events you want to intercept, validate, or react
  to.
- All path resolution is against *this session's own* current screen only
  — the same isolation the rest of the framework relies on (each user gets
  their own screen module instance). There's no way to address another
  session's tree even by path.

### 4.2 Batching

Send a JSON array instead of a single object to submit several messages in
one frame; each is processed in order, same as if sent as separate frames.

### 4.3 Special paths

| `path` | Meaning |
|---|---|
| `["root"]` | Switch screens. `value` is the target screen's name. Answered with a full `screen` message, `reload: true` (3.5). |
| `["voice"]` | Talk to the voice ("Mate") subsystem instead of a screen element — see §5. |

### 4.4 Closing the connection

The raw string `"close"` (see §2) closes the socket. There is no graceful
"goodbye" message expected back — the server just closes with code 1000.

## 5. Voice / Mate Sub-Protocol

When voice control is active (`screen.voice`, on by default — see README,
"Voice interaction" — off automatically when `config.mirror` is set), every
screen carrying it gets an extra synthetic block (labelled `"Mate:"` in the
served screen tree) holding four live-updated units: **System message**,
**Recognized words**, **Elements**, **Commands**. These update the same way
any other unit does (3.2) — a voice interaction is not a special
client-visible shape, just messages addressed to `path: ["voice"]`:

```json
{"path": ["voice"], "event": "listen", "value": true}
```
Toggling `listen` on returns a full `screen` message (3.1) — the Mate block
is being added to what the client renders, which is enough of a structural
change to warrant a full replace rather than a partial `update`.

```json
{"path": ["voice"], "event": "word", "value": "root"}
```
A recognized word/command is delivered as its own event; the response is a
normal `update` (3.2) touching the four Mate units. `Commands` is populated
with the exact current global-escape vocabulary (`cancel`, `choose`,
`menu`, `ok`, `okay`, `reset`, `root`, `screen`, `select`, `set`, `stop`) —
see the framework's own voice-subsystem doc for what each mode recognizes
beyond those.

## 6. Quick Reference

| You send | You get back |
|---|---|
| `{"path":[name,...],"event":"changed","value":v}`, handler accepts | `null`, or `update` with any *other* units that changed as a side effect |
| same, handler rejects (`Error`/`Warning`/`Info`) | matching `type`, `updates` containing the rolled-back unit |
| `path` doesn't resolve on this session's screen | `error`, `updates: []` |
| `{"path":["root"],"event":"changed","value":"ScreenName"}` | full `screen`, `reload: true` |
| `{"path":[name,"B"],"event":"get","value":<int>}` on a DB-backed table | `get`, `value.data` = that page's rows |
| same with a non-int `value` (`null` included) | `get` envelope whose `value` is itself an `error` message — not a top-level `error` |
| `{"path":["voice"],"event":"listen","value":true}` | full `screen` (Mate block added) |
| `{"path":["voice"],"event":"word","value":"..."}` | `update` (Mate units refreshed) |
| raw string `"close"` | socket closes, code 1000 |
| any handler returns `True` or `Redesign` | full `screen`, `reload: true` |

## 7. Building a Custom Client

None of the above changes when a custom front end is swapped in via
`config.web_client` (README, "Custom web client") — `web_client` only picks
which static files answer `GET /`; whatever you serve there needs to speak
exactly this protocol against `/ws` to actually work as a UNISI client.
`/default` always keeps serving the bundled reference client regardless, so
it stays available to compare wire traffic against a known-correct
implementation.
