"""Модульные тесты оценки живучести системы Q(S) (стандартная библиотека unittest)."""

import math
import random
import unittest

from survivability.io import job_from_json, system_from_dict
from survivability.model import (NodeInput, SystemInput, ValidationError)
from survivability.system_calc import (EXACT_MAX_NODES, IMPORTANCE_RUNS,
                                       node_importance, qs_exact,
                                       system_qs, wilson_interval,
                                       w_connectivity, w_node_fraction)


def make_node_q(name, q, period=1.0):
    """Узел с заданной живучестью Q = e^(−λT) (без восстановления, T = period)."""
    rate = -math.log(q) if 0.0 < q < 1.0 else (0.0 if q == 1.0 else None)
    if rate is None:
        raise ValueError("q=0 недостижимо конечной интенсивностью; используйте qs_exact с явным q")
    return NodeInput(name, rate, (), 0.0, period)


def make_chain(q=(0.9, 0.8, 0.7), w_def=1.0, target="connectivity",
               links=(("1", "2"), ("2", "3")), **kwargs):
    nodes = tuple(make_node_q(f"{i + 1}", q[i]) for i in range(len(q)))
    return SystemInput("тест", nodes, tuple(links), w_def, target, **kwargs)


class TestWilsonInterval(unittest.TestCase):
    def test_known_value(self):
        # p = 0.5, n = 100 (z = 1.959963984540054)
        lo, hi = wilson_interval(50, 100)
        self.assertAlmostEqual(lo, 0.4038315303659956, places=12)
        self.assertAlmostEqual(hi, 0.5961684696340044, places=12)

    def test_all_successes_clamped_to_one(self):
        lo, hi = wilson_interval(100, 100)
        self.assertEqual(hi, 1.0)
        self.assertGreater(lo, 0.96)

    def test_no_runs(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))


class TestEffectivenessFunctions(unittest.TestCase):
    def test_node_fraction_counts_bits(self):
        self.assertEqual(w_node_fraction(0b101, [], 3), 2 / 3)

    def test_connectivity_single_node(self):
        self.assertEqual(w_connectivity(1, [], 1), 1.0)
        self.assertEqual(w_connectivity(0, [], 1), 0.0)

    def test_connectivity_components(self):
        # Компоненты {0,1} и {2}: C(2,2)/C(3,2) = 1/3
        adj = [[1], [0], []]
        self.assertAlmostEqual(w_connectivity(0b011, adj, 3), 1 / 3)
        # Полный граф: все три узла в одной компоненте
        full = [[1, 2], [0, 2], [0, 1]]
        self.assertAlmostEqual(w_connectivity(0b111, full, 3), 1.0)
        self.assertEqual(w_connectivity(0b000, adj, 3), 0.0)


class TestExactEnumeration(unittest.TestCase):
    def test_series_two_nodes_w_def_one(self):
        # W_доп = 1 ⇒ требуются оба узла ⇒ Q(S) = Q1·Q2
        system = make_chain(q=(0.9, 0.8), w_def=1.0, target="node_fraction",
                            links=())
        estimate = system_qs(system)
        self.assertEqual(estimate.method, "exact")
        self.assertAlmostEqual(estimate.q, 0.9 * 0.8)

    def test_parallel_two_nodes_w_def_half(self):
        # W_доп = 1/2 ⇒ достаточно одного узла ⇒ Q(S) = 1 − (1−Q1)(1−Q2)
        system = make_chain(q=(0.9, 0.8), w_def=0.5, target="node_fraction",
                            links=())
        self.assertAlmostEqual(system_qs(system).q, 1.0 - 0.1 * 0.2)

    def test_connectivity_chain_w_def_one(self):
        # Цепочка 1–2–3, W_доп = 1 ⇒ все три узла работоспособны
        system = make_chain(q=(0.9, 0.8, 0.7), w_def=1.0)
        self.assertAlmostEqual(system_qs(system).q, 0.9 * 0.8 * 0.7)

    def test_connectivity_chain_w_def_one_third(self):
        # Цепочка 1–2–3, W_доп = 1/3 ⇒ существует связная пара ⇒
        # P = P(1∧2) + P(2∧3) − P(1∧2∧3) = 2q² − q³
        q = 0.8
        system = make_chain(q=(q, q, q), w_def=1.0 / 3.0)
        self.assertAlmostEqual(system_qs(system).q, 2 * q * q - q ** 3)

    def test_connectivity_triangle_w_def_one_third(self):
        # Треугольник, W_доп = 1/3 ⇒ хотя бы два узла работоспособны ⇒
        # P = 3q²(1−q) + q³ = 3q² − 2q³
        q = 0.8
        system = make_chain(q=(q, q, q), w_def=1.0 / 3.0,
                            links=(("1", "2"), ("2", "3"), ("1", "3")))
        self.assertAlmostEqual(system_qs(system).q, 3 * q * q - 2 * q ** 3)

    def test_enumeration_matches_poisson_binomial(self):
        # Независимая сверка: для node_fraction Q(S) — хвост распределения
        # Пуассона–Бернулли (считается динамическим программированием)
        q = [0.9, 0.7, 0.85, 0.4, 0.95, 0.6, 0.75, 0.5, 0.8, 0.65, 0.55, 0.9]
        system = make_chain(q=tuple(q), w_def=7.0 / 12.0,
                            target="node_fraction", links=())
        estimate = qs_exact(system, list(q))
        # DP: распределение числа работоспособных узлов
        dp = [1.0]
        for prob in q:
            new = [0.0] * (len(dp) + 1)
            for successes, p in enumerate(dp):
                new[successes] += p * (1.0 - prob)
                new[successes + 1] += p * prob
            dp = new
        need = math.ceil(7.0 / 12.0 * 12)
        reference = sum(dp[need:])
        self.assertAlmostEqual(estimate.q, reference, places=12)


class TestDegenerateCases(unittest.TestCase):
    def test_all_nodes_perfect(self):
        system = make_chain(q=(1.0, 1.0, 1.0), w_def=1.0)
        self.assertEqual(system_qs(system).q, 1.0)

    def test_all_nodes_down(self):
        # q = 0 достигается пределом; проверяем qs_exact с явным вектором q
        system = make_chain(q=(0.9, 0.8, 0.7), w_def=0.4)
        self.assertEqual(qs_exact(system, [0.0, 0.0, 0.0]).q, 0.0)

    def test_all_nodes_down_w_def_zero(self):
        # Порог нулевой: система «работает» даже в полном отказе
        system = make_chain(q=(0.9, 0.8, 0.7), w_def=0.0)
        self.assertEqual(qs_exact(system, [0.0, 0.0, 0.0]).q, 1.0)

    def test_empty_links_connectivity(self):
        # Нет связей ⇒ нет связных пар (n ≥ 2)
        system = make_chain(q=(0.9, 0.8), w_def=1e-9, links=())
        self.assertEqual(system_qs(system).q, 0.0)


class TestMonteCarlo(unittest.TestCase):
    def test_method_selected_by_size(self):
        q = tuple([0.8] * (EXACT_MAX_NODES + 1))
        system = make_chain(q=q, w_def=0.5, target="node_fraction",
                            links=(), monte_carlo_runs=2000, seed=7)
        estimate = system_qs(system)
        self.assertEqual(estimate.method, "monte_carlo")
        self.assertEqual(estimate.runs, 2000)
        self.assertIsNotNone(estimate.ci)
        self.assertLessEqual(estimate.ci[0], estimate.q)
        self.assertGreaterEqual(estimate.ci[1], estimate.q)

    def test_ci_covers_exact_reference_at_n22(self):
        # n = 22 > предела перебора; эталон — хвост распределения
        # Пуассона–Бернулли (независимый от реализации способ)
        q = [0.95, 0.6, 0.8, 0.7, 0.9, 0.5, 0.85, 0.75,
             0.65, 0.9, 0.55, 0.8, 0.7, 0.95, 0.6, 0.85,
             0.75, 0.9, 0.5, 0.8, 0.65, 0.7]
        w_def = 0.5
        system = make_chain(q=tuple(q), w_def=w_def, target="node_fraction",
                            links=(), monte_carlo_runs=50_000, seed=12345)
        estimate = system_qs(system)
        dp = [1.0]
        for prob in q:
            new = [0.0] * (len(dp) + 1)
            for successes, p in enumerate(dp):
                new[successes] += p * (1.0 - prob)
                new[successes + 1] += p * prob
            dp = new
        need = math.ceil(w_def * len(q))
        reference = sum(dp[need:])
        lo, hi = estimate.ci
        self.assertGreaterEqual(reference, lo - 1e-12)
        self.assertLessEqual(reference, hi + 1e-12)

    def test_reproducible_with_seed(self):
        q = tuple([0.8] * 21)
        base = dict(q=q, w_def=0.5, target="node_fraction", links=(),
                    monte_carlo_runs=3000, seed=42)
        first = system_qs(make_chain(**base))
        second = system_qs(make_chain(**base))
        self.assertEqual(first.q, second.q)
        self.assertEqual(first.successes, second.successes)

    def test_external_rng_is_consumed(self):
        # Переданный генератор используется (общий поток для сравнения вариантов)
        q = tuple([0.8] * 21)
        system = make_chain(q=q, w_def=0.5, target="node_fraction", links=(),
                            monte_carlo_runs=2000)
        rng = random.Random(5)
        first = system_qs(system, rng=rng)
        self.assertGreaterEqual(first.q, 0.0)
        self.assertLessEqual(first.q, 1.0)


class TestNodeImportance(unittest.TestCase):
    def test_two_nodes_series_birnbaum(self):
        # W_доп = 1: I_1 = Q(S|x_1=1) − Q(S|x_1=0) = Q2 − 0 = Q2
        q1, q2 = 0.9, 0.8
        system = make_chain(q=(q1, q2), w_def=1.0, target="node_fraction",
                            links=())
        estimate = system_qs(system)
        importance = {imp.name: imp for imp in node_importance(system, estimate)}
        self.assertAlmostEqual(importance["1"].criticality, q2)
        self.assertAlmostEqual(importance["2"].criticality, q1)
        self.assertAlmostEqual(importance["1"].improvement, q2 - q1 * q2)

    def test_perfect_node_has_zero_improvement(self):
        # Q_2 = 1: отказ узла 2 невозможен ⇒ потенциал повышения Q(S|x=1)−Q(S) = 0;
        # критичность Бирнбаума — контрфактическая величина («что было бы, если бы
        # идеальный узел отказал») и для него сохраняется: Q(S|x_2=1) − Q(S|x_2=0)
        # = Q1 − 0 = Q1
        q1 = 0.9
        system = make_chain(q=(q1, 1.0), w_def=1.0, target="node_fraction",
                            links=())
        estimate = system_qs(system)
        importance = {imp.name: imp for imp in node_importance(system, estimate)}
        self.assertAlmostEqual(importance["2"].improvement, 0.0, places=12)
        self.assertAlmostEqual(importance["2"].criticality, q1)

    def test_monte_carlo_importance_smoke(self):
        # n = 21 ⇒ Монте-Карло; диапазоны значений и детерминизм
        q = tuple([0.8] * 21)
        base = dict(q=q, w_def=0.5, target="node_fraction", links=(),
                    monte_carlo_runs=1000, seed=11)
        system = make_chain(**base)
        estimate = system_qs(system)
        importance = node_importance(system, estimate)
        self.assertEqual(len(importance), 21)
        for imp in importance:
            self.assertGreaterEqual(imp.criticality, -1e-9)
            self.assertLessEqual(imp.criticality, 1.0 + 1e-9)
        system2 = make_chain(**base)
        importance2 = node_importance(system2, system_qs(system2))
        self.assertEqual([i.criticality for i in importance],
                         [i.criticality for i in importance2])
        self.assertGreater(IMPORTANCE_RUNS, 0)


class TestSystemValidation(unittest.TestCase):
    def test_empty_nodes(self):
        with self.assertRaises(ValidationError):
            SystemInput("тест", (), (), 0.5)

    def test_duplicate_node_names(self):
        nodes = (make_node_q("a", 0.9), make_node_q("a", 0.8))
        with self.assertRaises(ValidationError):
            SystemInput("тест", nodes, (), 0.5)

    def test_w_def_out_of_range(self):
        with self.assertRaises(ValidationError):
            make_chain(w_def=1.5)

    def test_unknown_target(self):
        with self.assertRaises(ValidationError):
            make_chain(target="no_such_target")

    def test_link_unknown_node(self):
        with self.assertRaises(ValidationError):
            make_chain(links=(("1", "негоций"),))

    def test_self_loop(self):
        with self.assertRaises(ValidationError):
            make_chain(links=(("1", "1"),))

    def test_duplicate_link_undirected(self):
        with self.assertRaises(ValidationError):
            make_chain(links=(("1", "2"), ("2", "1")))

    def test_non_positive_monte_carlo_runs(self):
        with self.assertRaises(ValidationError):
            make_chain(monte_carlo_runs=0)


class TestSystemJson(unittest.TestCase):
    def test_roundtrip(self):
        data = {
            "name": "система-1",
            "nodes": [
                {"name": "А", "own_failure_rate": 0.01, "period": 1.0,
                 "impacts": [{"name": "DDoS", "intensity": 1.0,
                              "defeat_probability": 0.3}]},
                {"name": "Б", "own_failure_rate": 0.02, "period": 1.0,
                 "impacts": []},
            ],
            "links": [["А", "Б"]],
            "w_def": 0.5,
            "target": "connectivity",
            "monte_carlo_runs": 1000,
            "seed": 3,
        }
        system = system_from_dict(data)
        self.assertEqual(system.name, "система-1")
        self.assertEqual(len(system.nodes), 2)
        self.assertEqual(system.links, (("А", "Б"),))
        self.assertEqual(system.w_def, 0.5)
        self.assertEqual(system.target, "connectivity")
        self.assertEqual(system.monte_carlo_runs, 1000)
        self.assertEqual(system.seed, 3)

    def test_node_without_name_rejected(self):
        with self.assertRaises(ValidationError):
            system_from_dict({"nodes": [{"own_failure_rate": 0.1, "period": 1.0,
                                         "impacts": []}], "w_def": 0.5})

    def test_missing_w_def(self):
        with self.assertRaises(ValidationError):
            system_from_dict({"nodes": [{"name": "А", "own_failure_rate": 0.1,
                                         "period": 1.0, "impacts": []}]})

    def test_bad_link_shape(self):
        with self.assertRaises(ValidationError):
            system_from_dict({
                "nodes": [{"name": "А", "own_failure_rate": 0.1, "period": 1.0,
                           "impacts": []}], "w_def": 0.5, "links": [["А"]],
            })


class TestLegacyExamples(unittest.TestCase):
    """Обратная совместимость: старые файлы примеров — это одиночные узлы."""

    def test_all_legacy_examples_parse_as_node_jobs(self):
        import pathlib
        examples = sorted(pathlib.Path("examples").glob("example*.json"))
        self.assertGreaterEqual(len(examples), 5)
        for path in examples:
            if "system" in path.name or "variants" in path.name:
                continue  # новые примеры задач 2–3
            with self.subTest(path=path.name):
                job = job_from_json(path)
                self.assertIsNotNone(job.node)
                self.assertIsNone(job.system)


if __name__ == "__main__":
    unittest.main()
