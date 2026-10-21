"""Выбор рационального способа защиты g* = arg max Q_g(S) при C(g) ≤ C_доп —
задача 3 (слайд 8 презентации, ЧНЗ №4 слайда 9).

Каждый вариант защиты g порождает из базовой системы производную
(переопределённые параметры узлов, добавленные связи), для которой оценка
Q_g(S) выполняется тем же аппаратом, что и Q(S) (survivability/system_calc.py).
Для сравнения вариантов все оценки Монте-Карло используют общий поток
случайных чисел: различия оценок тогда отражают различия вариантов, а не
случайность разных потоков (оценки зависимы — сравнение консервативно,
что и требуется при принятии решения).

Вывод формул — в doc/formulas.tex (раздел «Выбор рационального способа
защиты»).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from .model import (ProtectionInput, ProtectionVariant, SystemInput,
                    ValidationError)
from .system_calc import SystemEstimate, system_qs


def apply_variant(system: SystemInput, variant: ProtectionVariant) -> SystemInput:
    """Система с применённым вариантом защиты g: переопределённые узлы
    и расширенный список связей. Валидация — конструктором SystemInput."""
    variant.validate_against(system)
    nodes = tuple(
        replace(
            node,
            own_failure_rate=variant.node_overrides[node.name].own_failure_rate
            if node.name in variant.node_overrides
            and variant.node_overrides[node.name].own_failure_rate is not None
            else node.own_failure_rate,
            recovery_rate=variant.node_overrides[node.name].recovery_rate
            if node.name in variant.node_overrides
            and variant.node_overrides[node.name].recovery_rate is not None
            else node.recovery_rate,
            impacts=variant.node_overrides[node.name].impacts
            if node.name in variant.node_overrides
            and variant.node_overrides[node.name].impacts is not None
            else node.impacts,
        )
        for node in system.nodes
    )
    return SystemInput(
        name=f"{system.name} [{variant.name}]",
        nodes=nodes,
        links=system.links + variant.links_added,
        w_def=system.w_def,
        target=system.target,
        monte_carlo_runs=system.monte_carlo_runs,
        seed=system.seed,
    )


@dataclass(frozen=True)
class VariantEstimate:
    """Оценка живучести системы с одним вариантом защиты."""

    name: str
    cost: float
    feasible: bool  # C(g) ≤ C_доп
    estimate: SystemEstimate


@dataclass(frozen=True)
class ProtectionResult:
    """Результат сравнения вариантов защиты."""

    budget: float                           # C_доп
    base: SystemEstimate                    # Q_base(S) — система без защиты
    rows: tuple[VariantEstimate, ...]       # в порядке следования во входных данных
    chosen: VariantEstimate                 # g*
    delta_q: float                          # Q_{g*}(S) − Q_base(S)
    relative_delta: float                   # ΔQ / Q_base (0, если Q_base = 0)
    cost_efficiency: float                  # ΔQ / C(g*) (0, если C(g*) = 0)


def compare_protection(system: SystemInput, protection: ProtectionInput,
                       seed: int | None = None) -> ProtectionResult:
    """Сравнение вариантов защиты и выбор рационального g*.

    Оценки выполняются последовательно над одним генератором rng
    (общий поток случайных чисел для Монте-Карло): базовая система, затем
    варианты в порядке их следования во входных данных. При равенстве Q_g
    выбирается вариант с меньшей стоимостью, при равенстве стоимостей —
    лексикографически меньшее имя (детерминированный выбор).
    """
    actual_seed = seed if seed is not None else system.seed
    rng = random.Random(actual_seed)
    for variant in protection.variants:
        variant.validate_against(system)

    base = system_qs(system, rng=rng)
    rows = tuple(
        VariantEstimate(
            name=variant.name,
            cost=variant.cost,
            feasible=variant.cost <= protection.budget,
            estimate=system_qs(apply_variant(system, variant), rng=rng),
        )
        for variant in protection.variants
    )
    feasible_rows = [row for row in rows if row.feasible]
    if not feasible_rows:
        costs = ", ".join(f"{row.name}: {row.cost:g}" for row in rows)
        raise ValidationError(
            f"Нет допустимых вариантов защиты: бюджет C_доп = {protection.budget:g}, "
            f"стоимости вариантов: {costs}"
        )
    chosen = sorted(feasible_rows,
                    key=lambda row: (-row.estimate.q, row.cost, row.name))[0]
    delta_q = chosen.estimate.q - base.q
    result = ProtectionResult(
        budget=protection.budget,
        base=base,
        rows=rows,
        chosen=chosen,
        delta_q=delta_q,
        relative_delta=delta_q / base.q if base.q > 0.0 else 0.0,
        cost_efficiency=delta_q / chosen.cost if chosen.cost > 0.0 else 0.0,
    )
    return result
