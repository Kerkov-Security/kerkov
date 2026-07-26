"""Tests for the HTTP credential profile loader."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

from kerkov.tools.http_profile_loader import ResolvedProfile, load_http_profiles


def _write_yaml(directory: Path, content: str) -> Path:
    path = directory / "http_profiles.yaml"
    path.write_text(dedent(content), encoding="utf-8")
    return path


class LoadValidYamlTest(unittest.TestCase):
    def test_returns_typed_resolved_profile(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  example:
                    base_url: https://api.example.com
                    headers:
                      Accept: application/json
                      User-Agent: kerkov-bot/0.1
                    timeout_seconds: 30
                """,
            )
            profiles = load_http_profiles(path, env={})
            self.assertIn("example", profiles)
            profile = profiles["example"]
            self.assertIsInstance(profile, ResolvedProfile)
            self.assertEqual(profile.base_url, "https://api.example.com")
            self.assertEqual(profile.headers["Accept"], "application/json")
            self.assertEqual(profile.headers["User-Agent"], "kerkov-bot/0.1")
            self.assertEqual(profile.timeout_seconds, 30.0)

    def test_default_timeout_when_omitted(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  example:
                    base_url: https://api.example.com
                """,
            )
            profiles = load_http_profiles(path, env={})
            self.assertEqual(profiles["example"].timeout_seconds, 60.0)
            self.assertEqual(profiles["example"].headers, {})

    def test_headers_property_returns_copy(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  example:
                    base_url: https://api.example.com
                    headers:
                      Accept: application/json
                """,
            )
            profile = load_http_profiles(path, env={})["example"]
            headers = profile.headers
            headers["Mutated"] = "yes"
            self.assertNotIn("Mutated", profile.headers)


class EnvSubstitutionTest(unittest.TestCase):
    def test_substitutes_in_headers(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  htb:
                    base_url: https://www.hackthebox.com
                    headers:
                      Authorization: "Bearer ${HTB_API_TOKEN}"
                """,
            )
            profiles = load_http_profiles(path, env={"HTB_API_TOKEN": "secret123"})
            self.assertEqual(
                profiles["htb"].headers["Authorization"], "Bearer secret123"
            )

    def test_substitutes_in_base_url(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  custom:
                    base_url: "https://${CUSTOM_HOST}/api"
                """,
            )
            profiles = load_http_profiles(path, env={"CUSTOM_HOST": "example.com"})
            self.assertEqual(profiles["custom"].base_url, "https://example.com/api")

    def test_missing_env_var_raises_with_profile_and_var_name(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  htb:
                    base_url: https://www.hackthebox.com
                    headers:
                      Authorization: "Bearer ${HTB_API_TOKEN}"
                """,
            )
            with self.assertRaises(ValueError) as ctx:
                load_http_profiles(path, env={})
            message = str(ctx.exception)
            self.assertIn("HTB_API_TOKEN", message)
            self.assertIn("htb", message)

    def test_interactive_loading_retains_missing_variable_for_first_use(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  htb:
                    base_url: https://labs.hackthebox.com
                    headers:
                      Authorization: "Bearer ${HTB_API_TOKEN}"
                """,
            )

            profile = load_http_profiles(
                path,
                env={},
                allow_unresolved=True,
            )["htb"]

            self.assertEqual(profile.required_env_vars, {"HTB_API_TOKEN"})
            resolved = profile.resolve({"HTB_API_TOKEN": "secret123"})
            self.assertEqual(
                resolved.headers["Authorization"],
                "Bearer secret123",
            )
            self.assertEqual(resolved.required_env_vars, set())


class FailureTest(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "does_not_exist.yaml"
            self.assertEqual(load_http_profiles(path, env={}), {})

    def test_empty_file_returns_empty_dict(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(Path(d), "")
            self.assertEqual(load_http_profiles(path, env={}), {})

    def test_malformed_yaml_raises(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(Path(d), "profiles:\n  - [unclosed bracket\n")
            with self.assertRaises(ValueError) as ctx:
                load_http_profiles(path, env={})
            self.assertIn("Malformed YAML", str(ctx.exception))

    def test_schema_violation_raises(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  bad:
                    headers:
                      Accept: application/json
                """,
            )
            with self.assertRaises(ValueError) as ctx:
                load_http_profiles(path, env={})
            self.assertIn("Invalid HTTP profiles schema", str(ctx.exception))


class RedactionTest(unittest.TestCase):
    def test_repr_redacts_authorization_header(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  htb:
                    base_url: https://www.hackthebox.com
                    headers:
                      Authorization: "Bearer supersecretvalue1234"
                """,
            )
            profile = load_http_profiles(path, env={})["htb"]
            self.assertNotIn("supersecretvalue1234", repr(profile))
            self.assertNotIn("supersecretvalue1234", str(profile))

    def test_repr_redacts_api_key_header(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  shodan:
                    base_url: https://api.shodan.io
                    headers:
                      X-Api-Key: confidential-token-9999
                """,
            )
            profile = load_http_profiles(path, env={})["shodan"]
            self.assertNotIn("confidential-token-9999", repr(profile))

    def test_repr_does_not_redact_innocent_headers(self) -> None:
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  example:
                    base_url: https://api.example.com
                    headers:
                      Accept: application/json
                      User-Agent: kerkov-bot/0.1
                """,
            )
            profile = load_http_profiles(path, env={})["example"]
            text = repr(profile)
            self.assertIn("application/json", text)
            self.assertIn("kerkov-bot/0.1", text)


if __name__ == "__main__":
    unittest.main()
