# `web_search` and `read_url`

Two halves of one loop. `web_search` finds candidate URLs and returns snippets;
`read_url` deep-reads the promising ones as clean text. Neither renders
JavaScript.

The split exists to keep the context window intact. A search that returned full
pages would spend thousands of tokens on results the model discards after reading
one line, so stage one is deliberately snippet-only and stage two is explicitly
chosen.

`read_url` is registered unconditionally — it needs no provider and no key.
`web_search` needs a search backend, so it appears only when one is configured.

## `web_search`

Searches the live web and returns a ranked list of `title` / `url` / `snippet`.

Reach for it when a fact is volatile or specific enough that training data may be
stale — a CVE's affected versions, a current tool flag, whether a public PoC
exists. Not for stable methodology the model already knows.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | **required** | The search query. Must be non-empty. |
| `num_results` | integer | `5` | Results to return, clamped to [`SEARCH_MAX_RESULTS`](../configuration.md#web-search-web_search) (10). |
| `recency` | `day` \| `week` \| `month` \| `year` | — | Freshness filter, passed through where the backend supports it. |
| `site` | string | — | Restrict to one domain, e.g. `github.com`. |

### Backends

`web_search` sits behind a `SearchBackend` interface with two implementations,
selected by [`SEARCH_PROVIDER`](../configuration.md#web-search-web_search).

| Provider | Needs | Notes |
|---|---|---|
| `tavily` | An API key in `SEARCH_API_KEY`, the credential store, or entered when prompted | Commercial search API. In an interactive session a missing key is asked for on first use; a 401 or 403 forgets it so the next attempt can re-prompt. |
| `searxng` | `SEARCH_BASE_URL` pointing at an instance whose `search.formats` includes `json` | Self-hosted and keyless. `/searxng setup` provisions a working instance — a stock SearXNG answers `format=json` with 403, so the managed setup is the practical route. |

The tool is only registered when the selected provider can actually serve a
query. For Tavily that includes "an interactive client could ask for the key";
for SearXNG it strictly requires the base URL, since there is nothing to prompt
for. Running [`/searxng`](../slash-commands.md#tools) or `/auth` mid-session
rebuilds the search tools, so the tool can appear without a restart.

### Approval

Prompts by default — the packaged rules carry no allow rule for `web_search`,
because a search sends the query to a third party. Approval widens to **the whole
tool**: every call is a snippet-level search, so there is nothing below it worth
distinguishing, and `allow: "web_search({})"` is a reasonable line to add to
`~/.kerkov/approvals.yaml` if you would rather not be asked.

### Returns

A numbered list of results, three lines each (title, URL, snippet). Metadata
carries `provider`, `query`, and `results` — the same results as structured
records.

An empty result set is a success with `No results found for '<query>'.` A
provider timeout, HTTP error, or missing key is a failure with `provider` and
`query` in metadata.

### Examples

```json
{ "query": "CVE-2021-44228 affected log4j versions", "num_results": 5 }
```

```json
{ "query": "GitLab 16.0 authenticated RCE proof of concept", "site": "github.com" }
```

```json
{ "query": "opengrep release notes", "recency": "month" }
```

## `read_url`

Fetches an HTTP or HTTPS page and returns its readable text, extracted locally
with `trafilatura`. No markup, no navigation chrome, no scripts.

Every call re-fetches the page. There is no cache.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string | **required** | The page to read. |
| `max_chars` | integer | [`READ_MAX_CHARS`](../configuration.md#web-page-reads-read_url) (20000) | Output budget, truncation marker included. Can only request *less* than the configured maximum. |
| `offset` | integer | `0` | Character offset into the extracted text, for continuing a long page. |

A 10 MB download guard applies before extraction, so an unexpectedly large
response is refused rather than parsed.

### Paging

A page that fits the budget comes back whole. A longer one comes back as a head
and a tail with a marker between them:

```text
… [truncated 48213 of 61004 chars — call again with offset=15831] …
```

The head is four fifths of the content budget and the tail one fifth. **The tail
is a preview, not the end of the document** — it is the last characters of the
page, shown so the model can see where the text is going. Continuing from the
marker's `offset` (also in metadata as `next_offset`) resumes at the end of the
*head*, so nothing between them is skipped, and the tail may repeat across pages.

### Approval

Prompts by default. An approval saved from the prompt widens to **the origin** —
`origin + "/*"`, matching [`http`](http.md#approval) exactly and for the same
reason: `origin + "*"` would also match `https://host.attacker.example`.

`max_chars` and `offset` never reach a saved rule. A rule pinned to one offset
would never match the next page of the same document.

### Returns

The extracted text as output. Metadata carries `url`, `resolved_url` (the final
URL after redirects — check it when a read returns something unexpected), `title`,
`offset`, `next_offset`, `truncated`, `returned_length`, and `total_length`.

`read_url` output is **exempt from the knowledge base's clipping**: pages stay
inline under their own `READ_MAX_CHARS` budget rather than being replaced with an
`artifact#N` reference. A second round of trimming would hide part of the
requested head before `next_offset`, and continuing from the marker would then
skip text nobody ever saw. The pages are still indexed, so compaction can evict
them later and `recall` can bring them back.

### Examples

```json
{ "url": "https://nvd.nist.gov/vuln/detail/CVE-2021-44228" }
```

Continuing a long advisory:

```json
{ "url": "https://target.example/docs/api", "offset": 15831 }
```

Keeping a read small on purpose:

```json
{ "url": "https://target.example/robots.txt", "max_chars": 2000 }
```

## Related

- [`http`](http.md) — the raw-response tool. Use it for APIs and for any case
  where the markup, headers, or status code is the point.
- [`browser`](browser.md) — when the content only exists after JavaScript runs.
- [`cve_lookup`](cve-lookup.md) — for CVE facts, prefer the canonical feeds over
  searching for them.
- [`/searxng`](../slash-commands.md#tools) — provisioning the keyless backend.
