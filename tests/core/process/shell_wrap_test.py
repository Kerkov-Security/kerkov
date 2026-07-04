import base64
import importlib
import unittest
from unittest.mock import patch

from kerkov.core.process.shell_wrap import default_shell, shell_wrap

# The package re-exports the shell_wrap *function*, which shadows the submodule
# attribute on the package object, so fetch the real module from sys.modules.
shell_wrap_mod = importlib.import_module("kerkov.core.process.shell_wrap")


class ShellWrapPosixTest(unittest.TestCase):

    @patch.object(shell_wrap_mod.sys, "platform", "linux")
    def test_wraps_with_bash(self):
        assert shell_wrap("echo hi") == ["bash", "-c", "echo hi"]

    @patch.object(shell_wrap_mod.sys, "platform", "linux")
    def test_explicit_shell_override(self):
        assert shell_wrap("echo hi", shell="zsh") == ["zsh", "-c", "echo hi"]

    @patch.object(shell_wrap_mod.sys, "platform", "linux")
    def test_default_shell_is_bash(self):
        assert default_shell() == "bash"


class ShellWrapWindowsTest(unittest.TestCase):

    @patch.dict(shell_wrap_mod.os.environ, {}, clear=True)
    @patch.object(shell_wrap_mod.sys, "platform", "win32")
    def test_defaults_to_cmd(self):
        assert shell_wrap("echo hi") == ["cmd", "/c", "echo hi"]

    @patch.dict(
        shell_wrap_mod.os.environ, {"WINDOWS_DEFAULT_SHELL": "powershell"}, clear=True
    )
    @patch.object(shell_wrap_mod.sys, "platform", "win32")
    def test_env_selects_powershell(self):
        result = shell_wrap("echo hi")
        assert result[0] == "powershell"
        assert result[-1] == "echo hi"
        assert "-NoProfile" in result

    @patch.dict(
        shell_wrap_mod.os.environ, {"WINDOWS_DEFAULT_SHELL": "bogus"}, clear=True
    )
    @patch.object(shell_wrap_mod.sys, "platform", "win32")
    def test_unknown_env_falls_back_to_cmd(self):
        assert shell_wrap("echo hi") == ["cmd", "/c", "echo hi"]

    @patch.object(shell_wrap_mod.sys, "platform", "win32")
    def test_explicit_pwsh_override(self):
        result = shell_wrap("echo hi", shell="pwsh")
        assert result[0] == "pwsh"
        assert result[-1] == "echo hi"


class WindowsPtySpecTest(unittest.TestCase):

    @patch.object(shell_wrap_mod.sys, "platform", "win32")
    def test_cmd_spec_is_verbatim(self):
        appname, cmdline = shell_wrap_mod.windows_pty_spec(
            'python -c "print(1)"', shell="cmd"
        )
        assert appname.lower().endswith("cmd.exe")
        # Embedded quotes preserved verbatim (no backslash escaping).
        assert cmdline == ' /d /s /c "python -c "print(1)""'

    @patch.object(shell_wrap_mod.sys, "platform", "win32")
    def test_powershell_spec(self):
        appname, cmdline = shell_wrap_mod.windows_pty_spec(
            'Write-Output "KERKOV DQ"', shell="powershell"
        )
        assert appname == "powershell.exe"
        assert "-EncodedCommand" in cmdline
        assert "-Command" not in cmdline
        encoded = cmdline.split()[-1]
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        assert decoded == 'Write-Output "KERKOV DQ"'

    @patch.object(shell_wrap_mod.sys, "platform", "win32")
    def test_pwsh_spec_uses_encoded_command(self):
        appname, cmdline = shell_wrap_mod.windows_pty_spec(
            'Write-Output "KERKOV DQ"', shell="pwsh"
        )
        assert appname == "pwsh.exe"
        assert "-EncodedCommand" in cmdline
        encoded = cmdline.split()[-1]
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        assert decoded == 'Write-Output "KERKOV DQ"'


if __name__ == "__main__":
    unittest.main()
