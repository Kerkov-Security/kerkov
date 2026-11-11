"""Mode prompt content — delegation cues (issue #206, Part A item 2)."""

import unittest

from kerkov.core.session.modes import PURPLE_MODE, RED_MODE


class DelegationCueTest(unittest.TestCase):
    def test_purple_delegates_parallel_enumeration(self):
        self.assertIn(
            "delegate per-host and per-service scanning",
            PURPLE_MODE.system_prompt,
        )

    def test_red_reserves_the_main_thread_for_the_chain(self):
        prompt = RED_MODE.system_prompt
        self.assertIn("parallel `recon` agents", prompt)
        self.assertIn("reserve the main thread for the attack chain", prompt)


if __name__ == "__main__":
    unittest.main()
