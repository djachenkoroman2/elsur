"""Модель данных: деструктивные воздействия и параметры узла."""

from __future__ import annotations

from dataclasses import dataclass, field


class ValidationError(ValueError):
    """Ошибка валидации входных данных."""


def _check_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValidationError(f"{name} не может быть отрицательным: {value}")


def _check_probability(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValidationError(f"{name} должна лежать в диапазоне [0; 1]: {value}")


@dataclass(frozen=True)
class Impact:
    """Деструктивное воздействие d_j на узел e_i.

    intensity          — интенсивность воздействия λ_j (1/ед. времени);
    defeat_probability — вероятность поражения узла этим воздействием p_ij.
    """

    name: str
    intensity: float
    defeat_probability: float

    def __post_init__(self) -> None:
        _check_non_negative("Интенсивность воздействия", self.intensity)
        _check_probability("Вероятность поражения", self.defeat_probability)

    @property
    def hazard(self) -> float:
        """Вклад воздействия в общую интенсивность ущерба: λ_j · p_ij."""
        return self.intensity * self.defeat_probability


@dataclass(frozen=True)
class NodeInput:
    """Входные данные для расчёта живучести узла e_i.

    own_failure_rate — интенсивность собственных (случайных) отказов λ_i^(0);
    impacts          — перечень деструктивных воздействий D = {d_j};
    recovery_rate    — интенсивность восстановления μ (0 — восстановления нет);
    period           — период оценивания T > 0.
    """

    name: str
    own_failure_rate: float
    impacts: tuple[Impact, ...]
    recovery_rate: float
    period: float

    def __post_init__(self) -> None:
        _check_non_negative("Интенсивность собственных отказов", self.own_failure_rate)
        _check_non_negative("Интенсивность восстановления", self.recovery_rate)
        if self.period <= 0:
            raise ValidationError(f"Период оценивания T должен быть положительным: {self.period}")

    @property
    def total_hazard(self) -> float:
        """Общая интенсивность ущерба узла: Λ_i = λ_i^(0) + Σ_j λ_j · p_ij."""
        return self.own_failure_rate + sum(imp.hazard for imp in self.impacts)


# ---------------------------------------------------------------------------
# Задача 2: структурная модель системы S = ⟨E, L, F⟩
# ---------------------------------------------------------------------------

#: Доступные виды целевой функции W(X). Для добавления нового варианта
#: достаточно внести имя в этот кортеж и реализацию в TARGET_FUNCTIONS
#: (survivability/system_calc.py) — ядро переписывать не потребуется.
SYSTEM_TARGET_NODE_FRACTION = "node_fraction"
SYSTEM_TARGET_CONNECTIVITY = "connectivity"
SYSTEM_TARGETS = (SYSTEM_TARGET_NODE_FRACTION, SYSTEM_TARGET_CONNECTIVITY)

DEFAULT_MONTE_CARLO_RUNS = 100_000


@dataclass(frozen=True)
class SystemInput:
    """Структурная модель системы S = ⟨E, L, F⟩ и условие допустимости.

    name           — наименование системы;
    nodes          — элементы E (узлы со всей статистикой по задаче 1);
    links          — связи L: неориентированные пары имён узлов;
    w_def          — допустимый порог деградации W_доп ∈ [0; 1];
    target         — вид целевой функции W(X), см. SYSTEM_TARGETS;
    monte_carlo_runs — число прогонов Монте-Карло (при n > предела перебора);
    seed           — зерно ГПСЧ для воспроизводимого Монте-Карло (None — случайно).
    """

    name: str
    nodes: tuple[NodeInput, ...]
    links: tuple[tuple[str, str], ...]
    w_def: float
    target: str = SYSTEM_TARGET_NODE_FRACTION
    monte_carlo_runs: int = DEFAULT_MONTE_CARLO_RUNS
    seed: int | None = None

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValidationError("Система должна содержать хотя бы один узел")
        names = [node.name for node in self.nodes]
        if len(set(names)) != len(names):
            raise ValidationError("Имена узлов системы должны быть уникальными")
        _check_probability("Допустимый порог деградации W_доп", self.w_def)
        if self.target not in SYSTEM_TARGETS:
            raise ValidationError(
                f"Неизвестный вид целевой функции: {self.target!r} "
                f"(доступны: {', '.join(SYSTEM_TARGETS)})"
            )
        if isinstance(self.monte_carlo_runs, bool) or not isinstance(self.monte_carlo_runs, int):
            raise ValidationError(f"Число прогонов Монте-Карло должно быть целым: {self.monte_carlo_runs}")
        if self.monte_carlo_runs <= 0:
            raise ValidationError(f"Число прогонов Монте-Карло должно быть положительным: {self.monte_carlo_runs}")
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
            raise ValidationError(f"Зерно ГПСЧ должно быть целым числом: {self.seed}")

        known = set(names)
        seen: set[tuple[str, str]] = set()
        for link in self.links:
            if len(link) != 2:
                raise ValidationError(f"Связь должна быть парой имён узлов: {link!r}")
            a, b = str(link[0]), str(link[1])
            if a == b:
                raise ValidationError(f"Петля (связь узла самого с собой) недопустима: {a!r}")
            if a not in known or b not in known:
                raise ValidationError(f"Связь ссылается на несуществующий узел: {a!r} — {b!r}")
            key = tuple(sorted((a, b)))
            if key in seen:
                raise ValidationError(f"Дубликат связи: {a!r} — {b!r}")
            seen.add(key)


# ---------------------------------------------------------------------------
# Задача 3: варианты способа защиты
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeOverride:
    """Переопределение параметров узла в варианте защиты.

    Поле, равное None, не изменяется (берётся значение базовой системы).
    impacts, если задано, полностью заменяет список воздействий узла
    (защита меняет вероятности поражения p_ij).
    """

    own_failure_rate: float | None = None
    recovery_rate: float | None = None
    impacts: tuple[Impact, ...] | None = None

    def __post_init__(self) -> None:
        if self.own_failure_rate is not None:
            _check_non_negative("Интенсивность собственных отказов", self.own_failure_rate)
        if self.recovery_rate is not None:
            _check_non_negative("Интенсивность восстановления", self.recovery_rate)


@dataclass(frozen=True)
class ProtectionVariant:
    """Вариант способа защиты g: переопределение параметров узлов,
    добавление резервных связей и затраты C(g)."""

    name: str
    cost: float
    node_overrides: dict[str, NodeOverride]
    links_added: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.cost < 0:
            raise ValidationError(f"Стоимость варианта защиты не может быть отрицательной: {self.cost}")
        seen: set[tuple[str, str]] = set()
        for link in self.links_added:
            if len(link) != 2:
                raise ValidationError(f"Связь должна быть парой имён узлов: {link!r}")
            a, b = str(link[0]), str(link[1])
            if a == b:
                raise ValidationError(f"Петля (связь узла самого с собой) недопустима: {a!r}")
            key = tuple(sorted((a, b)))
            if key in seen:
                raise ValidationError(f"Дубликат добавляемой связи: {a!r} — {b!r}")
            seen.add(key)

    def validate_against(self, system: SystemInput) -> None:
        """Проверка ссылок варианта на элементы базовой системы."""
        known = {node.name for node in system.nodes}
        for node_name in self.node_overrides:
            if node_name not in known:
                raise ValidationError(
                    f"Вариант {self.name!r}: переопределение несуществующего узла {node_name!r}"
                )
        existing = {tuple(sorted(link)) for link in system.links}
        for link in self.links_added:
            a, b = str(link[0]), str(link[1])
            if a not in known or b not in known:
                raise ValidationError(
                    f"Вариант {self.name!r}: добавляемая связь ссылается на "
                    f"несуществующий узел: {a!r} — {b!r}"
                )
            if tuple(sorted((a, b))) in existing:
                raise ValidationError(
                    f"Вариант {self.name!r}: связь {a!r} — {b!r} уже существует в системе"
                )


@dataclass(frozen=True)
class ProtectionInput:
    """Условие выбора способа защиты: бюджет C_доп и множество вариантов G."""

    budget: float
    variants: tuple[ProtectionVariant, ...]

    def __post_init__(self) -> None:
        _check_non_negative("Бюджет C_доп", self.budget)
        if not self.variants:
            raise ValidationError("Список вариантов защиты не должен быть пустым")
        names = [variant.name for variant in self.variants]
        if len(set(names)) != len(names):
            raise ValidationError("Имена вариантов защиты должны быть уникальными")
