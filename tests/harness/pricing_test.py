"""Tests for harness pricing tables."""

import unittest

from src.harness.pricing import compute_cost_usd, get_pricing


class GetPricingTest(unittest.TestCase):
    def test_exact_match(self):
        pricing = get_pricing("ollama")
        self.assertIsNotNone(pricing)
        self.assertEqual(pricing.input_per_mtok, 0.0)

    def test_anthropic_prefix_match_with_date_suffix(self):
        # Real Anthropic model IDs include date suffixes.
        pricing = get_pricing("claude-sonnet-4-5-20250929")
        self.assertIsNotNone(pricing)
        self.assertEqual(pricing.input_per_mtok, 3.00)

    def test_longest_prefix_wins(self):
        # claude-opus-4-7 should match the more specific entry, not claude-opus-4.
        pricing = get_pricing("claude-opus-4-7-20260101")
        self.assertIsNotNone(pricing)
        self.assertEqual(pricing.input_per_mtok, 15.00)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(get_pricing("not-a-real-model-xyz"))

    def test_empty_returns_none(self):
        self.assertIsNone(get_pricing(""))


class ComputeCostUsdTest(unittest.TestCase):
    def test_known_model_computes(self):
        # claude-sonnet-4-5 = $3/M input, $15/M output
        cost = compute_cost_usd("claude-sonnet-4-5", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 18.00, places=6)

    def test_partial_million(self):
        # 100k input + 50k output on sonnet pricing
        cost = compute_cost_usd("claude-sonnet-4-5", 100_000, 50_000)
        self.assertAlmostEqual(cost, 0.30 + 0.75, places=6)

    def test_ollama_is_free(self):
        self.assertEqual(compute_cost_usd("ollama", 999_999, 999_999), 0.0)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(compute_cost_usd("nope", 1000, 1000))

    def test_zero_tokens_returns_zero(self):
        self.assertEqual(compute_cost_usd("claude-sonnet-4-5", 0, 0), 0.0)


if __name__ == "__main__":
    unittest.main()
