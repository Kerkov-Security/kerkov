# `http`

Sends an HTTP or HTTPS request and returns the response body as text, with the
status code and every header in metadata. This is the tool for APIs, probes, and
anything where the response *itself* is the point — the code, the headers, the
unextracted body.

For reading a web *page*, use [`read_url`](web-search.md#read_url) instead. It
extracts readable text and bounds the output; `http` hands back the markup.

`http` is not a byte-level client. The body is decoded to `str` before it is
returned, so it is the wrong tool for binary responses — see
[Returns](#returns).

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `method` | `GET` \| `POST` \| `PUT` \| `PATCH` \| `DELETE` | **required** | HTTP method. |
| `url` | string | — | Full URL, for ad-hoc requests. Mutually exclusive with `profile`. |
| `profile` | string | — | Named credential profile. Supplies base URL, default headers, and timeout. Pair with `path`. |
| `path` | string | — | Path joined onto the profile's `base_url`. Required with `profile`. |
| `headers` | object | — | Extra headers, merged on top of the profile's. |
| `params` | object | — | Query-string parameters. |
| `json` | any | — | JSON body; sets `Content-Type: application/json`. Mutually exclusive with `data`. |
| `data` | string | — | Raw text body. Mutually exclusive with `json`. |
| `timeout` | number | profile's, then [`MAX_TIMEOUT_SECONDS`](../configuration.md#tool-execution) | Per-request timeout in seconds. |

Responses over 10 MB of decoded text are refused, with the length in metadata,
rather than truncated.

## Credential profiles

A profile bundles a base URL, default headers, and a timeout under a name, so an
authenticated API can be called without the token ever appearing in a tool
argument. Profiles are loaded from
[`HTTP_PROFILES_PATH`](../configuration.md#tool-execution)
(`config/http_profiles.yaml` by default); a missing file is fine — ad-hoc `url`
requests still work.

```yaml
profiles:
  htb:
    base_url: https://labs.hackthebox.com
    headers:
      Authorization: "Bearer ${HTB_API_TOKEN}"
      Accept: "application/json"
    timeout_seconds: 60
```

`${VAR}` references resolve against the environment at load time, then against
the credential store. In an interactive session a still-missing value is asked
for with masked input the first time the profile is used, and can be saved —
see [`/auth`](../slash-commands.md#auth). Resolved headers are held
privately and redacted in logs and in the object's `repr`, so a `Bearer` token
does not reach a transcript through this path.

## Approval

Prompts by default. An approval saved from the prompt widens to **the origin and
the method** — trusting a site is trusting its pages, but not its other verbs, so
a saved `GET` grant does not cover `DELETE`. With a profile, the profile name
takes the origin's place.

The pattern is `origin + "/*"`, never `origin + "*"`. The second form would also
match `https://host.attacker.example`, so the boundary is drawn at the path
separator. A request for the bare origin with no path therefore falls outside the
rule and prompts again — the safe direction to fail.

Three parameter groups never reach a saved rule: `timeout` (varies per call),
`headers`, and the `json` and `data` bodies. A rule pinned to one request body
would never match a second time, and writing a login body into `approvals.yaml`
would store the credential in cleartext. A URL carrying userinfo credentials is
still the identity of the request, so it is kept in the exact rule — with the
credentials stripped from the authority.

A malformed URL may be approved once for validation but is never saveable.

## Returns

The response body as **decoded text**, unextracted and unreformatted. Everything
about the response other than its body is in metadata: `url`, `status`,
`content_type`, `content_length` (the length of that decoded text), and the full
`headers` dictionary.

Decoding happens before the body reaches you: the tool reads `httpx`'s
`response.text`, which applies the charset from `Content-Type`, falls back to UTF-8
when the header names none or names one it does not know, and replaces every byte
that will not decode. Bytes are therefore **not preserved** — a body of `ff fe 41`
served without a charset comes back as `��A`, and the original bytes cannot be
recovered from the result. For binary payloads — an image, an
archive, a core dump — fetch with `exec` (`curl -o`) and work with the file on
disk.

When the response is JSON, metadata also carries `parsed` — the body decoded into
a structure. This is convenient and it is also why a large JSON response counts
twice toward the
[clipping threshold](../configuration.md#knowledge-base-and-findings-ledger):
the footprint measured is output *plus* metadata, so both copies are considered
before an `artifact#N` reference is added.

`success` follows the status code: `2xx` succeeds, everything else fails with
`error` set to `HTTP <code>: <reason>` and the body and metadata still attached.
A 401 is a result, not a malfunction — the body is there to read.

## Examples

An authenticated API call through a profile:

```json
{ "method": "GET", "profile": "htb", "path": "/api/v4/machine/active" }
```

An ad-hoc probe:

```json
{ "method": "GET", "url": "https://10.10.10.5:8443/actuator/env", "timeout": 10 }
```

A JSON POST:

```json
{
  "method": "POST",
  "url": "https://target.example/api/login",
  "json": { "username": "admin", "password": "admin" }
}
```

A raw body, with an explicit content type:

```json
{
  "method": "POST",
  "url": "https://target.example/search",
  "headers": { "Content-Type": "application/x-www-form-urlencoded" },
  "data": "q=%27+OR+1%3D1--"
}
```

## Related

- [`read_url`](web-search.md#read_url) — the same protocol, but extracting
  readable text and bounding the output. Use it for documentation and advisories.
- [`browser`](browser.md) — when the page needs JavaScript. `http` does not
  execute any.
- [`zap`](zap.md) — for systematic web-application scanning rather than
  individual requests.
- [`/auth`](../slash-commands.md#auth) — storing the tokens profiles
  reference.
