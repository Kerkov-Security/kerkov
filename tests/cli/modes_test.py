"""Mode prompt content — delegation cues (issue #206, Part A item 2)."""

import unittest

from kerkov.core.session.modes import AUDIT_MODE, CTF_MODE, OFFENSIVE_MODE


class DelegationCueTest(unittest.TestCase):
    def test_audit_delegates_parallel_enumeration(self):
        self.assertIn(
            "delegate per-host and per-service scanning",
            AUDIT_MODE.system_prompt,
        )

    def test_offensive_reserves_the_main_thread_for_the_chain(self):
        prompt = OFFENSIVE_MODE.system_prompt
        self.assertIn("parallel `recon` agents", prompt)
        self.assertIn("reserve the main thread for the attack chain", prompt)

    def test_ctf_delegates_independent_parts(self):
        self.assertIn(
            "When a challenge splits into independent parts",
            CTF_MODE.system_prompt,
        )


if __name__ == "__main__":
    unittest.main()
