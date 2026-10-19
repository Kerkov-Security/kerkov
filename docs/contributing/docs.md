# Working on the docs

This site is [MkDocs](https://www.mkdocs.org/) with the Material theme, built
from the Markdown under `docs/`. A change to the docs runs two gates: the strict
build and an accuracy lint.

## Serve it locally

```bash
uv sync --extra docs
uv run mkdocs serve
```

That serves the site at `http://127.0.0.1:8000/` with live reload — edit a page
and the browser refreshes. This is the fastest way to see your change rendered,
links resolved, and admonitions styled.

## The two gates

```bash
uv run python scripts/docs_lint.py --strict
uv run mkdocs build --strict
```

Run both before pushing. The CI docs workflow gates any PR that touches
`docs/**`, `mkdocs.yml`, `src/**`, or the lint script.

### `mkdocs build --strict`

`--strict` promotes warnings to errors, and the `validation:` block in
`mkdocs.yml` raises link problems to warnings (MkDocs logs them at INFO by
default, which strict alone would not catch). So a broken relative link or an
unresolvable heading anchor fails the build. Exit code `0` is the only signal
that matters.

Two lines look like failures but are not:

- Material prints a red **"MkDocs 2.0"** banner on every build.
- The `git-revision-date` plugin warns that an **uncommitted** file "has no git
  logs". It resolves once the file is committed.

What *does* fail is any `WARNING - Doc file ... contains a link ...` line, or a
link or anchor that cannot be resolved.

### `scripts/docs_lint.py`

The build proves pages render and link; the lint proves they match the code.
Every failure class the issue #200 audit found is introspectable from the
source, so the tables that describe tools, commands, and settings are asserted
against it rather than reviewed by eye. The checks:

| Check | Asserts |
|---|---|
| `source-paths` | Every `src/kerkov/…py` path cited on a published page exists |
| `denylist` | No removed symbol (the deleted shell stack) appears on a published page |
| `tool-pages` | Every tool the suite can build has a reference page (via `TOOL_PAGES`) |
| `slash-commands` | Every slash command and alias appears in `reference/slash-commands.md` |
| `config-settings` | Every `ConfigManager` field has a row in `reference/configuration.md` |
| `ledger-vocabulary` | Every `LedgerSection` value and `ledger` op appears in both the ledger reference and concept pages |

Without `--strict`, a check whose page does not exist yet reports **pending**
rather than failing; `--strict` treats pending as failure, which is the mode CI
runs. The lint has been strict-clean since the tool reference landed.

!!! danger "The lint checks existence, not truth"
    `config-settings` proves a row for `MAX_TIMEOUT_SECONDS` exists; it cannot
    prove the row describes it correctly. A page can name every tool and still
    describe them all wrongly. The lint bounds the rot — it does not replace
    reading the code. **Verify every behavioral claim against the code path, not
    against `AGENTS.md` or an older page.** When they disagree, the code wins.

### Two maps a contributor touches

- **`TOOL_PAGES`** in `scripts/docs_lint.py` maps each tool name to the
  reference page documenting it (several tools share a page). A new tool must be
  added here and given a page under `docs/reference/tools/`, or `tool-pages`
  fails. See [Writing a tool](writing-a-tool.md).
- **`exclude_docs`** in `mkdocs.yml` keeps unpublished files out of the build
  entirely — not merely out of the nav. MkDocs builds every file under `docs/`
  regardless of the nav, so an unlisted page would still ship as a live URL and
  appear in `sitemap.xml`. Today it excludes `background/` (parked material;
  see `docs/background/README.md`). Anchor any pattern you add with a leading
  `/` when you mean a top-level directory — a bare `tools/` would also match
  `reference/tools/` and silently drop the whole tool reference from the build.

## Audience and register

The nav is Diátaxis-shaped, and each section has a fixed register. Write a page
in the register of the section it lives in:

| Section | Audience | Register |
|---|---|---|
| Get started, Guides, Reference | Operators | How to *use* Kerkov |
| Security | Operators | Obligations, boundaries, and data handling |
| Concepts, Contributing | Contributors | How Kerkov is *built* and extended |

Contributor-facing pages (Concepts and Contributing) open with a
`!!! note "Contributor-facing"` admonition pointing a reader who only wants to
use Kerkov back to Get started. Keep that marker on new pages in those sections.

## Related

- [Development setup](development.md) — the environment, formatters, and type
  checker.
- [Writing a tool](writing-a-tool.md) — includes the `TOOL_PAGES` and reference-
  page requirement.
