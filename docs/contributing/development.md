# Development setup

!!! note "Contributor-facing"
    This section is for people changing Kerkov's code. To *use* Kerkov, start
    with [Get started](../get-started/install.md).

Kerkov targets Python 3.10 through 3.14 and uses [uv](https://docs.astral.sh/uv/)
for everything — dependencies, the lock file, and running commands.

## Get the environment

```bash
git clone https://github.com/Kerkov-Security/kerkov.git
cd kerkov
uv sync --locked --extra dev --extra test
```

`--extra dev` brings in the formatters, linters, and type checker; `--extra test`
brings in the test runner. Add `--extra web` if you are working on the web
console, and `--extra docs` for the documentation site (see
[Working on the docs](docs.md)). `--locked` installs exactly what `uv.lock`
pins; when you change a dependency, edit `pyproject.toml`, run `uv lock`, and
re-run the locked sync.

Run anything in the environment with `uv run`:

```bash
uv run kerkov
uv run python -m pytest tests
```

The second line is the test command CI runs. [Testing](testing.md) covers
running a single file and the web tests.

## The checks CI runs

The `Unit Tests` workflow gates every pull request on two commands:

| Check | Command | What it enforces |
|---|---|---|
| Blank lines | `uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests` | The PEP 8 blank-line rules, nothing else |
| Tests | `uv run python -m pytest tests` | The whole suite, with the configured `addopts` |

A change under `docs/`, `mkdocs.yml`, or `src/` also runs the two docs gates
described in [Working on the docs](docs.md).

## Local checks

Four more tools are configured in `pyproject.toml`, but no workflow runs them, so
a change that fails them still merges. Run them before you push anyway, and run
the formatters first because they rewrite files.

| Check | Command | What it enforces |
|---|---|---|
| Format | `uv run black src/ tests/` | Black, line length **88**, target 3.10 |
| Imports | `uv run isort src/ tests/` | isort, **black profile**, line length 88 |
| Types | `uv run mypy src/` | The strict flag family (see below) |
| Security lint | `uv run bandit -c pyproject.toml -r src/` | Common insecure patterns |

Black and isort are configured in `pyproject.toml` to agree with each other, so
running both in either order converges. Bandit reads `[tool.bandit]` only when
given `-c pyproject.toml`; with it, Bandit skips `B101` (its `assert_used` check)
and excludes `tests/`.

Because nothing enforces mypy or Bandit, neither is clean on `main`: both report
existing findings. Compare against a run on `main` and make sure your change adds
none, rather than expecting zero output.

### Type checking is strict

`[tool.mypy]` does not set the single `strict = true` switch, but it turns on the
flags that make up strict mode — `disallow_untyped_defs`,
`disallow_incomplete_defs`, `disallow_untyped_decorators`, `no_implicit_optional`,
`warn_return_any`, `warn_redundant_casts`, `warn_unused_ignores`, `warn_no_return`,
`warn_unreachable`, and `strict_equality`. In practice: every function needs type
hints, and nothing untyped slips through.

!!! warning "Use `Optional[T]`, not `T | None`"
    `python_version` is pinned to `3.10`. Write `Optional[T]` and
    `from typing import Optional`, not the `T | None` shorthand, so the code type-checks
    and runs on the oldest supported interpreter. This is the one type-hint rule
    worth memorizing — it is easy to get wrong and mypy will not always flag it on
    a newer local interpreter.

## Code style beyond the formatters

Black owns whitespace and line wrapping, but two conventions are on you:

- **Blank lines (PEP 8).** Two blank lines between top-level definitions, one
  between methods inside a class. Black enforces this; `flake8 --select=E301,E302,E303,E305,E306 src tests`
  is the narrow check CI uses for it.
- **Comments are sparse.** The code is expected to explain itself. Add a `#`
  comment only where something is genuinely non-obvious — a workaround, a
  non-local constraint, a subtle ordering requirement, a "why this and not the
  obvious thing". Never restate what the next line already says. If a comment
  feels necessary to make code followable, prefer a clearer name or a smaller
  function.
- **Docstrings are one line.** A single summary line is the default for modules,
  classes, and functions. Expand past that only when a caller genuinely cannot
  use the thing correctly without it — a non-obvious contract or a surprising
  failure mode — and then add the minimum, not a full Args/Returns treatment that
  duplicates the type hints.

## Conventions that shape the code

- **Async everywhere.** Every provider `complete()` and every tool `run()` is
  `async`. Use `asyncio`, not threads, for concurrency.
- **Abstract base classes.** Providers extend
  [`BaseLLMProvider`](writing-a-provider.md); tools extend
  [`BaseTool`](writing-a-tool.md).
- **Pydantic 2** for data models, tool parameters (`{ToolName}Params`), and
  configuration.

!!! note "`anthrophic_provider.py` is a known typo"
    The Anthropic provider lives at `src/kerkov/providers/anthrophic_provider.py`.
    The misspelling is deliberate for now — do not rename the file without
    coordinating, because imports and tooling reference it by that path.

## Related

- [Testing](testing.md) — how the test suite is laid out and run.
- [Writing a tool](writing-a-tool.md) and [Writing a provider](writing-a-provider.md)
  — the two extension points.
- [Working on the docs](docs.md) — the site build and its accuracy lint.
