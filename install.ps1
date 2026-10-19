# Install a tagged Kerkov release into an isolated uv tool environment (Windows).
# Supports Windows PowerShell 5.1 and PowerShell 7+.

function Install-Kerkov {
    Set-StrictMode -Version 2.0
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    function Write-Fail([string]$Message) { [Console]::Error.WriteLine($Message) }
    function Format-Command([string]$Path) { "& '" + $Path.Replace("'", "''") + "'" }

    $web = $false; $chromium = 'ask'; $setupPath = $true
    foreach ($arg in $args) {
        switch -CaseSensitive ($arg) {
            '--web' { $web = $true }
            '--chromium' { $chromium = 'yes' }
            '--no-chromium' { $chromium = 'no' }
            '--no-modify-path' { $setupPath = $false }
            { $_ -in '-h', '--help' } {
                Write-Host @'
Usage: install.ps1 [--web] [--chromium | --no-chromium] [--no-modify-path]
  $env:VERSION='0.1.0'           Install this stable release (default: latest release).
  $env:INSTALL_DIR='C:\path\bin' Executable directory (default: ~\.kerkov\bin).
  $env:KERKOV_WHEEL_URL='URL'    Test a built wheel via HTTPS or file URL (requires VERSION).
  --web                 Include the experimental web console dependencies.
  --chromium            Download Playwright Chromium without prompting.
  --no-chromium         Skip the optional Chromium download prompt (for CI).
  --no-modify-path      Leave the user PATH unchanged.
'@
                return 0
            }
            default { Write-Fail "Unknown option: $arg"; return 1 }
        }
    }

    if ($env:OS -ne 'Windows_NT') {
        Write-Fail 'This installer is for Windows. On Linux and macOS, use install.sh.'
        return 1
    }
    # A 32-bit host on 64-bit Windows reports x86 here and the real arch below.
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($arch -notin 'AMD64', 'ARM64') {
        Write-Fail "Unsupported architecture: $arch (supported: x86_64 and arm64)."
        return 1
    }
    # Windows PowerShell 5.1 may default to TLS 1.0, which GitHub and astral.sh refuse.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $version = "$env:VERSION"
    $wheelUrl = "$env:KERKOV_WHEEL_URL"
    if ($wheelUrl) {
        if (-not $version) {
            Write-Fail 'VERSION is required when KERKOV_WHEEL_URL is set.'
            return 1
        }
        if ($wheelUrl -notmatch '^(https://|file:///)') {
            Write-Fail 'KERKOV_WHEEL_URL must be an HTTPS or absolute file URL.'
            return 1
        }
    }
    if (-not $version) {
        Write-Host 'Looking up the latest Kerkov release...'
        try {
            $release = Invoke-RestMethod -UseBasicParsing -TimeoutSec 60 `
                -Uri 'https://api.github.com/repos/Kerkov-Security/kerkov/releases/latest'
            $version = "$($release.tag_name)"
        } catch {
            Write-Fail 'Could not resolve a release. Check your connection/GitHub rate limit.'
            Write-Fail 'A maintainer must publish the first release before this installer can be used.'
            Write-Fail "To select an existing release explicitly, set `$env:VERSION='0.1.0'."
            return 1
        }
    }
    if ($version.StartsWith('v')) { $version = $version.Substring(1) }
    if ($version -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
        Write-Fail "Invalid release version: $version. Expected X.Y.Z or vX.Y.Z."
        return 1
    }

    $binDir = if ($env:INSTALL_DIR) { $env:INSTALL_DIR } else { Join-Path $env:USERPROFILE '.kerkov\bin' }
    if ($binDir -notmatch '^([A-Za-z]:\\|\\\\)') {
        Write-Fail 'INSTALL_DIR must be an absolute path.'
        return 1
    }
    if ($binDir -match "[;`r`n]") {
        Write-Fail 'INSTALL_DIR cannot contain newlines or semicolons.'
        return 1
    }

    $oldCommand = Get-Command kerkov -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty Source
    if ($oldCommand) { Write-Host "Existing command: $oldCommand" }

    $savedEnv = @{}
    foreach ($name in 'UV_TOOL_BIN_DIR', 'UV_INSTALL_DIR', 'UV_NO_MODIFY_PATH') {
        $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty Source
        if (-not $uv) {
            Write-Host "Installing uv (Astral Python package manager) into $binDir..."
            # Download completely, then run it in a child host: uv's installer may
            # call exit, which under `irm | iex` would close the user's own shell.
            $uvScript = Join-Path ([IO.Path]::GetTempPath()) ("kerkov-uv-" + [guid]::NewGuid() + '.ps1')
            try {
                Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -OutFile $uvScript `
                    -Uri 'https://astral.sh/uv/install.ps1'
                $env:UV_INSTALL_DIR = $binDir
                $env:UV_NO_MODIFY_PATH = '1'
                & (Get-Process -Id $PID).Path -NoProfile -ExecutionPolicy Bypass -File $uvScript | Out-Host
                if ($LASTEXITCODE -ne 0) { throw "uv installer exited with $LASTEXITCODE" }
            } catch {
                Write-Fail "Could not install uv: $_"
                return 1
            } finally {
                Remove-Item -LiteralPath $uvScript -Force -ErrorAction SilentlyContinue
            }
            $uv = Join-Path $binDir 'uv.exe'
        }

        $env:UV_TOOL_BIN_DIR = $binDir
        $package = if ($web) { 'kerkov[web]' } else { 'kerkov' }
        if (-not $wheelUrl) {
            $wheelUrl = "https://github.com/Kerkov-Security/kerkov/releases/download/v$version/kerkov-$version-py3-none-any.whl"
        }
        $archName = if ($arch -eq 'AMD64') { 'x86_64' } else { 'arm64' }
        Write-Host "Installing Kerkov $version for Windows/$archName (Python 3.13)..."
        # uv compares the pinned source AND extras, so a repeat is a no-op and
        # --web on an existing CLI install still installs the requested extras.
        & $uv tool install --no-config --python 3.13 "$package @ $wheelUrl" | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Fail 'Installation failed. Check the uv error above and the release URL:'
            Write-Fail $wheelUrl
            Write-Fail 'If an executable already exists in INSTALL_DIR, remove it using its owning package manager, then retry.'
            return 1
        }

        $toolDir = (& $uv tool dir --no-config | Out-String).Trim()
        $python = Join-Path $toolDir 'kerkov\Scripts\python.exe'
        $kerkovExe = Join-Path $binDir 'kerkov.exe'
        if (-not (Test-Path -LiteralPath $kerkovExe)) {
            # uv treats a repeat install as a no-op even when INSTALL_DIR changed.
            Write-Fail "Verification failed: $kerkovExe was not created."
            Write-Fail 'Rerun with the original INSTALL_DIR, or run uv tool uninstall kerkov first.'
            return 1
        }
        $installed = (& $kerkovExe --version | Out-String).Trim()
        if ($installed -ne "kerkov $version") {
            Write-Fail "Verification failed: expected kerkov $version, got $installed"
            return 1
        }
        & $python -m kerkov.core.install_support --seed | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Fail 'Default setup is incomplete; fix the reported permissions and retry the setup command above.'
        }
        if ($setupPath) {
            & $python -m kerkov.core.install_support --path $binDir | Out-Host
            if ($LASTEXITCODE -ne 0) {
                Write-Fail 'Automatic PATH setup is incomplete; use the PATH command printed below.'
            }
        } elseif ($env:GITHUB_PATH) {
            try {
                [IO.File]::AppendAllText($env:GITHUB_PATH, "$binDir`n")
            } catch {
                Write-Fail 'Could not update GITHUB_PATH; use the PATH command printed below.'
            }
        }
        $env:Path = "$binDir;$env:Path"

        if ($oldCommand -and ($oldCommand -ne (Join-Path $binDir 'kerkov.exe'))) {
            Write-Host "Another installation remains at $oldCommand."
            Write-Host 'System PATH entries precede user ones, so remove it if new terminals still find it first:'
            Write-Host 'pipx uninstall kerkov, or python -m pip uninstall kerkov with the Python that owns it.'
        }

        & $python -m kerkov.core.install_support --check-chromium | Out-Host
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'Playwright Chromium is ready.'
        } else {
            if ($chromium -eq 'ask' -and -not $env:CI -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
                $answer = Read-Host 'Download Playwright Chromium (large optional download)? [y/N]'
                if ($answer -in 'y', 'yes') { $chromium = 'yes' }
            }
            if ($chromium -eq 'yes') {
                & $python -m playwright install chromium | Out-Host
                if ($LASTEXITCODE -ne 0) { Write-Fail 'Chromium download failed; the CLI is still installed.' }
            }
            Write-Host "Browser setup/remediation: $(Format-Command $python) -m playwright install chromium"
        }
        & $python -m kerkov.core.install_support --preflight | Out-Host
        if ($LASTEXITCODE -ne 0) { Write-Fail 'Optional preflight could not complete.' }

        Write-Host ''
        Write-Host "$installed installed. Open a new terminal or run:"
        Write-Host ("  `$env:Path = '" + $binDir.Replace("'", "''") + ";' + `$env:Path")
        Write-Host '  kerkov'
        Write-Host ''
        Write-Host 'Follow the first-run provider prompt. Use /auth, /provider, and /help.'
        Write-Host 'Modes: kerkov --mode recon|audit|offensive'
        Write-Host 'Upgrade: rerun this installer (include --web to retain web dependencies).'
        Write-Host 'Uninstall: uv tool uninstall kerkov (your ~\.kerkov data is preserved).'
        return 0
    } finally {
        foreach ($name in $savedEnv.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process')
        }
    }
}

# Wrapping execution prevents a truncated piped download from running half a script.
$kerkovExitCode = Install-Kerkov @args
# Under `irm | iex` there is no script file, and `exit` would close the user's shell.
if ($PSCommandPath) { exit $kerkovExitCode }
if ($kerkovExitCode -ne 0) { $global:LASTEXITCODE = $kerkovExitCode }
