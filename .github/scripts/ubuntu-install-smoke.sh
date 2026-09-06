#!/usr/bin/env bash
# This script runs only inside the disposable Ubuntu container used by CI.
set -euo pipefail

if [ "${1:-}" != --as-user ]; then
    cat /etc/os-release
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash ca-certificates curl passwd util-linux
    useradd --create-home --shell /bin/bash kerkov-installer
    exec runuser -u kerkov-installer -- env -i \
        HOME=/home/kerkov-installer USER=kerkov-installer LOGNAME=kerkov-installer \
        SHELL=/bin/bash PATH=/usr/local/bin:/usr/bin:/bin CI=true \
        INSTALL_WEB="${INSTALL_WEB:-false}" \
        bash "$0" --as-user
fi

test "$(id -u)" -ne 0
for command in uv python python3 pip pip3 kerkov; do
    if command -v "$command" >/dev/null; then
        printf 'Expected a fresh machine, but found %s\n' "$command" >&2
        exit 1
    fi
done
printf 'Confirmed: regular user, no preinstalled uv, Python, pip, or Kerkov.\n'

set -- /inputs/dist/kerkov-*-py3-none-any.whl
test "$#" -eq 1 && test -f "$1"
wheel="$1"
version=${wheel##*/kerkov-}
version=${version%-py3-none-any.whl}
args=(--no-chromium)
if [ "$INSTALL_WEB" = true ]; then args+=(--web); fi

mkdir -p "$HOME/engagement"
cd "$HOME/engagement"

install_kerkov() {
    # Exercise the piped installer unchanged, using this build's wheel before
    # a release exists. uv and Python are downloaded by the real bootstrap.
    VERSION="$version" KERKOV_WHEEL_URL="file://$wheel" \
        bash -s -- "${args[@]}" < /inputs/install.sh
}

install_kerkov

# A separate login shell must find the command using the installed profile.
bash -lc '
    set -euo pipefail
    test "$(command -v kerkov)" = "$HOME/.kerkov/bin/kerkov"
    kerkov --version
    kerkov --help
'
# shellcheck source=/dev/null
source "$HOME/.profile"
test "$(kerkov --version)" = "kerkov $version"
tool_dir=$(uv tool dir --no-config)
tool_python="$tool_dir/kerkov/bin/python"
"$tool_python" - <<'PY'
import importlib.util
import os
import sys
from pathlib import Path
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.core.config_manager import ConfigManager

assert sys.version_info[:2] == (3, 13), sys.version
assert "localhost-scan" in {skill.name for skill in SkillManager().list_skills()}
home = Path.home() / ".kerkov"
assert (home / "skills/htb-machine-solve/scripts/htb_service.py").is_file()
assert (home / "agents/example-recon/AGENT.md").is_file()
assert Path(ConfigManager().http_profiles_path) == home / "config/http_profiles.yaml"
assert (home / "approvals.yaml").stat().st_mode & 0o777 == 0o600
assert (importlib.util.find_spec("django") is not None) == (os.environ["INSTALL_WEB"] == "true")
print(f"Verified Python {sys.version.split()[0]}, packaged skills, user config, and web selection.")
PY
if [ "$INSTALL_WEB" = true ]; then kerkov-web --help; fi

printf '# Keep my custom rules\nrules: []\n' > "$HOME/.kerkov/approvals.yaml"
cp "$HOME/.kerkov/approvals.yaml" "$HOME/expected-approvals.yaml"
before=$(stat -c '%Y' "$HOME/.kerkov/bin/kerkov")
install_kerkov
cmp "$HOME/expected-approvals.yaml" "$HOME/.kerkov/approvals.yaml"
test "$before" = "$(stat -c '%Y' "$HOME/.kerkov/bin/kerkov")"
test "$(kerkov --version)" = "kerkov $version"
printf '\nPASS: Kerkov %s installed on fresh Ubuntu; repeat install preserved user data.\n' "$version"
