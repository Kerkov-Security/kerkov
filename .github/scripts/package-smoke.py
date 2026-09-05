"""Exercise a built wheel as a CLI-only uv tool outside the checkout."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    wheel = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="kerkov-package-") as directory:
        scratch = Path(directory)
        home = scratch / "home with spaces"
        home.mkdir()
        engagement = scratch / "engagement"
        engagement.mkdir()
        (engagement / "pyproject.toml").write_text('[project]\nname = "unrelated"\n')
        (engagement / "skills").mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "UV_TOOL_DIR": str(scratch / "tools"),
            "UV_TOOL_BIN_DIR": str(home / ".kerkov/bin"),
            "KERKOV_LOGGING_DIR": str(scratch / "logs"),
            "KERKOV_CREDENTIAL_STORE": "file",
            "KERKOV_CREDENTIAL_FILE": str(home / ".kerkov/credentials.json"),
        }
        env.pop("PYTHONPATH", None)

        def run(*args: str) -> str:
            return subprocess.check_output(args, cwd=engagement, env=env, text=True)

        spec = f"kerkov @ {wheel.as_uri()}"
        install = ["uv", "tool", "install", "--no-config", "--python", sys.executable]
        run(*install, spec)
        executable = home / ".kerkov/bin/kerkov"
        python = scratch / "tools/kerkov/bin/python"
        version = wheel.name.split("-")[1]
        assert run(str(executable), "--version").strip() == f"kerkov {version}"
        assert "--version" in run(str(executable), "--help")
        # The second install must be a no-op with the same interpreter/env.
        before = executable.lstat().st_mtime_ns
        run(*install, spec)
        assert executable.lstat().st_mtime_ns == before
        run(
            str(python),
            "-c",
            """
import importlib.util
from pathlib import Path
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.agents.agent_manager import AgentManager
from kerkov.core.config_manager import ConfigManager
from kerkov.cli.approval_rules import ApprovalRulesEngine, default_approvals_source
from kerkov.core.install_support import seed_user_data
from kerkov.core.project_paths import resolve_project_dir

assert importlib.util.find_spec("django") is None
assert importlib.util.find_spec("kerkov.core.install_support") is not None
assert "bundled" in str(resolve_project_dir("skills"))
assert "localhost-scan" in {skill.name for skill in SkillManager().list_skills()}
assert (resolve_project_dir("skills") / "htb-machine-solve/scripts/htb_service.py").is_file()
assert (resolve_project_dir("agents") / "example-recon/AGENT.md").is_file()
AgentManager()
assert "htb:" in Path(ConfigManager().http_profiles_path).read_text()
engine = ApprovalRulesEngine.from_files(default_approvals_source())
assert engine.evaluate("exec", {"command": "sudo whoami"}) == "deny"
assert seed_user_data() > 0
assert seed_user_data() == 0
assert resolve_project_dir("skills") == Path.home() / ".kerkov/skills"
assert "localhost-scan" in {skill.name for skill in SkillManager().list_skills()}
assert Path(ConfigManager().http_profiles_path) == Path.home() / ".kerkov/config/http_profiles.yaml"
""",
        )
        # Adding web to the same version must not be mistaken for a no-op.
        run(*install, f"kerkov[web] @ {wheel.as_uri()}")
        run(str(python), "-c", "import django, channels")
        assert "runserver" in run(str(home / ".kerkov/bin/kerkov-web"), "--help")
        print(
            f"Wheel smoke passed: {wheel.name} (CLI, assets, repeat install, web opt-in)"
        )


if __name__ == "__main__":
    main()
