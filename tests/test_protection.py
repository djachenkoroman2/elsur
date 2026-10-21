"""Модульные тесты выбора способа защиты g* (стандартная библиотека unittest)."""

import math
import unittest

from survivability.calc import survivability
from survivability.io import job_from_json, protection_from_dict
from survivability.model import (Impact, NodeInput, NodeOverride,
                                 ProtectionInput, ProtectionVariant,
                                 SystemInput, ValidationError)
from survivability.protection import apply_variant, compare_protection
from survivability.system_calc import system_qs


def make_node(name, own=0.0, impacts=(), mu=0.0, period=1.0):
    return NodeInput(name, own, tuple(impacts), mu, period)


def make_chain(impacts=(), w_def=1.0 / 3.0, q_hint=None, **kwargs):
    """Цепочка 1–2–3; при q_hint узлы строятся с Q = q_hint (экспонента)."""
    if q_hint is not None:
        rate = -math.log(q_hint)
        nodes = tuple(make_node(str(i + 1), own=rate) for i in range(3))
    else:
        nodes = tuple(make_node(str(i + 1), own=0.0, impacts=impacts)
                      for i in range(3))
    return SystemInput("тест", nodes, (("1", "2"), ("2", "3")), w_def,
                       "connectivity", **kwargs)


def no_op_variant(name, cost=0.0):
    return ProtectionVariant(name, cost, {})


class TestApplyVariant(unittest.TestCase):
    def test_override_replaces_only_given_fields(self):
        base_impact = Impact("DDoS", 1.0, 0.5)
        system = SystemInput(
            "тест",
            (make_node("А", own=0.01, impacts=(base_impact,), mu=2.0),),
            (), 0.5, "node_fraction",
        )
        new_impact = Impact("DDoS (с экранированием)", 1.0, 0.1)
        variant = ProtectionVariant(
            "экранирование", 10.0,
            {"А": NodeOverride(impacts=(new_impact,))},
        )
        changed = apply_variant(system, variant)
        node = changed.nodes[0]
        self.assertEqual(node.impacts, (new_impact,))
        # Не заданные поля сохранены
        self.assertEqual(node.own_failure_rate, 0.01)
        self.assertEqual(node.recovery_rate, 2.0)
        # Базовая система не мутирована
        self.assertEqual(system.nodes[0].impacts, (base_impact,))

    def test_links_added(self):
        system = make_chain()
        variant = ProtectionVariant("резерв", 5.0, {}, (("1", "3"),))
        changed = apply_variant(system, variant)
        self.assertEqual(len(changed.links), 3)

    def test_added_link_duplicating_existing_rejected(self):
        system = make_chain()
        variant = ProtectionVariant("дубль", 5.0, {}, (("2", "1"),))
        with self.assertRaises(ValidationError):
            apply_variant(system, variant)


class TestCompareAnalytic(unittest.TestCase):
    def test_variant_lowering_defeat_probability(self):
        # Двухузловая система, node_fraction, W_доп = 1 ⇒ Q(S) = Q1·Q2.
        # Вариант снижает p атаки на узел 1: Q1 растёт, Q_g совпадает с эталоном.
        attack = Impact("атака", 1.0, 0.6)
        system = SystemInput(
            "тест",
            (make_node("1", own=0.3, impacts=(attack,)),
             make_node("2", own=0.2)),
            (), 1.0, "node_fraction",
        )
        q1_base = survivability(system.nodes[0])
        q2 = survivability(system.nodes[1])
        q_base = system_qs(system).q
        self.assertAlmostEqual(q_base, q1_base * q2)

        screened = Impact("атака", 1.0, 0.1)
        protection = ProtectionInput(100.0, (
            ProtectionVariant("экранирование", 25.0,
                              {"1": NodeOverride(impacts=(screened,))}),
        ))
        result = compare_protection(system, protection)
        q1_new = survivability(apply_variant(system, protection.variants[0]).nodes[0])
        self.assertGreater(q1_new, q1_base)
        self.assertAlmostEqual(result.rows[0].estimate.q, q1_new * q2)
        self.assertAlmostEqual(result.delta_q, q1_new * q2 - q_base)
        self.assertAlmostEqual(result.cost_efficiency,
                               (q1_new * q2 - q_base) / 25.0)
        self.assertEqual(result.chosen.name, "экранирование")

    def test_backup_link_analytics(self):
        # Цепочка 1–2–3, W_доп = 1/3, равные Q = q.
        # База: есть связная пара ⇔ P = 2q² − q³.
        # Вариант «резервный канал 1–3»: хотя бы два узла ⇔ P = 3q² − 2q³.
        q = 0.8
        system = make_chain(q_hint=q)
        base_expected = 2 * q * q - q ** 3
        variant_expected = 3 * q * q - 2 * q ** 3
        self.assertAlmostEqual(system_qs(system).q, base_expected)

        protection = ProtectionInput(100.0, (
            ProtectionVariant("резервный канал 1–3", 40.0, {}, (("1", "3"),)),
        ))
        result = compare_protection(system, protection)
        self.assertAlmostEqual(result.rows[0].estimate.q, variant_expected)
        self.assertAlmostEqual(result.delta_q, variant_expected - base_expected)
        self.assertAlmostEqual(result.relative_delta,
                               (variant_expected - base_expected) / base_expected)


class TestBudget(unittest.TestCase):
    def test_expensive_best_variant_rejected(self):
        # Лучший по Q вариант вне бюджета ⇒ выбирается допустимый, хотя он хуже
        q = 0.8
        system = make_chain(q_hint=q)
        ideal = {name: NodeOverride(own_failure_rate=0.0)
                 for name in ("1", "2", "3")}
        protection = ProtectionInput(
            30.0,
            (ProtectionVariant("полное резервирование", 40.0, ideal),
             ProtectionVariant("резервный канал 1–3", 20.0, {}, (("1", "3"),))),
        )
        result = compare_protection(system, protection)
        by_name = {row.name: row for row in result.rows}
        self.assertFalse(by_name["полное резервирование"].feasible)
        self.assertTrue(by_name["резервный канал 1–3"].feasible)
        self.assertEqual(result.chosen.name, "резервный канал 1–3")
        # Дорогой вариант действительно лучший: Q = 1 против 3q² − 2q³
        self.assertAlmostEqual(by_name["полное резервирование"].estimate.q, 1.0)
        self.assertAlmostEqual(by_name["резервный канал 1–3"].estimate.q,
                               3 * q * q - 2 * q ** 3)
        self.assertLess(result.delta_q, 1.0 - system_qs(system).q)

    def test_budget_filters_and_chooses_cheaper(self):
        q = 0.8
        system = make_chain(q_hint=q)
        protection = ProtectionInput(
            30.0,
            (ProtectionVariant("усиление узла 2", 10.0,
                               {"2": NodeOverride(own_failure_rate=0.0)}),
             ProtectionVariant("дорогой резерв", 40.0, {}, (("1", "3"),))),
        )
        result = compare_protection(system, protection)
        by_name = {row.name: row for row in result.rows}
        self.assertFalse(by_name["дорогой резерв"].feasible)
        self.assertTrue(by_name["усиление узла 2"].feasible)
        self.assertEqual(result.chosen.name, "усиление узла 2")
        # Узел 2 идеален ⇒ база с ним: P(пары (1,2) или (2,3)) = 2q − q²
        expected = 2 * q - q * q
        self.assertAlmostEqual(by_name["усиление узла 2"].estimate.q, expected)

    def test_empty_feasible_set_raises(self):
        system = make_chain(q_hint=0.8)
        protection = ProtectionInput(
            5.0,
            (ProtectionVariant("вариант А", 40.0, {}),
             ProtectionVariant("вариант Б", 60.0, {})),
        )
        with self.assertRaises(ValidationError) as ctx:
            compare_protection(system, protection)
        self.assertIn("C_доп", str(ctx.exception))
        self.assertIn("40", str(ctx.exception))


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_result_monte_carlo(self):
        # n = 21 ⇒ Монте-Карло; общий поток случайных чисел для всех оценок
        rate = -math.log(0.8)
        nodes = tuple(make_node(f"узел-{i}", own=rate) for i in range(21))
        system = SystemInput("тест", nodes, (), 0.5, "node_fraction",
                             monte_carlo_runs=4000, seed=777)
        protection = ProtectionInput(100.0, (
            ProtectionVariant("половина идеальна", 10.0,
                              {"узел-0": NodeOverride(own_failure_rate=0.0)}),
            ProtectionVariant("все идеальны", 50.0,
                              {name: NodeOverride(own_failure_rate=0.0)
                               for name in (n.name for n in nodes)}),
        ))
        first = compare_protection(system, protection)
        second = compare_protection(system, protection)
        self.assertEqual(first.chosen.name, second.chosen.name)
        self.assertEqual([row.estimate.q for row in first.rows],
                         [row.estimate.q for row in second.rows])
        self.assertEqual(first.base.q, second.base.q)
        self.assertEqual(first.rows[0].estimate.ci, second.rows[0].estimate.ci)

    def test_tie_break_by_cost_then_name(self):
        q = 0.8
        system = make_chain(q_hint=q)
        protection = ProtectionInput(100.0, (
            ProtectionVariant("б", 30.0, {}),
            ProtectionVariant("а", 30.0, {}),  # тот же эффект, меньшее имя
        ))
        result = compare_protection(system, protection)
        self.assertEqual(result.chosen.name, "а")


class TestProtectionValidation(unittest.TestCase):
    def _system(self):
        return make_chain(q_hint=0.8)

    def test_negative_cost(self):
        with self.assertRaises(ValidationError):
            ProtectionVariant("плохой", -1.0, {})

    def test_negative_budget(self):
        variant = no_op_variant("г")
        with self.assertRaises(ValidationError):
            ProtectionInput(-5.0, (variant,))

    def test_empty_variants(self):
        with self.assertRaises(ValidationError):
            ProtectionInput(10.0, ())

    def test_duplicate_variant_names(self):
        with self.assertRaises(ValidationError):
            ProtectionInput(10.0, (no_op_variant("г"), no_op_variant("г")))

    def test_override_unknown_node(self):
        variant = ProtectionVariant("плохой", 1.0,
                                    {"неизвестный": NodeOverride(own_failure_rate=0.0)})
        with self.assertRaises(ValidationError):
            variant.validate_against(self._system())

    def test_links_added_loop(self):
        with self.assertRaises(ValidationError):
            ProtectionVariant("петля", 1.0, {}, (("1", "1"),))

    def test_links_added_unknown_endpoint(self):
        variant = ProtectionVariant("плохой", 1.0, {}, (("1", "X"),))
        with self.assertRaises(ValidationError):
            variant.validate_against(self._system())


class TestProtectionJson(unittest.TestCase):
    def test_roundtrip(self):
        data = {
            "budget": 50,
            "variants": [
                {"name": "экранирование", "cost": 25,
                 "nodes": {"1": {"impacts": [
                     {"name": "DDoS", "intensity": 2.0,
                      "defeat_probability": 0.1}]}}},
                {"name": "резерв", "cost": 40, "links_added": [["1", "3"]]},
            ],
        }
        protection = protection_from_dict(data, make_chain(q_hint=0.8))
        self.assertEqual(protection.budget, 50.0)
        self.assertEqual(len(protection.variants), 2)
        variant = protection.variants[0]
        self.assertEqual(variant.name, "экранирование")
        self.assertEqual(variant.node_overrides["1"].impacts[0].defeat_probability, 0.1)
        self.assertEqual(protection.variants[1].links_added, (("1", "3"),))

    def test_unknown_node_in_json(self):
        data = {"budget": 50, "variants": [
            {"name": "плохой", "cost": 10, "nodes": {"X": {"own_failure_rate": 0.1}}}]}
        with self.assertRaises(ValidationError):
            protection_from_dict(data, make_chain(q_hint=0.8))

    def test_probability_out_of_range_in_json(self):
        data = {"budget": 50, "variants": [
            {"name": "плохой", "cost": 10,
             "nodes": {"1": {"impacts": [
                 {"name": "DDoS", "intensity": 1.0,
                  "defeat_probability": 1.5}]}}}]}
        with self.assertRaises(ValidationError):
            protection_from_dict(data, make_chain(q_hint=0.8))

    def test_job_with_protection_dispatched(self):
        import json
        import tempfile
        from pathlib import Path
        payload = {
            "system": {
                "name": "система с защитой",
                "nodes": [{"name": "А", "own_failure_rate": 0.1, "period": 1.0,
                           "impacts": []}],
                "w_def": 0.5,
            },
            "protection": {"budget": 10, "variants": [
                {"name": "вариант", "cost": 5}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            job = job_from_json(path)
        self.assertIsNotNone(job.system)
        self.assertIsNotNone(job.protection)
        self.assertEqual(job.protection.budget, 10.0)


if __name__ == "__main__":
    unittest.main()
