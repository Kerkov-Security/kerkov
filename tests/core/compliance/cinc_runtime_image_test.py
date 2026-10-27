"""Opt-in verification of generated SSH settings against an installed Cinc image."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kerkov.core.compliance.cinc_runtime import DockerCincRuntime, prepare_scan

IMAGE = os.environ.get("KERKOV_CINC_TEST_IMAGE")


@unittest.skipUnless(
    IMAGE and os.name != "nt", "set KERKOV_CINC_TEST_IMAGE to a local image"
)
class CincRuntimeImageTest(unittest.TestCase):
    def test_generated_config_and_mount_require_a_previously_trusted_host_key(self):
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
                b"fixture.invalid " + public + b"\n"
            )
            (workspace / "key").write_text("unused by this offline verifier test")
            (workspace / "key").chmod(0o600)
            prepared = prepare_scan(
                workspace,
                profile="baseline",
                private_key_file="key",
                known_hosts_file="known_hosts",
                inputs_file=None,
                host="fixture.invalid",
                user="audit",
                port=22,
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
host_keys = Net::SSH::KnownHosts.search_for("fixture.invalid", options)
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
