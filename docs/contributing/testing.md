# Testing

Kerkov tests with the standard library's `unittest` and `unittest.mock`. Pytest
is also configured and runs the same tests, which is how CI collects the
async-native web tests.

## Layout and naming

`tests/` mirrors the `src/kerkov/` package tree: a change to
`src/kerkov/tools/cve_lookup_tool.py` has its tests in
`tests/tools/cve_lookup_tool_test.py`. Two naming rules are enforced by the
pytest configuration in `pyproject.toml` and are not optional:

| Thing | Rule | Example |
|---|---|---|
| Test file | `*_test.py` — the suffix, **not** the `test_*` prefix | `key_encoder_test.py` |
| Test class | `{Name}Test` | `StripAnsiTest`, `ChildAdapterTest` |
| Test method | `test_*` | `test_timeout_kills_command` |

`[tool.pytest.ini_options]` sets `python_files = "*_test.py"`,
`python_classes = "*Test"`, and `python_functions = "test_*"`. A file named
`test_foo.py` or a class named `TestFoo` is simply not collected — a silent way
to write tests that never run.

## Writing a test

Most tests subclass `unittest.TestCase`. Define mock factories as module-level
`_create_mock_*()` functions so they are shared across the file's test classes
rather than rebuilt inline.

```python
import unittest


class CveLookupToolTest(unittest.TestCase):
    def test_rejects_a_malformed_identifier(self) -> None:
        tool = CveLookupTool()
        error = tool.validate_params(CveLookupToolParams(cve_id="not-a-cve"))
        self.assertIsNotNone(error)
```

### Async tests outside `tests/web/`

Tool `run()` and provider `complete()` are coroutines, but `unittest` does not
await. Drive them with `asyncio.run()`, usually through a one-line module helper:

```python
import asyncio


def _run(coro):
    return asyncio.run(coro)


class ExecToolTest(unittest.TestCase):
    def test_runs_a_one_shot_command(self) -> None:
        result = _run(tool.run(ExecToolParams(command="echo hi")))
        self.assertTrue(result.success)
```

### `tests/web/` is different

The Django/Channels tests under `tests/web/` use **pytest-native** async tests
and fixtures (an autouse fixture in `tests/web/conftest.py` redirects the
engagement store to a temp directory so a test turn never writes into your real
`~/.kerkov/`). `unittest` does not collect those functions, so run `tests/web/`
under pytest.

## Running tests

```bash
uv run python -m unittest tests/tools/cve_lookup_tool_test.py -v   # one file
uv run pytest tests/tools/cve_lookup_tool_test.py                  # the same file via pytest
uv run pytest tests/web/                                           # the web tests (pytest only)
uv run pytest                                                      # everything, with coverage
```

The bare `pytest` run applies the configured `addopts`: coverage over `kerkov`
(`--cov`), `--strict-markers`, and `-v`. The registered markers are `slow`,
`integration`, and `security`.

## The coverage lint for `approval_scope`

`tests/tools/approval_scope_coverage_test.py` asserts that **every** tool
overrides [`approval_scope`](writing-a-tool.md#approval_scope) — the abstract
method has no default, so a new tool that forgets it fails this test rather than
inheriting someone else's idea of how broadly one approval may be saved. If you
add a tool, that test is one of the two docs-and-tests gates you must satisfy
(the other is the [`TOOL_PAGES` map in the docs lint](docs.md)).

## Related

- [Development setup](development.md) — the environment the tests run in.
- [Writing a tool](writing-a-tool.md) — what `approval_scope` is and why its test
  exists.
