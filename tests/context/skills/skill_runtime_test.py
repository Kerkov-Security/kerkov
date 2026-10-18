"""Loaded helper commands use the app interpreter, independent of shell PATH."""

import asyncio
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kerkov.cli.messages.skill_message import SkillLoadMessage
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.tools.skill_tool import SkillTool, SkillToolParams


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell execution and symlinks")
@pytest.mark.parametrize("entrypoint", ["slash_command", "tool"])
def test_loaded_helper_uses_quoted_runtime_with_no_python_on_path(
    tmp_path, monkeypatch, entrypoint
):
    runtime = tmp_path / "runtime's $shell tools" / "python"
    runtime.parent.mkdir()
    runtime.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(runtime))
    skill = tmp_path / "skills" / "runtime-test" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: runtime-test\ndescription: Test the runtime\n---\n"
        "```sh\n<kerkov_python> -c 'print(12345)'\n```\n"
    )
    manager = SkillManager(str(skill.parent.parent))
    if entrypoint == "tool":
        result = asyncio.run(
            SkillTool(manager).run(
                SkillToolParams(action="load", skill_id="runtime-test")
            )
        )
        assert result.success
        text = result.output
    else:
        cli = SimpleNamespace(
            skill_manager=manager, console=Mock(), get_response=AsyncMock()
        )
        asyncio.run(SkillLoadMessage("runtime-test").execute(cli))
        text = cli.get_response.call_args.args[0]
        assert str(skill.parent) in text
    command = re.search(r"```sh\n(.*?)\n```", text, re.DOTALL).group(1)
    assert shlex.split(command)[0] == str(runtime)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        env={"PATH": str(tmp_path / "no-commands")},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "12345"
    assert "<kerkov_python>" in manager.get_skill("runtime-test").content
