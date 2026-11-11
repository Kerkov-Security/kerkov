"""Opt-in verification of generated SSH settings against an installed Cinc image."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kerkov.core.compliance.cinc_runtime import DockerCincRuntime, prepare_scan

IMAGE = os.environ.get("KERKOV_CINC_TEST_IMAGE")


@unittest.skipUnless(
    IMAGE and os.name != "nt", "set KERKOV_CINC_TEST_IMAGE to a local image"
)
class CincRuntimeImageTest(unittest.TestCase):
    def test_generated_config_and_mount_require_a_previously_trusted_host_key(self):
        self._verify_trusted_config("fixture.invalid", 22)

    def test_loopback_route_keeps_the_original_known_host_identity(self):
        with patch("kerkov.core.compliance.cinc_runtime.sys.platform", "darwin"):
            self._verify_trusted_config("127.0.0.1", 2222)

    def _verify_trusted_config(self, host, port):
        image_id = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            profile = workspace / "baseline"
            profile.mkdir()
            (profile / "inspec.yml").write_text("name: fixture\n")
            public = (
                rsa.generate_private_key(65537, 2048)
                .public_key()
                .public_bytes(
                    serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
                )
            )
            (workspace / "known_hosts").write_bytes(
                (host if port == 22 else f"[{host}]:{port}").encode()
                + b" "
                + public
                + b"\n"
            )
            (workspace / "key").write_text("unused by this offline verifier test")
            (workspace / "key").chmod(0o600)
            prepared = prepare_scan(
                workspace,
                profile="baseline",
                private_key_file="key",
                known_hosts_file="known_hosts",
                inputs_file=None,
                host=host,
                user="audit",
                port=port,
                sudo=False,
            )
            args = DockerCincRuntime(image=IMAGE)._create_args(prepared, image_id)
            script = """
require "json"
require "base64"
require "train"
require "net/ssh"
require "net/ssh/verifiers/always"
config = JSON.parse(File.read("/run/kerkov/config/connection.json"), symbolize_names: true)
transport = Train.create(config.delete(:backend), config)
options = transport.send(:connection_options, transport.options)
abort "host verification is not strict" unless options[:verify_host_key] == :always
options = Net::SSH.configuration_for(config[:host], options[:config]).merge(options)
identity = config[:port] == 22 ? config[:host] : "[#{config[:host]}]:#{config[:port]}"
if config[:host] == "127.0.0.1"
  abort "loopback was not routed through Docker host" unless options[:host_name] == "host.docker.internal"
  abort "host-key alias changed identity" unless options[:host_key_alias] == identity
  abort "gateway acquired trust" unless Net::SSH::KnownHosts.search_for("[host.docker.internal]:2222", options).empty?
end
host_keys = Net::SSH::KnownHosts.search_for(options[:host_key_alias] || identity, options)
abort "mounted trusted keys were not loaded" if host_keys.empty?
blob = Base64.decode64(File.read("/etc/ssh/ssh_known_hosts").split[2])
key = Net::SSH::Buffer.new(blob).read_key
session = Struct.new(:host_keys).new(host_keys)
verifier = Net::SSH::Verifiers::Always.new
abort "trusted key rejected" unless verifier.verify(session: session, key: key)
begin
  verifier.verify(session: session, key: OpenSSL::PKey::RSA.new(2048))
  abort "mismatched key accepted"
rescue Net::SSH::HostKeyMismatch
end
session.host_keys = Net::SSH::KnownHosts.search_for("unknown.invalid", options)
begin
  verifier.verify(session: session, key: key)
  abort "unknown key accepted"
rescue Net::SSH::HostKeyUnknown
end
puts JSON.generate(train: Train::VERSION, net_ssh: Net::SSH::Version::CURRENT.to_s)
"""
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--entrypoint=/opt/cinc-auditor/embedded/bin/ruby",
                    *args[2 : args.index(image_id)],
                    image_id,
                    "-e",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("net_ssh", json.loads(result.stdout))
