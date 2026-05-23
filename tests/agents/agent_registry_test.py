"""Tests for AgentRegistry — depth + total-budget tracking."""

import unittest

from src.agents.agent_registry import AgentRegistry, BudgetExceeded


class CanSpawnTest(unittest.TestCase):
    def test_within_budget_allows(self):
        reg = AgentRegistry(max_depth=2, max_total=10)
        reg.can_spawn(count=3, parent_depth=0)  # no raise

    def test_depth_cap_raises(self):
        reg = AgentRegistry(max_depth=2, max_total=10)
        with self.assertRaises(BudgetExceeded):
            reg.can_spawn(count=1, parent_depth=2)

    def test_total_cap_raises(self):
        reg = AgentRegistry(max_depth=2, max_total=5)
        reg.record(4)
        with self.assertRaises(BudgetExceeded):
            reg.can_spawn(count=2, parent_depth=0)

    def test_total_exactly_at_cap_allowed(self):
        reg = AgentRegistry(max_depth=2, max_total=5)
        reg.record(4)
        reg.can_spawn(count=1, parent_depth=0)


class DepthCapReachedTest(unittest.TestCase):
    def test_true_at_cap(self):
        reg = AgentRegistry(max_depth=2, max_total=10)
        self.assertTrue(reg.depth_cap_reached(2))

    def test_false_below_cap(self):
        reg = AgentRegistry(max_depth=2, max_total=10)
        self.assertFalse(reg.depth_cap_reached(1))


if __name__ == "__main__":
    unittest.main()
