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
            r"""
import json
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import yaml
from pathlib import Path
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.agents.agent_manager import AgentManager
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import CredentialManager, LocalFileSecretStore, credential_spec_for_env_var
from kerkov.core.session.approval_rules import ApprovalRulesEngine, default_approvals_source
from kerkov.core.install_support import seed_user_data
from kerkov.core.project_paths import bundled_asset_path, resolve_project_dir
from kerkov.core.session.state import build_session_state

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
user_approvals = Path.home() / ".kerkov/approvals.yaml"
assert yaml.safe_load(user_approvals.read_text()) == {"rules": []}
engine = ApprovalRulesEngine.from_files(user_approvals, default_approvals_source())
assert engine.evaluate("exec", {"command": "sudo whoami"}) == "deny"
assert resolve_project_dir("skills") == Path.home() / ".kerkov/skills"
assert "localhost-scan" in {skill.name for skill in SkillManager().list_skills()}
assert Path(ConfigManager().http_profiles_path) == Path.home() / ".kerkov/config/http_profiles.yaml"

# Execute the loaded HTB command's interpreter with an ordinary shell PATH and
# a persisted synthetic token. Mock the transport so this never calls HTB.
rendered = SkillManager().load_skill("htb-machine-setup")
skill_dir = resolve_project_dir("skills") / "htb-machine-setup"
command = re.search(r'exec command="(.+?)" mode=one_shot', rendered).group(1)
argv = shlex.split(command.replace("<skill_dir>", str(skill_dir)))
assert argv[0] == sys.executable, argv
store = LocalFileSecretStore(Path(os.environ["KERKOV_CREDENTIAL_FILE"]))
store.set(credential_spec_for_env_var("HTB_API_TOKEN").key, "kerkov-package-smoke-token")
runner = Path.cwd() / "htb-offline-smoke.py"
runner.write_text('''import json, runpy, sys
from unittest.mock import MagicMock, patch
def response(request, timeout):
    assert request.get_header("Authorization") == "Bearer kerkov-package-smoke-token"
    result = MagicMock()
    result.__enter__.return_value.status = 200
    result.__enter__.return_value.headers = {}
    result.__enter__.return_value.read.return_value = json.dumps({"info": {"id": 1, "name": "smoke"}}).encode()
    return result
sys.argv = sys.argv[1:]
with patch("urllib.request.urlopen", side_effect=response):
    runpy.run_path(sys.argv[0], run_name="__main__")
''')
child_env = {**os.environ, "PATH": os.defpath}
child_env.pop("PYTHONPATH", None)
child_env.pop("HTB_API_TOKEN", None)
result = subprocess.check_output([argv[0], str(runner), *argv[1:]], env=child_env, text=True)
assert json.loads(result)["id"] == 1

# Simulate the next bundle revision inside the isolated tool environment. An
# atomic replacement avoids modifying a possible uv-cache hardlink in place.
bundled_helper = bundled_asset_path("skills") / "htb-machine-setup/scripts/htb_service.py"
updated = bundled_helper.read_bytes() + b"\n# installed upgrade smoke revision\n"
replacement = bundled_helper.with_suffix(".new")
replacement.write_bytes(updated)
replacement.replace(bundled_helper)
custom_skill = skill_dir / "SKILL.md"
custom_content = custom_skill.read_text() + "\n\nLocal customization.\n"
custom_skill.write_text(custom_content)
# Older installers did not record provenance. Recognize their known default
# helpers while retaining a genuinely customized skill file.
(Path.home() / ".kerkov/.bundled-assets.json").unlink()
build_session_state(ConfigManager(), CredentialManager(environment={}, persistent_store=store))
assert (skill_dir / "scripts/htb_service.py").read_bytes() == updated
assert custom_skill.read_text() == custom_content
""",
        )
        # Adding web to the same version must not be mistaken for a no-op.
        run(*install, f"kerkov[web] @ {wheel.as_uri()}")
        run(str(python), "-c", "import django, channels")
        assert "runserver" in run(str(home / ".kerkov/bin/kerkov-web"), "--help")
        print(
            f"Wheel smoke passed: {wheel.name} (CLI, assets, HTB runtime, upgrades, web opt-in)"
        )


if __name__ == "__main__":
    main()
