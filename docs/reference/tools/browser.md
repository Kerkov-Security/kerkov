# `browser`

Drives a real Chromium browser through Playwright. This is the tool for anything
that only exists after JavaScript runs — a single-page app, a login flow that
posts from script, a token written into `localStorage`.

The tool is available in the CLI and [web console](../../guides/web-console.md).
The console's **Browser** tab shows page previews and the latest agent snapshot,
and provides operator navigation, clicking, scrolling, typing, and key controls.
Web sessions run headless, with viewports capped at 1920 × 1080. Each WebSocket
owns its browser sessions; its agents share them. Switching engagements or
disconnecting closes those sessions. Reopening a saved engagement does not
restore live browser sessions. The operator can explicitly open a named persistent
profile from the Browser tab to reuse saved cookies and site storage across
reconnects. Tool-created sessions remain fresh. While the operator controls a
page, tool actions that change or close it are rejected until control is returned
to Kerkov; inspection is still available. Sessions normally expire 60 minutes
after launch, checked on tool calls. The page under operator control is exempt
until released; an already expired page can close on the next tool call.
Snapshot element labels omit password input values. See the
[session mode guide](../../guides/web-console.md#controlling-the-browser).

!!! note "Chromium is a separate install"
    Playwright ships the client, not the browser. Install Chromium once:

    ```bash
    uv run python -m playwright install chromium
    ```

## Parameters

| Parameter | Type | Default | Applies to |
|---|---|---|---|
| `action` | see below | **required** | — |
| `session_id` | string | — | Every action except `start` and `list`. On `start`, a name for a predictable session id. |
| `url` | string | — | `start`, `navigate`; the source URL for `sources` |
| `headless` | boolean | `true` | `start` |
| `viewport_width` | integer | `1280` | `start` |
| `viewport_height` | integer | `720` | `start` |
| `wait_until` | `load` \| `domcontentloaded` \| `networkidle` \| `commit` | `domcontentloaded` | `start`, `navigate` |
| `timeout` | number | `30` | Any action, in seconds |
| `ref` | string | — | `click`, `type`, `screenshot`. A ref from a snapshot. |
| `selector` | string | — | `click`, `type`, `screenshot`. A CSS selector; prefer `ref`. |
| `text` | string | — | `type` |
| `clear_first` | boolean | `true` | `type`. Replace the field's contents; `false` clicks and types into the focused element. |
| `keys` | string | — | `key`. E.g. `Enter`, `Tab`, `Control+A`. |
| `kind` | `all` \| `localStorage` \| `sessionStorage` \| `cookies` | `all` | `storage` |
| `include_values` | boolean | `true` | `storage`. `false` lists keys only. |
| `limit` | integer | `50` | `sources`, `network` |
| `full_page` | boolean | `false` | `screenshot` |
| `path` | string | `<workspace>/browser/screenshots` | `screenshot`. Confined to the screenshot root. |
| `script` | string | — | `evaluate` |
| `all_sessions` | boolean | `false` | `close` |

## Actions

| Action | What it does |
|---|---|
| `start` | Launch a browser session, optionally navigating straight to `url`. |
| `list` | Every open session. |
| `navigate` | Go to a URL in an existing session. |
| `snapshot` | A compact accessibility-style view of the page, with element refs. |
| `click` | Click an element by ref or selector. |
| `type` | Type text into a field. |
| `key` | Press a key or combination. |
| `storage` | Read `localStorage`, `sessionStorage`, and cookies. |
| `sources` | List the JS and CSS the page loaded, or fetch one by URL. |
| `network` | Recent network requests. |
| `screenshot` | Capture the page, an element, or the full scroll height. |
| `evaluate` | Run JavaScript in the page context. |
| `close` | Close one session, or all of them. |

### Snapshot first

`snapshot` is the action that makes the rest usable. It returns a compressed
accessibility tree with stable refs like `[ref=e1]`, and `click`, `type` and
`screenshot` accept those refs directly. Guessing a CSS selector against a page
nobody has looked at is how a browser session wastes ten turns.

Refs from a snapshot, legacy refs such as `button:1`, and plain CSS selectors are
all accepted, in that order of preference.

## Approval

An approval saved from a `browser` prompt widens to **the action, not the
target**. A URL here is an argument to a navigation, not a boundary: granting
"navigate" is granting navigation, and pinning it to one origin would be a
promise the tool cannot keep once the page redirects.

No browser action is auto-approved by the packaged rules.

!!! warning "`evaluate` runs arbitrary JavaScript"
    `evaluate` executes whatever script it is given in the live page context,
    with the session's cookies and storage. Its results carry
    `risk: "arbitrary_page_javascript"` in metadata. In `permissive` or `yolo`
    approval modes nothing will stop it, so treat those modes as a decision about
    this action in particular.

## Returns

JSON as output for the structured actions (`snapshot`, `storage`, `sources`,
`network`, `list`), a confirmation for the rest. Metadata carries `session_id`
throughout, plus `risk` on `evaluate` and `recoverable` on a failure that leaves
the session usable.

`sources` output is capped before it reaches the model. The `screenshot` action
writes to disk and returns a path. Separately, the console's Browser tab receives
bounded, in-memory viewport previews over its authenticated WebSocket; these
previews are not added to the model's conversation.

Screenshot paths are confined: a relative `path` resolves under
`<workspace>/browser/screenshots`, and a path that would escape that root is
rejected. With no `path`, the file is named for the session and a timestamp.

## Examples

Start a session and look at the page:

```json
{ "action": "start", "url": "http://10.10.10.5/login", "session_id": "target" }
```
```json
{ "action": "snapshot", "session_id": "target" }
```

Fill in a form using refs from that snapshot:

```json
{ "action": "type", "session_id": "target", "ref": "e4", "text": "admin" }
```
```json
{ "action": "type", "session_id": "target", "ref": "e5", "text": "admin" }
```
```json
{ "action": "click", "session_id": "target", "ref": "e6" }
```

Read the session token the app just stored:

```json
{ "action": "storage", "session_id": "target", "kind": "localStorage" }
```

Find the client-side code worth reading:

```json
{ "action": "sources", "session_id": "target", "limit": 50 }
```

Capture evidence:

```json
{ "action": "screenshot", "session_id": "target", "full_page": true }
```

Close up:

```json
{ "action": "close", "all_sessions": true }
```

## Related

- [`read_url`](web-search.md#read_url) — far cheaper for a page whose content is
  in the HTML. Reach for `browser` when it is not.
- [`http`](http.md) — once `browser` has shown you the request, replaying it with
  `http` is usually faster than driving the UI again.
- [`zap`](zap.md) — for systematic scanning rather than targeted interaction.
