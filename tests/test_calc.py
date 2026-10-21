"""Модульные тесты расчётных формул (стандартная библиотека unittest)."""

import math
import unittest

from survivability.calc import (impact_contributions,
                                steady_state_availability, survivability)
from survivability.io import node_from_dict
from survivability.model import Impact, NodeInput, ValidationError


def make_node(own=0.0, impacts=(), mu=0.0, period=1.0):
    return NodeInput("тестовый узел", own, tuple(impacts), mu, period)


class TestSurvivability(unittest.TestCase):
    def test_no_failures_no_impacts(self):
        # Λ = 0 ⇒ узел не отказывает ⇒ Q = 1
        node = make_node()
        self.assertAlmostEqual(survivability(node), 1.0)

    def test_without_recovery_is_exponential(self):
        # Q(T) = e^(−ΛT); при Λ=2, T=0.5 ⇒ e^(−1)
        node = make_node(own=2.0, period=0.5)
        self.assertAlmostEqual(survivability(node), math.exp(-1.0))

    def test_impact_adds_hazard(self):
        # Λ = λ0 + λ·p = 1 + 2·0.5 = 2; без восстановления Q = e^(−2)
        node = make_node(own=1.0,
                         impacts=[Impact("атака", 2.0, 0.5)])
        self.assertAlmostEqual(survivability(node), math.exp(-2.0))

    def test_with_recovery_long_period_tends_to_steady_state(self):
        # При большом T Q(T) → K = μ/(Λ+μ)
        node = make_node(own=1.0, impacts=[Impact("атака", 1.0, 0.5)],
                         mu=4.0, period=1000.0)
        self.assertAlmostEqual(survivability(node),
                               steady_state_availability(node), places=6)

    def test_with_recovery_at_t_zero(self):
        # В начальный момент узел работоспособен ⇒ Q(0) = 1
        node = make_node(own=1.0, mu=2.0, period=1e-9)
        self.assertAlmostEqual(survivability(node), 1.0, places=6)

    def test_recovery_improves_survivability(self):
        node = make_node(own=1.0, period=1.0)
        self.assertGreater(survivability(make_node(own=1.0, mu=5.0, period=1.0)),
                           survivability(node))

    def test_steady_state_formula(self):
        # Λ = 3, μ = 6 ⇒ K = 6/9 = 2/3
        node = make_node(own=3.0, mu=6.0)
        self.assertAlmostEqual(steady_state_availability(node), 2.0 / 3.0)

    def test_zero_hazard_with_recovery(self):
        # Λ = 0 ⇒ узел не ломается ⇒ Q = 1 независимо от μ
        node = make_node(mu=3.0, period=10.0)
        self.assertAlmostEqual(survivability(node), 1.0)
        self.assertAlmostEqual(steady_state_availability(node), 1.0)


class TestContributions(unittest.TestCase):
    def test_shares_sum_to_one(self):
        node = make_node(own=1.0, impacts=[Impact("a", 2.0, 0.5),
                                           Impact("b", 4.0, 0.25)])
        contribs = impact_contributions(node)
        # λ0=1, a: λ·p=1, b: λ·p=1 ⇒ Λ=3, доли по 1/3
        self.assertEqual(len(contribs), 2)
        self.assertAlmostEqual(sum(c.share for c in contribs) +
                               node.own_failure_rate / node.total_hazard, 1.0)

    def test_zero_hazard_shares_are_zero(self):
        node = make_node(impacts=[Impact("a", 0.0, 0.5)])
        self.assertEqual(impact_contributions(node)[0].share, 0.0)


class TestValidation(unittest.TestCase):
    def test_negative_own_failure_rate(self):
        with self.assertRaises(ValidationError):
            make_node(own=-1.0)

    def test_probability_out_of_range(self):
        with self.assertRaises(ValidationError):
            Impact("атака", 1.0, 1.5)

    def test_non_positive_period(self):
        with self.assertRaises(ValidationError):
            make_node(period=0.0)

    def test_negative_recovery_rate(self):
        with self.assertRaises(ValidationError):
            make_node(mu=-0.5)


class TestJsonInput(unittest.TestCase):
    def test_roundtrip(self):
        data = {
            "name": "узел-1",
            "own_failure_rate": 0.01,
            "recovery_rate": 4.0,
            "period": 24.0,
            "impacts": [{"name": "DDoS", "intensity": 2.0,
                         "defeat_probability": 0.3}],
        }
        node = node_from_dict(data)
        self.assertEqual(node.name, "узел-1")
        self.assertEqual(node.total_hazard, 0.01 + 0.6)
        k = 4.0 / 4.61  # μ/(Λ+μ)
        self.assertAlmostEqual(survivability(node),
                               k + (1.0 - k) * math.exp(-4.61 * 24.0))

    def test_invalid_dict_raises(self):
        with self.assertRaises(ValidationError):
            node_from_dict({"own_failure_rate": 0.1})


if __name__ == "__main__":
    unittest.main()
