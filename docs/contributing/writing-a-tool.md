# Writing a tool

A tool is a capability the model can call: read a file, run a command, look up a
CVE. Every tool extends `BaseTool` (`src/kerkov/tools/base_tool.py`), carries a
Pydantic parameters model, and returns a `ToolResult`. This page builds one end
to end, using [`cve_lookup`](../reference/tools/cve-lookup.md)
(`src/kerkov/tools/cve_lookup_tool.py`) as the clean example and
[`exec`](../reference/tools/exec-process.md) (`src/kerkov/tools/exec_tool.py`)
for contrast where the two make different choices.

## The four methods you implement

`BaseTool` is abstract. A tool implements four methods; the base class provides
`execute()`, which validates parameters and then calls your `run()`, so you never
override `execute()` yourself.

| Method | Returns | Purpose |
|---|---|---|
| `get_params_class()` | `Type[ToolParams]` | Names your parameters model |
| `validate_params(params)` | `Optional[str]` | Cross-field checks; `None` means valid, a string is the error |
| `run(params)` | `ToolResult` | The actual work — **`async`** |
| `approval_scope(params)` | `ApprovalScope` | How far one approval may be saved as a rule |

## 1. The parameters model

Subclass `ToolParams` (a thin Pydantic `BaseModel`) and describe every field
with a `Field`. The descriptions are not decoration — they are serialized into
the tool schema the model sees, so they are how the model learns to call the
tool correctly.

```python
from typing import Literal, Optional

from pydantic import Field

from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class CveLookupToolParams(ToolParams):
    action: Literal["get"] = Field(
        default="get",
        description="Lookup action. Only 'get' (one CVE by id) is supported.",
    )
    cve_id: str = Field(
        description="CVE identifier to look up, e.g. 'CVE-2021-44228'.",
    )
    refresh: Optional[bool] = Field(
        default=False,
        description="Re-fetch from every source, ignoring cached records.",
    )
```

Pydantic validates types and required fields before your code runs. A field that
violates its own type (a non-string `cve_id`) is rejected by `execute()` with a
validation error before `validate_params` or `run` is reached.

## 2. The tool class

```python
class CveLookupTool(BaseTool):
    """Fetch and fuse authoritative CVE data from NVD, CISA-KEV, and EPSS."""

    def __init__(self) -> None:
        super().__init__(
            name="cve_lookup",
            description=(
                "Look up authoritative data for a CVE from canonical feeds: "
                "NVD, CISA-KEV, and EPSS. Use it before rating or reporting any "
                "CVE — recalled CVSS scores go stale."
            ),
        )

    def get_params_class(self) -> type:
        return CveLookupToolParams
```

`name` is how the model and the approval rules address the tool; `description` is
the model's documentation for it. Both are passed to `super().__init__`.

A tool that needs a collaborator at runtime — a process supervisor, the config,
credentials — takes it as a constructor argument rather than building it at
import time. `ExecTool`, for example, is constructed with `execution_enabled`, a
registry, and a supervisor.

## 3. `validate_params` and `run`

`validate_params` is for checks Pydantic cannot express — cross-field rules, or
domain validation like "this looks like a CVE id". Return `None` when the input
is acceptable, or a human-readable error string.

```python
    def validate_params(self, toolParams: CveLookupToolParams) -> Optional[str]:
        cve_id = (toolParams.cve_id or "").strip()
        if not cve_id:
            return "`cve_id` must be a non-empty string"
        if not CVE_ID_PATTERN.match(cve_id.upper()):
            return f"`cve_id` must look like 'CVE-2021-44228', got '{cve_id}'"
        return None
```

`run` does the work and **always returns a `ToolResult`** — it catches its own
failures rather than raising. `ToolResult` has four fields: `success`, `output`
(the text the model sees), `error` (set when `success` is false), and `metadata`
(structured data that rides alongside but is not the primary output).

```python
    async def run(self, toolParams: CveLookupToolParams) -> ToolResult:
        try:
            record = await self._lookup(toolParams.cve_id.strip().upper())
        except Exception as e:
            return ToolResult(success=False, output="", error=f"CVE lookup failed: {e}")
        return ToolResult(
            success=True,
            output=_format_record(record),
            metadata={"cve_id": record.cve_id, "record": record.model_dump()},
        )
```

Keep `output` compact and human-readable; put the full structured result in
`metadata`. Large output is clipped into the [knowledge base](../concepts/knowledge-base.md)
for `recall`, and `metadata` counts toward that footprint, so do not duplicate a
huge body into both.

## How one approval generalizes { #approval_scope }

`approval_scope` is abstract — it has **no default**, and
`tests/tools/approval_scope_coverage_test.py` fails the build if a tool omits it.
This is deliberate: a tool inheriting someone else's idea of "broad enough" is
how a grant nobody chose gets made.

When the operator approves a call, the prompt offers to save it as a rule, and
the breadth you return here is what a single keystroke can widen that rule to. A
tool may only widen to a boundary its own arguments define. The factories on
`ApprovalScope` (`src/kerkov/core/approval_scope.py`) are:

| Factory | Offers | Use when |
|---|---|---|
| `exact_only()` | no broad rung | the arguments define no defensible boundary |
| `whole_tool()` | `tool({})` | nothing below the tool is worth distinguishing |
| `matching(patterns)` | an explicit glob pattern | you can state the boundary yourself |
| `keeping(params, fields)` | the named discriminating fields | a few arguments are the identity |
| `parent_directory(params, field)` | the directory of a path argument | a path's folder is the natural scope |

`cve_lookup` returns `ApprovalScope.whole_tool()` — keyless reads of public CVE
intelligence have nothing below the tool worth distinguishing:

```python
    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        return ApprovalScope.whole_tool()
```

`exec` returns the opposite, and its docstring says why:

```python
    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        # argv[0] is not a boundary: `sudo *` and `bash *` are blanket grants
        # wearing a program name, and `curl *` exfiltrates.
        return ApprovalScope.exact_only(volatile=("timeout", "no_output_timeout"))
```

`volatile` names parameters that must never reach a saved rule — values the
model varies per call (a `timeout`) or payloads that would leak into the rules
file (a request body). Use it so a saved rule matches on what identifies the
call, not on incidental arguments.

## 4. Register the tool

Where you register decides which conversations get the tool.

- **In the suite.** Most tools are built in the list returned by
  `ToolSuite.get_tools()` (`src/kerkov/tools/tool_suite.py`). A tool with no
  runtime dependency is appended directly (`CveLookupTool()`); one that needs a
  collaborator is constructed there with it. The suite is what the CLI, the web
  session, and agents all draw from.
- **At the CLI level.** A handful of tools are appended in
  `KerkovCLI.__init__` (`src/kerkov/cli/kerkov_cli.py`) instead —
  `recall` and `ledger`, for instance — because they need something that does not
  exist when the suite is built, such as the live engagement's knowledge base.
  They take a *getter* for that dependency rather than the object itself.

Choose the suite unless your tool genuinely depends on live session state.

!!! warning "A new tool has two docs-and-tests gates"
    1. Add the tool's `name` to the `TOOL_PAGES` map in `scripts/docs_lint.py`
       **and** give it a page under `docs/reference/tools/`, or the docs lint
       fails. See [Working on the docs](docs.md).
    2. Implement `approval_scope`, or `approval_scope_coverage_test.py` fails.

## Give it an approval rule, if it earns one

Auto-approval is opt-in through `config/approvals.yaml`. If your tool is
read-only and safe to run unattended, it may warrant an `allow` rule; most tools
do not, and leaving them unmatched is correct (NORMAL mode then prompts). The
allow list is short on purpose — read the [approvals guide](../guides/approvals.md)
before adding to it.

!!! danger "Edit both copies of the rules file"
    There are two: `config/approvals.yaml` (the source checkout) and its packaged
    twin `src/kerkov/config/approvals.yaml` (loaded by installed wheels). They
    must be kept byte-for-byte in step — a rule added to one and not the other
    behaves differently depending on how Kerkov was installed.

## Related

- [Tool reference](../reference/tools/index.md) — every shipped tool, as the
  model sees it.
- [Approvals guide](../guides/approvals.md) — rule syntax, precedence, and what
  `approval_scope` feeds.
- [Testing](testing.md) — the coverage test that enforces `approval_scope`.
