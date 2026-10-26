#!/usr/bin/env python3
"""Mechanical accuracy checks for the published documentation site.

Every failure class found in the issue #200 audit is detectable from the code:
tools, slash commands and settings are all introspectable, so the tables that
describe them can be asserted against the source rather than reviewed by eye.

What this cannot do is verify that prose is *true* — a page can name every tool
and still describe them wrongly. This bounds the rot; it does not replace
reading the code.
"""

from __future__ import annotations

import argparse
import inspect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"

# Symbols from the deleted shell stack. None may appear on a published page as
# part of the active system.
DENYLISTED_SYMBOLS = (
    "ShellTool",
    "FetchTool",
    "PersistentShellToolSuite",
    "ShellManager",
    "BashTool",
)

# Tool name -> the reference page documenting it. Several tools share a page, so
# this is a map rather than one-file-per-tool. Every tool the suite can build,
# conditional ones included, must appear here; the check reports any that do not.
TOOL_PAGES = {
    "read": "reference/tools/read-write.md",
    "write": "reference/tools/read-write.md",
    "read_url": "reference/tools/web-search.md",
    "web_search": "reference/tools/web-search.md",
    "http": "reference/tools/http.md",
    "exec": "reference/tools/exec-process.md",
    "process": "reference/tools/exec-process.md",
    "browser": "reference/tools/browser.md",
    "container_lifecycle": "reference/tools/container.md",
    "sbom_generate": "reference/tools/sbom-vulnerability-scan.md",
    "vulnerability_scan": "reference/tools/sbom-vulnerability-scan.md",
    "sast_scan": "reference/tools/sast.md",
    "ansible": "reference/tools/ansible.md",
    "compliance_report": "reference/tools/compliance.md",
    "compliance_scan": "reference/tools/compliance.md",
    "cve_lookup": "reference/tools/cve-lookup.md",
    "zap": "reference/tools/zap.md",
    "skill_lookup": "reference/tools/skill.md",
    "recall": "reference/tools/recall.md",
    "ledger": "reference/tools/ledger.md",
    "delegate": "reference/tools/delegate.md",
    "agent_wait": "reference/tools/delegate.md",
}

SLASH_COMMANDS_PAGE = "reference/slash-commands.md"
CONFIGURATION_PAGE = "reference/configuration.md"
LEDGER_REFERENCE_PAGE = "reference/tools/ledger.md"
LEDGER_CONCEPT_PAGE = "concepts/findings-ledger.md"

_SOURCE_PATH = re.compile(r"\b(src/kerkov/[A-Za-z0-9_/]+\.py)\b")


@dataclass
class CheckResult:
    """Outcome of one check: failures, or a reason it cannot run yet."""

    name: str
    failures: list[str] = field(default_factory=list)
    pending: Optional[str] = None

    @property
    def status(self) -> str:
        if self.failures:
            return "FAIL"
        return "PENDING" if self.pending else "PASS"


class _IgnoreUnknownTags(yaml.SafeLoader):
    """Load mkdocs.yml, whose extension config carries !!python/name: tags."""


_IgnoreUnknownTags.add_multi_constructor("", lambda loader, suffix, node: None)


def published_pages() -> list[str]:
    """Docs-relative paths of every page reachable from the mkdocs nav."""

    config = yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_IgnoreUnknownTags)
    return sorted(set(_walk_nav(config.get("nav") or [])))


def _walk_nav(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        if node.endswith(".md"):
            yield node
    elif isinstance(node, list):
        for item in node:
            yield from _walk_nav(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_nav(value)


def _page_text(page: str) -> str:
    return (DOCS_DIR / page).read_text(encoding="utf-8")


def _instantiate(cls: type) -> Any:
    """Build a tool with stand-ins, to read the name it registers under."""

    signature = inspect.signature(cls.__init__)
    parameters = list(signature.parameters.values())[1:]
    args = [
        MagicMock()
        for p in parameters
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    kwargs = {
        p.name: MagicMock()
        for p in parameters
        if p.kind is p.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    }
    return cls(*args, **kwargs)


def registered_tool_names() -> set[str]:
    """Every tool name this checkout can register, suite plus CLI level.

    The suite is environment-sensitive — `web_search` needs a search provider and
    `browser` can be switched off — so a runtime-only list would silently shrink
    in CI. Discovery is therefore additive: what this environment builds, plus
    the CLI-level tools, and TOOL_PAGES has to cover all of it.
    """

    from kerkov.agents.agent_tool import SpawnAgentTool
    from kerkov.agents.agent_wait_tool import AgentWaitTool
    from kerkov.tools.ledger_tool import LedgerTool
    from kerkov.tools.recall_tool import RecallTool
    from kerkov.tools.skill_tool import SkillTool
    from kerkov.tools.tool_suite import ToolSuite

    names = {tool.name for tool in ToolSuite.get_tools()}
    for cls in (SkillTool, RecallTool, LedgerTool, SpawnAgentTool, AgentWaitTool):
        names.add(_instantiate(cls).name)
    return names


def check_source_paths(pages: list[str]) -> CheckResult:
    result = CheckResult("source-paths")
    for page in pages:
        for match in sorted(set(_SOURCE_PATH.findall(_page_text(page)))):
            if not (REPO_ROOT / match).exists():
                result.failures.append(f"{page}: cites missing source file {match}")
    return result


def check_denylist(pages: list[str]) -> CheckResult:
    result = CheckResult("denylist")
    for page in pages:
        text = _page_text(page)
        for symbol in DENYLISTED_SYMBOLS:
            if re.search(rf"\b{symbol}\b", text):
                result.failures.append(f"{page}: mentions removed symbol {symbol}")
    return result


def check_tool_pages(pages: list[str]) -> CheckResult:
    result = CheckResult("tool-pages")
    published = set(pages)

    for name in sorted(registered_tool_names()):
        if name not in TOOL_PAGES:
            result.failures.append(
                f"tool {name!r} is registered but absent from TOOL_PAGES in this script"
            )

    missing = sorted({p for p in TOOL_PAGES.values() if p not in published})
    if missing:
        covered = sorted({t for t, p in TOOL_PAGES.items() if p in missing})
        result.pending = (
            f"{len(missing)} tool reference page(s) not yet published, "
            f"covering {len(covered)} tool(s): {', '.join(missing)}"
        )
    return result


def check_slash_commands(pages: list[str]) -> CheckResult:
    from kerkov.cli import input_parser

    result = CheckResult("slash-commands")
    if SLASH_COMMANDS_PAGE not in pages:
        result.pending = f"{SLASH_COMMANDS_PAGE} is not published yet"
        return result

    text = _page_text(SLASH_COMMANDS_PAGE)
    for command in input_parser.COMMANDS:
        for name in command.names:
            if not re.search(rf"(?<![\w/]){re.escape(name)}\b", text):
                result.failures.append(
                    f"{SLASH_COMMANDS_PAGE}: no entry for {name}"
                    + (f" (alias of {command.name})" if name != command.name else "")
                )
    return result


def check_config_settings(pages: list[str]) -> CheckResult:
    from kerkov.core.config_manager import ConfigManager

    result = CheckResult("config-settings")
    if CONFIGURATION_PAGE not in pages:
        result.pending = f"{CONFIGURATION_PAGE} is not published yet"
        return result

    text = _page_text(CONFIGURATION_PAGE)
    for field_name, info in ConfigManager.model_fields.items():
        env_name = info.alias or field_name.upper()
        if not re.search(rf"\b{re.escape(env_name)}\b", text):
            result.failures.append(f"{CONFIGURATION_PAGE}: no row for {env_name}")
    return result


def check_ledger_vocabulary(pages: list[str]) -> CheckResult:
    from kerkov.core.knowledge_base.models import LedgerSection
    from kerkov.tools.ledger_tool import _LEDGER_OP

    result = CheckResult("ledger-vocabulary")
    unpublished = [
        page
        for page in (LEDGER_REFERENCE_PAGE, LEDGER_CONCEPT_PAGE)
        if page not in pages
    ]
    if unpublished:
        result.pending = f"{', '.join(unpublished)} not published yet"
        return result

    vocabulary = [section.value for section in LedgerSection] + list(_LEDGER_OP)
    for page in (LEDGER_REFERENCE_PAGE, LEDGER_CONCEPT_PAGE):
        text = _page_text(page)
        for term in vocabulary:
            if not re.search(rf"\b{re.escape(term)}\b", text):
                result.failures.append(f"{page}: no mention of ledger term {term!r}")
    return result


def run_checks() -> list[CheckResult]:
    pages = published_pages()
    return [
        check_source_paths(pages),
        check_denylist(pages),
        check_tool_pages(pages),
        check_slash_commands(pages),
        check_config_settings(pages),
        check_ledger_vocabulary(pages),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat pending checks as failures (for once every page is written).",
    )
    args = parser.parse_args()

    results = run_checks()
    failed = False

    for result in results:
        print(f"[{result.status:>7}] {result.name}")
        for failure in result.failures:
            print(f"          {failure}")
        if result.pending:
            print(f"          {result.pending}")
        if result.failures or (result.pending and args.strict):
            failed = True

    pending = [r.name for r in results if r.pending]
    if pending and not args.strict:
        print(
            f"\n{len(pending)} check(s) pending on unwritten pages: {', '.join(pending)}."
            "\nRun with --strict once those pages land."
        )
    print("\nFAILED" if failed else "\nOK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
