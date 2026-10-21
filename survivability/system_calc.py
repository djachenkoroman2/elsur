"""Оценка живучести системы Q(S) = P(W(X) ≥ W_доп) — задача 2.

Живучести узлов Q_i берутся из задачи 1 (survivability/calc.py), векторы
состояний X ∈ Ω = {0, 1}^n считаются независимыми. Два способа расчёта:

- точный перебор всех 2^n векторов состояний (при n ≤ EXACT_MAX_NODES);
- метод Монте-Карло с доверительным интервалом Уилсона (при n > EXACT_MAX_NODES).

Вывод формул — в doc/formulas.tex (раздел «Оценка живучести системы»).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable

from .calc import survivability
from .model import (SYSTEM_TARGET_CONNECTIVITY, SYSTEM_TARGET_NODE_FRACTION,
                    SystemInput)

#: Предел точного перебора: 2^20 ≈ 1.0e6 векторов состояний — перебор
#: занимает секунды; уже при n = 21 объём вдвое больше и время перебора
#: становится неприемлемым для интерактивного расчёта, поэтому далее —
#: Монте-Карло.
EXACT_MAX_NODES = 20

#: Число условных прогонов для оценки важности узлов в режиме Монте-Карло.
#: Важность — вспомогательная диагностика, поэтому точность на порядок
#: ниже, чем у оценки Q(S), зато суммарные затраты на 2·n оценок W(X)
#: на n = 22 остаются секундами, а не минутами.
IMPORTANCE_RUNS = 2_000

#: 97.5%-квантиль N(0, 1): доверительная вероятность интервала Уилсона 95%.
WILSON_Z = 1.959963984540054

#: Вердикт по вектору состояний: W(X) ≥ W_доп.
Effectiveness = Callable[[int, list[list[int]], int], float]


def node_survivabilities(system: SystemInput) -> list[float]:
    """Живучести узлов системы Q_i за период T (задача 1)."""
    return [survivability(node) for node in system.nodes]


def build_adjacency(system: SystemInput) -> list[list[int]]:
    """Списки смежности графа связей в порядке следования узлов системы."""
    index = {node.name: i for i, node in enumerate(system.nodes)}
    adj: list[list[int]] = [[] for _ in system.nodes]
    for a, b in system.links:
        i, j = index[a], index[b]
        adj[i].append(j)
        adj[j].append(i)
    return adj


# ---------------------------------------------------------------------------
# Целевые функции W(X)
# ---------------------------------------------------------------------------

def w_node_fraction(mask: int, adj: list[list[int]], n: int) -> float:
    """Доля работоспособных узлов: W(X) = (1/n)·Σ_i x_i. Связи не учитываются."""
    return mask.bit_count() / n


def w_connectivity(mask: int, adj: list[list[int]], n: int) -> float:
    """Доля пар узлов, между которыми существует путь по работоспособным
    узлам и связям: W(X) = Σ_k C(s_k, 2) / C(n, 2), где s_k — размер k-й
    компоненты связности работоспособной части графа. При n = 1: W = x_1."""
    if n == 1:
        return 1.0 if mask & 1 else 0.0
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        if not (mask >> i) & 1:
            continue
        for j in adj[i]:
            if j > i and (mask >> j) & 1:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    sizes: dict[int, int] = {}
    for i in range(n):
        if (mask >> i) & 1:
            root = find(i)
            sizes[root] = sizes.get(root, 0) + 1
    pairs = sum(size * (size - 1) // 2 for size in sizes.values())
    total_pairs = n * (n - 1) // 2
    return pairs / total_pairs


TARGET_FUNCTIONS: dict[str, Effectiveness] = {
    SYSTEM_TARGET_NODE_FRACTION: w_node_fraction,
    SYSTEM_TARGET_CONNECTIVITY: w_connectivity,
}


# ---------------------------------------------------------------------------
# Оценка Q(S)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SystemEstimate:
    """Результат оценки живучести системы."""

    q: float
    method: str  # "exact" | "monte_carlo"
    runs: int | None = None
    successes: int | None = None
    ci: tuple[float, float] | None = None  # доверительный интервал Уилсона 95 %


def wilson_interval(successes: int, runs: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Доверительный интервал Уилсона для биномиальной доли, уровень 95 %."""
    if runs <= 0:
        return (0.0, 1.0)
    p = successes / runs
    z2 = z * z
    denom = 1.0 + z2 / runs
    center = (p + z2 / (2.0 * runs)) / denom
    half = z * math.sqrt(p * (1.0 - p) / runs + z2 / (4.0 * runs * runs)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def qs_exact(system: SystemInput, q: list[float]) -> SystemEstimate:
    """Точный перебор: Q(S) = Σ_{X: W(X) ≥ W_доп} P(X),
    P(X) = Π_i Q_i^{x_i} (1 − Q_i)^{1−x_i} (независимые узлы)."""
    n = len(q)
    adj = build_adjacency(system)
    effectiveness = TARGET_FUNCTIONS[system.target]
    w_def = system.w_def
    q0 = [1.0 - prob for prob in q]
    total = 0.0
    for mask in range(1 << n):
        p = 1.0
        for i in range(n):
            p *= q[i] if (mask >> i) & 1 else q0[i]
        if p and effectiveness(mask, adj, n) >= w_def:
            total += p
    return SystemEstimate(q=min(1.0, total), method="exact")


def qs_monte_carlo(system: SystemInput, q: list[float],
                   rng: random.Random) -> SystemEstimate:
    """Метод Монте-Карло: N независимых прогонов, состояние узла i —
    Бернулли с вероятностью Q_i; оценка Q̂(S) = N*/N с интервалом Уилсона."""
    n = len(q)
    adj = build_adjacency(system)
    effectiveness = TARGET_FUNCTIONS[system.target]
    w_def = system.w_def
    runs = system.monte_carlo_runs
    up_threshold = [1.0 - prob for prob in q]
    rand = rng.random
    successes = 0
    for _ in range(runs):
        mask = 0
        for i in range(n):
            if rand() >= up_threshold[i]:
                mask |= 1 << i
        if effectiveness(mask, adj, n) >= w_def:
            successes += 1
    return SystemEstimate(
        q=successes / runs,
        method="monte_carlo",
        runs=runs,
        successes=successes,
        ci=wilson_interval(successes, runs),
    )


def system_qs(system: SystemInput, q: list[float] | None = None,
              rng: random.Random | None = None) -> SystemEstimate:
    """Оценка Q(S) с автоматическим выбором способа: перебор при
    n ≤ EXACT_MAX_NODES, иначе Монте-Карло.

    rng — внешний генератор: передача одного экземпляра в несколько вызовов
    даёт общий поток случайных чисел (снижение дисперсии сравнения вариантов,
    см. survivability/protection.py).
    """
    if q is None:
        q = node_survivabilities(system)
    if len(system.nodes) <= EXACT_MAX_NODES:
        return qs_exact(system, q)
    if rng is None:
        rng = random.Random(system.seed)
    return qs_monte_carlo(system, q, rng)


# ---------------------------------------------------------------------------
# Важность узлов
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeImportance:
    """Важность узла для живучести системы (критичность отказа).

    q_if_up     — Q(S | x_i = 1): живучесть при гарантированно работающем узле;
    q_if_down   — Q(S | x_i = 0): живучесть при гарантированно отказавшем узле
                  (0, если состояние x_i = 0 невозможно при данном Q_i);
    criticality — критичность Бирнбаума I_i = Q(S|x_i=1) − Q(S|x_i=0):
                  доля состояний, в которых отказ узла i критичен;
    improvement — потенциал повышения Q(S|x_i=1) − Q(S): прирост живучести
                  от идеального узла.
    """

    name: str
    q_if_up: float
    q_if_down: float
    criticality: float
    improvement: float


def importance_exact(system: SystemInput, q: list[float],
                     q_base: float) -> list[NodeImportance]:
    """Важность узлов одним проходом перебора: накапливаются суммы
    S1_i = Σ_{X: x_i=1, W≥W_доп} P(X) и S0_i = Σ_{X: x_i=0, W≥W_доп} P(X),
    условные вероятности Q(S|x_i=·) = S·_i / P(x_i=·)."""
    n = len(q)
    adj = build_adjacency(system)
    effectiveness = TARGET_FUNCTIONS[system.target]
    w_def = system.w_def
    q0 = [1.0 - prob for prob in q]
    up = [0.0] * n
    down = [0.0] * n
    for mask in range(1 << n):
        p = 1.0
        for i in range(n):
            p *= q[i] if (mask >> i) & 1 else q0[i]
        if p and effectiveness(mask, adj, n) >= w_def:
            for i in range(n):
                if (mask >> i) & 1:
                    up[i] += p
                else:
                    down[i] += p
    return [
        NodeImportance(
            name=system.nodes[i].name,
            q_if_up=up[i] / q[i] if q[i] > 0.0 else 0.0,
            q_if_down=down[i] / (1.0 - q[i]) if q[i] < 1.0 else 0.0,
            criticality=(up[i] / q[i] if q[i] > 0.0 else 0.0)
                        - (down[i] / (1.0 - q[i]) if q[i] < 1.0 else 0.0),
            improvement=(up[i] / q[i] if q[i] > 0.0 else 0.0) - q_base,
        )
        for i in range(n)
    ]


def importance_monte_carlo(system: SystemInput, q: list[float],
                           q_base: float, rng: random.Random) -> list[NodeImportance]:
    """Важность узлов в режиме Монте-Карло: условные оценки по IMPORTANCE_RUNS
    прогонам состояний остальных узлов при принудительно работающем
    (x_i = 1) и принудительно отказавшем (x_i = 0) узле i."""
    n = len(q)
    adj = build_adjacency(system)
    effectiveness = TARGET_FUNCTIONS[system.target]
    w_def = system.w_def
    up_threshold = [1.0 - prob for prob in q]
    rand = rng.random
    result = []
    for i in range(n):
        bit_i = 1 << i
        up_ok = 0
        down_ok = 0
        for _ in range(IMPORTANCE_RUNS):
            mask = 0
            for j in range(n):
                if j != i and rand() >= up_threshold[j]:
                    mask |= 1 << j
            if effectiveness(mask | bit_i, adj, n) >= w_def:
                up_ok += 1
            if effectiveness(mask, adj, n) >= w_def:
                down_ok += 1
        q_if_up = up_ok / IMPORTANCE_RUNS
        q_if_down = down_ok / IMPORTANCE_RUNS
        result.append(NodeImportance(
            name=system.nodes[i].name,
            q_if_up=q_if_up,
            q_if_down=q_if_down,
            criticality=q_if_up - q_if_down,
            improvement=q_if_up - q_base,
        ))
    return result


def node_importance(system: SystemInput, estimate: SystemEstimate,
                    q: list[float] | None = None,
                    rng: random.Random | None = None) -> list[NodeImportance]:
    """Важность всех узлов системы (способ зависит от способа оценки Q(S))."""
    if q is None:
        q = node_survivabilities(system)
    if estimate.method == "exact":
        return importance_exact(system, q, estimate.q)
    if rng is None:
        rng = random.Random(system.seed)
    return importance_monte_carlo(system, q, estimate.q, rng)
