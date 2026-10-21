"""Расчётные формулы живучести узла.

Принята марковская модель «отказ–восстановление»: потоки собственных отказов
и атак — пуассоновские с суммарной интенсивностью Λ_i, восстановление —
экспоненциальное с интенсивностью μ. Подробный вывод формул — в doc/formulas.tex.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .model import Impact, NodeInput


def survivability(node: NodeInput) -> float:
    """Живучесть узла Q_i = P(x_i(T) = 1) — вероятность того, что узел
    работоспособен к концу периода T (начальное состояние — работоспособен).

    Без восстановления (μ = 0):        Q_i(T) = e^(−Λ_i·T)          — формула (5)
    С восстановлением (μ > 0):         Q_i(T) = μ/(Λ_i+μ) + Λ_i/(Λ_i+μ)·e^(−(Λ_i+μ)·T) — формула (6)

    Номера формул — по doc/formulas.tex.
    """
    hazard = node.total_hazard
    mu = node.recovery_rate
    if mu == 0.0:
        return math.exp(-hazard * node.period)
    k = mu / (hazard + mu)
    return k + (hazard / (hazard + mu)) * math.exp(-(hazard + mu) * node.period)


def steady_state_availability(node: NodeInput) -> float:
    """Предельный коэффициент готовности K = μ/(Λ_i+μ) — формула (7)
    из doc/formulas.tex. При μ = 0 равен 0."""
    hazard = node.total_hazard
    if node.recovery_rate == 0.0:
        return 0.0
    return node.recovery_rate / (hazard + node.recovery_rate)


@dataclass(frozen=True)
class ImpactContribution:
    """Вклад одного воздействия в общий ущерб узла."""

    impact: Impact
    hazard: float
    share: float  # доля в общей интенсивности ущерба, [0; 1]


def impact_contributions(node: NodeInput) -> list[ImpactContribution]:
    """Распределение общей интенсивности ущерба между источниками
    (собственные отказ + каждое воздействие), формула (11) из doc/formulas.tex:
    s_j = λ_j·p_ij / Λ_i. При Λ_i = 0 доли равны 0."""
    hazard = node.total_hazard
    if hazard == 0.0:
        return [
            ImpactContribution(imp, imp.hazard, 0.0) for imp in node.impacts
        ]
    return [
        ImpactContribution(imp, imp.hazard, imp.hazard / hazard)
        for imp in node.impacts
    ]
