#!/usr/bin/env bash
# Install a tagged Kerkov release into an isolated uv tool environment.
set -euo pipefail

main() {
    local web=false chromium=ask setup_path=true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --web) web=true ;;
            --chromium) chromium=yes ;;
            --no-chromium) chromium=no ;;
            --no-modify-path) setup_path=false ;;
            -h|--help)
                cat <<'HELP'
Usage: bash install.sh [--web] [--chromium | --no-chromium] [--no-modify-path]
  VERSION=0.1.0          Install this stable release (default: latest release).
  INSTALL_DIR=/path/bin  Executable directory (default: ~/.kerkov/bin).
  --web                 Include the experimental web console dependencies.
  --chromium            Download Playwright Chromium without prompting.
  --no-chromium         Skip the optional Chromium download prompt (for CI).
  --no-modify-path      Leave shell startup files unchanged.
HELP
                return ;;
            *) printf 'Unknown option: %s\n' "$1" >&2; return 1 ;;
        esac
        shift
    done

    local os arch
    os=$(uname -s)
    arch=$(uname -m)
    case "$os" in
        Linux|Darwin) ;;
        MINGW*|MSYS*|CYGWIN*)
            printf '%s\n' 'On Windows, use Python 3.13 and pip install <wheel URL> from:' \
                'https://github.com/Kerkov-Security/kerkov/releases/latest' >&2
            return 1 ;;
        *) printf 'Unsupported OS: %s (supported: Linux and macOS).\n' "$os" >&2; return 1 ;;
    esac
    case "$arch" in
        x86_64|amd64|aarch64|arm64) ;;
        *) printf 'Unsupported architecture: %s (supported: x86_64 and arm64).\n' "$arch" >&2; return 1 ;;
    esac
    command -v curl >/dev/null || { printf 'curl is required. Install curl and retry.\n' >&2; return 1; }

    local version=${VERSION:-} release_json
    if [ -z "$version" ]; then
        printf 'Looking up the latest Kerkov release...\n'
        if ! release_json=$(curl --fail --silent --show-error --location --retry 2 \
            --connect-timeout 15 --max-time 60 \
            https://api.github.com/repos/Kerkov-Security/kerkov/releases/latest); then
            printf '%s\n' 'Could not resolve a release. Check your connection/GitHub rate limit.' \
                'A maintainer must publish the first release before this installer can be used.' \
                'To select an existing release explicitly, set VERSION=0.1.0.' >&2
            return 1
        fi
        version=$(printf '%s\n' "$release_json" | sed -nE 's/.*"tag_name"[[:space:]]*:[[:space:]]*"(v[0-9]+\.[0-9]+\.[0-9]+)".*/\1/p')
    fi
    version=${version#v}
    if [[ ! "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
        printf 'Invalid release version: %s. Expected X.Y.Z or vX.Y.Z.\n' "$version" >&2
        return 1
    fi

    local bin_dir=${INSTALL_DIR:-"$HOME/.kerkov/bin"}
    case "$bin_dir" in
        /*) ;;
        *) printf 'INSTALL_DIR must be an absolute path.\n' >&2; return 1 ;;
    esac
    case "$bin_dir" in
        *$'\n'*|*$'\r'*|*:* ) printf 'INSTALL_DIR cannot contain newlines or colons.\n' >&2; return 1 ;;
    esac

    local old_command
    old_command=$(command -v kerkov || true)
    if [ -n "$old_command" ]; then
        printf 'Existing command: %s\n' "$old_command"
    fi

    local uv_bin
    if command -v uv >/dev/null; then
        uv_bin=$(command -v uv)
    else
        printf 'Installing uv (Astral Python package manager) into %s...\n' "$bin_dir"
        # Download completely before executing; uv must not edit profiles itself.
        local uv_script
        uv_script=$(curl --fail --silent --show-error --location --retry 2 \
            --connect-timeout 15 --max-time 120 https://astral.sh/uv/install.sh)
        printf '%s\n' "$uv_script" | env UV_INSTALL_DIR="$bin_dir" UV_NO_MODIFY_PATH=1 sh
        uv_bin="$bin_dir/uv"
    fi
    export UV_TOOL_BIN_DIR="$bin_dir"
    local package=kerkov
    if [ "$web" = true ]; then package='kerkov[web]'; fi
    local wheel_url="https://github.com/Kerkov-Security/kerkov/releases/download/v${version}/kerkov-${version}-py3-none-any.whl"
    printf 'Installing Kerkov %s for %s/%s (Python 3.13)...\n' "$version" "$os" "$arch"
    # uv compares the pinned source AND extras, so a repeat is a no-op and
    # --web on an existing CLI install still installs the requested extras.
    if ! "$uv_bin" tool install --no-config --python 3.13 "$package @ $wheel_url"; then
        printf '%s\n' 'Installation failed. Check the uv error above and the release URL:' "$wheel_url" >&2
        printf '%s\n' 'If an executable already exists in INSTALL_DIR, remove it using its owning package manager, then retry.' >&2
        return 1
    fi

    local tool_dir python installed
    tool_dir=$("$uv_bin" tool dir --no-config)
    python="$tool_dir/kerkov/bin/python"
    installed=$("$bin_dir/kerkov" --version)
    if [ "$installed" != "kerkov $version" ]; then
        printf 'Verification failed: expected kerkov %s, got %s\n' "$version" "$installed" >&2
        return 1
    fi
    "$python" -m kerkov.core.install_support --seed
    if [ "$setup_path" = true ]; then
        "$python" -m kerkov.core.install_support --path "$bin_dir"
    elif [ -n "${GITHUB_PATH:-}" ]; then
        printf '%s\n' "$bin_dir" >> "$GITHUB_PATH"
    fi
    export PATH="$bin_dir:$PATH"

    if [ -n "$old_command" ] && [ "$old_command" != "$bin_dir/kerkov" ]; then
        printf 'The new PATH entry takes precedence over %s in new shells.\n' "$old_command"
        printf '%s\n' 'To remove an older installation, use its package manager: pipx uninstall kerkov' \
            'or python -m pip uninstall kerkov with the Python that owns that installation.'
    fi

    if "$python" -m kerkov.core.install_support --check-chromium; then
        printf 'Playwright Chromium is ready.\n'
    else
        if [ "$chromium" = ask ] && [ -z "${CI:-}" ] && ( : </dev/tty ) 2>/dev/null; then
            local answer
            printf 'Download Playwright Chromium (large optional download)? [y/N] ' >/dev/tty
            read -r answer </dev/tty || answer=n
            case "$answer" in y|Y|yes|YES) chromium=yes ;; esac
        fi
        if [ "$chromium" = yes ]; then
            "$python" -m playwright install chromium || printf 'Chromium download failed; the CLI is still installed.\n' >&2
        fi
        printf 'Browser setup/remediation: %q -m playwright install chromium\n' "$python"
        if [ "$os" = Linux ]; then
            printf 'If browser OS libraries are missing: %q -m playwright install-deps chromium\n' "$python"
        fi
    fi
    "$python" -m kerkov.core.install_support --preflight || printf 'Optional preflight could not complete.\n' >&2

    printf '\n%s installed. Open a new terminal or run:\n' "$installed"
    local shell_name=${SHELL:-sh}
    if [ "${shell_name##*/}" = fish ]; then
        # shellcheck disable=SC2016 # Print a command for the user's next shell.
        printf '  set -gx PATH %q $PATH\n' "$bin_dir"
    else
        # shellcheck disable=SC2016 # Print a command for the user's next shell.
        printf '  export PATH=%q:"$PATH"\n' "$bin_dir"
    fi
    printf '%s\n' '  kerkov' '' \
        'Follow the first-run provider prompt. Use /auth, /provider, and /help.' \
        'Modes: kerkov --mode recon|audit|offensive' \
        'Upgrade: rerun this installer (include --web to retain web dependencies).' \
        'Uninstall: uv tool uninstall kerkov (your ~/.kerkov data is preserved).'
}

# Wrapping execution prevents a truncated piped download from running half a script.
main "$@"
