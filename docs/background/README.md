# Unpublished background material

Nothing in this directory is part of the documentation site. It is excluded
from the MkDocs build by `exclude_docs` in `mkdocs.yml`, so these pages are
neither navigable nor reachable by URL nor listed in the sitemap.

These are the six files the [issue #200](https://github.com/Kerkov-Security/kerkov/issues/200)
audit condemned. They are parked rather than deleted (open question 5, signed
off 2026-09-26) because a couple of them still carry usable structure for the
pages that replace them, and because reading the replacement next to the thing
it replaces is easier than reading it next to a `git show`.

**Do not cite them, and do not link to them from a published page.** Each was
condemned for being wrong about the code, not merely for being old.

| File | Why it was condemned | Replaced by |
|---|---|---|
| `PROJECT_OVERVIEW.md` | Describes a Gemini-only prototype; lists Anthropic and OpenAI as future work; opens with a general LLM primer; documents the removed `ShellTool`. | `concepts/architecture.md` + `concepts/conversation-loop.md` |
| `CLI_ARCHITECTURE.md` | Generic "how tools like Claude Code work" tutorial; never mentions Kerkov. | `concepts/conversation-loop.md` |
| `CONTEXT_WINDOW_MANAGEMENT.md` | Generic context-window tutorial priced against GPT-4; never mentions Kerkov. | `concepts/context-and-compaction.md` |
| `LLM_MESSAGE_FORMAT.md` | Generic message-format primer with no project content. | — |
| `TOOL_IMPLEMENTATION_EXAMPLE.md` | Walks through implementing a `BashTool` that does not exist in this codebase. | `contributing/writing-a-tool.md` |
| `DEV_ROADMAP.md` | Pre-implementation phase-and-weeks planning, superseded by the issue tracker. | — |

The replacements land in stages 4 and 5 of #200. Once each is written and
verified, the file it replaces can be deleted outright; git keeps the history
either way.
