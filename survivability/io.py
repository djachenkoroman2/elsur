"""Ввод/вывод данных: интерактивный ввод, чтение JSON, запись CSV/JSON."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .model import (DEFAULT_MONTE_CARLO_RUNS, SYSTEM_TARGETS, Impact,
                    NodeInput, NodeOverride, ProtectionInput,
                    ProtectionVariant, SystemInput, ValidationError)


def node_from_dict(data: dict) -> NodeInput:
    """Сборка NodeInput из словаря, разобранного из JSON.

    Ожидаемый формат (все интенсивности и period — в одних единицах времени):

        {
          "name": "Сервер маршрутизации",          # необязательно, по умолчанию "узел"
          "own_failure_rate": 0.02,                # обязательно, >= 0
          "recovery_rate": 2.0,                    # необязательно, >= 0; по умолчанию 0 (без восстановления)
          "period": 24.0,                          # обязательно, > 0
          "impacts": [                             # обязательно, может быть пустым []
            {"name": "DDoS-атака",                  # обязательно
             "intensity": 1.5,                      # обязательно, >= 0
             "defeat_probability": 0.4}             # обязательно, [0; 1]
          ]
        }

    Поднимает ValidationError при отсутствии обязательного поля, неверном
    типе значения или нарушении ограничений — сообщение пригодно для показа
    пользователю (код возврата программы в этом случае — 2).
    """
    try:
        impacts = tuple(
            Impact(
                name=str(imp["name"]),
                intensity=float(imp["intensity"]),
                defeat_probability=float(imp["defeat_probability"]),
            )
            for imp in data["impacts"]
        )
        return NodeInput(
            name=str(data.get("name", "узел")),
            own_failure_rate=float(data["own_failure_rate"]),
            impacts=impacts,
            recovery_rate=float(data.get("recovery_rate", 0.0)),
            period=float(data["period"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"Некорректный формат входных данных: {exc}") from exc


def node_from_json(path: Path) -> NodeInput:
    """Чтение исходных данных из JSON-файла в кодировке UTF-8.

    Поднимает OSError, если файл не найден/не читается, json.JSONDecodeError
    при синтаксической ошибке JSON и ValidationError при неверном формате
    данных (см. node_from_dict).
    """
    return node_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Задача 2–3: система и варианты защиты
# ---------------------------------------------------------------------------


def _impacts_from_list(items) -> tuple[Impact, ...]:
    return tuple(
        Impact(
            name=str(imp["name"]),
            intensity=float(imp["intensity"]),
            defeat_probability=float(imp["defeat_probability"]),
        )
        for imp in items
    )


def _links_from_list(items, what: str) -> tuple[tuple[str, str], ...]:
    links = []
    for link in items:
        if not isinstance(link, (list, tuple)) or len(link) != 2:
            raise ValidationError(f"{what}: связь должна быть парой имён узлов: {link!r}")
        links.append((str(link[0]), str(link[1])))
    return tuple(links)


def system_from_dict(data: dict) -> SystemInput:
    """Сборка SystemInput из словаря, разобранного из JSON (ключ `system`).

    Ожидаемый формат:

        {
          "name": "ЦОИ — типовой участок",          # необязательно
          "nodes": [ {... формат node_from_dict...} ],  # обязательно, ≥ 1;
                                                   # имя каждого узла обязательно
          "links": [["узел А", "узел Б"], ...],    # необязательно, по умолчанию []
          "w_def": 0.5,                            # обязательно, [0; 1]
          "target": "connectivity",                # необязательно; по умолчанию
                                                   # "node_fraction"; доступны:
          "monte_carlo_runs": 100000,              # необязательно, > 0
          "seed": 42                               # необязательно, целое
        }
    """
    try:
        nodes_data = data["nodes"]
        if not isinstance(nodes_data, list) or not nodes_data:
            raise ValidationError("Система должна содержать список nodes из хотя бы одного узла")
        for item in nodes_data:
            if "name" not in item:
                raise ValidationError("Каждый узел системы должен иметь поле name")
        nodes = tuple(node_from_dict(item) for item in nodes_data)
        links = _links_from_list(data.get("links", []), "links")
        return SystemInput(
            name=str(data.get("name", "система")),
            nodes=nodes,
            links=links,
            w_def=float(data["w_def"]),
            target=str(data.get("target", SYSTEM_TARGETS[0])),
            monte_carlo_runs=int(data.get("monte_carlo_runs", DEFAULT_MONTE_CARLO_RUNS)),
            seed=int(data["seed"]) if "seed" in data else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"Некорректный формат входных данных: {exc}") from exc


def protection_from_dict(data: dict, system: SystemInput) -> ProtectionInput:
    """Сборка ProtectionInput из словаря (ключ `protection`) для системы system.

    Ожидаемый формат:

        {
          "budget": 50,                            # обязательно, ≥ 0 — C_доп
          "variants": [                            # обязательно, ≥ 1 вариант
            {
              "name": "Резервный канал",           # обязательно, уникально
              "cost": 40,                          # обязательно, ≥ 0
              "nodes": {                           # необязательно: переопределения
                "ЦОИ": {                           #   только меняемые поля
                  "own_failure_rate": 0.005,
                  "recovery_rate": 6.0,
                  "impacts": [ {"name": "...", "intensity": ...,
                                "defeat_probability": ...} ]
                }
              },
              "links_added": [["узел А", "узел Б"]]  # необязательно
            }
          ]
        }
    """
    try:
        budget = float(data["budget"])
        variants_data = data["variants"]
        if not isinstance(variants_data, list) or not variants_data:
            raise ValidationError("Список вариантов защиты variants не должен быть пустым")
        variants = []
        for item in variants_data:
            overrides = {}
            for node_name, ov in (item.get("nodes") or {}).items():
                overrides[str(node_name)] = NodeOverride(
                    own_failure_rate=float(ov["own_failure_rate"])
                    if "own_failure_rate" in ov else None,
                    recovery_rate=float(ov["recovery_rate"])
                    if "recovery_rate" in ov else None,
                    impacts=_impacts_from_list(ov["impacts"])
                    if "impacts" in ov else None,
                )
            variant = ProtectionVariant(
                name=str(item["name"]),
                cost=float(item["cost"]),
                node_overrides=overrides,
                links_added=_links_from_list(item.get("links_added", []),
                                             f"вариант {item.get('name')!r}"),
            )
            variant.validate_against(system)
            variants.append(variant)
        return ProtectionInput(budget=budget, variants=tuple(variants))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"Некорректный формат входных данных: {exc}") from exc


@dataclass(frozen=True)
class InputJob:
    """Распознанное входное задание: одиночный узел либо система (+ защита)."""

    node: NodeInput | None = None
    system: SystemInput | None = None
    protection: ProtectionInput | None = None


def job_from_json(path: Path) -> InputJob:
    """Чтение JSON-файла и распознавание режима: ключ `system` — расчёт
    системы (при наличии ключа `protection` — выбор способа защиты),
    иначе — одиночный узел (обратная совместимость)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError("Некорректный формат входных данных: ожидается объект JSON")
    if "system" in data:
        system = system_from_dict(data["system"])
        protection = (protection_from_dict(data["protection"], system)
                      if "protection" in data else None)
        return InputJob(system=system, protection=protection)
    return InputJob(node=node_from_dict(data))


def demo_node() -> NodeInput:
    """Демонстрационный пример (режим --demo).

    Узел ЦОИ с тремя типовыми деструктивными воздействиями
    (по мотивам слайдов презентации: DDoS, целевая кибератака, РЭП).
    Интенсивности — условные, в 1/сутки.
    """
    return NodeInput(
        name="Узел ЦОИ-1",
        own_failure_rate=0.01,
        impacts=(
            Impact("Массовая DDoS-атака (Н1–Н2)", intensity=2.0, defeat_probability=0.30),
            Impact("Целевая кибератака (Н3)", intensity=0.1, defeat_probability=0.70),
            Impact("РЭП / разрыв канала (Н4)", intensity=0.2, defeat_probability=0.50),
        ),
        recovery_rate=4.0,
        period=1.0,
    )


def prompt_node() -> NodeInput:
    """Интерактивный опрос пользователя с валидацией."""
    print("=== Ввод исходных данных для расчёта живучести узла ===")
    name = input("Наименование узла [узел]: ").strip() or "узел"
    own = _prompt_float("Интенсивность собственных отказов λ_i^(0), 1/ед.вр.: ")
    mu = _prompt_float("Интенсивность восстановления μ, 1/ед.вр. (0 — без восстановления): ")
    period = _prompt_positive("Период оценивания T, ед.вр.: ")

    impacts: list[Impact] = []
    print("Деструктивные воздействия (пустое имя — конец ввода):")
    while True:
        imp_name = input("  Наименование воздействия: ").strip()
        if not imp_name:
            break
        lam = _prompt_float(f"  Интенсивность λ для «{imp_name}», 1/ед.вр.: ")
        p = _prompt_probability(f"  Вероятность поражения p для «{imp_name}» [0; 1]: ")
        impacts.append(Impact(imp_name, lam, p))

    return NodeInput(name, own, tuple(impacts), mu, period)


def _prompt_float(prompt: str) -> float:
    while True:
        try:
            return _prompt_non_negative(prompt)
        except ValidationError as exc:
            print(f"Ошибка: {exc}")


def _prompt_non_negative(prompt: str) -> float:
    value = float(input(prompt).replace(",", "."))
    if value < 0:
        raise ValidationError(f"значение не может быть отрицательным: {value}")
    return value


def _prompt_positive(prompt: str) -> float:
    while True:
        value = _prompt_non_negative(prompt)
        if value > 0:
            return value
        print("Ошибка: значение должно быть положительным")


def _prompt_probability(prompt: str) -> float:
    while True:
        value = float(input(prompt).replace(",", "."))
        if 0.0 <= value <= 1.0:
            return value
        print("Ошибка: вероятность должна лежать в диапазоне [0; 1]")


def format_report(node: NodeInput, result: dict) -> str:
    """Текстовый отчёт о результатах расчёта."""
    lines = [
        "",
        f"=== Результаты расчёта живучести: {node.name} ===",
        f"Период оценивания T                      : {node.period:g}",
        f"Интенсивность собственных отказов λ_i^(0): {node.own_failure_rate:g}",
        f"Интенсивность восстановления μ           : {node.recovery_rate:g}",
        f"Общая интенсивность ущерба Λ_i           : {node.total_hazard:.6g}",
        "",
        "Вклады источников ущерба:",
    ]
    own_share = (node.own_failure_rate / node.total_hazard) if node.total_hazard else 0.0
    lines.append(
        f"  {'Собственные отказы':<40} λ={node.own_failure_rate:<10.6g} "
        f"доля={own_share:6.2%}"
    )
    for c in result["contributions"]:
        lines.append(
            f"  {c.impact.name:<40} λ={c.impact.intensity:<10.6g} "
            f"p={c.impact.defeat_probability:<5.3g} λ·p={c.hazard:<10.6g} доля={c.share:6.2%}"
        )
    lines += [
        "",
        f"Живучесть узла Q_i = P(x_i(T)=1)         : {result['survivability']:.6f}",
    ]
    if node.recovery_rate > 0:
        lines.append(f"Предельный коэффициент готовности K        : {result['availability']:.6f}")
    lines.append("")
    return "\n".join(lines)


def save_csv(path: Path, node: NodeInput, result: dict) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["parameter", "value"])
        writer.writerow(["node", node.name])
        writer.writerow(["period_T", node.period])
        writer.writerow(["own_failure_rate", node.own_failure_rate])
        writer.writerow(["recovery_rate_mu", node.recovery_rate])
        writer.writerow(["total_hazard_Lambda", node.total_hazard])
        writer.writerow(["survivability_Q", result["survivability"]])
        writer.writerow(["steady_state_availability_K", result["availability"]])
        writer.writerow([])
        writer.writerow(["impact", "intensity_lambda", "defeat_probability_p",
                         "hazard_lambda_p", "share"])
        for c in result["contributions"]:
            writer.writerow([c.impact.name, c.impact.intensity,
                             c.impact.defeat_probability, c.hazard, c.share])


def save_json(path: Path, node: NodeInput, result: dict) -> None:
    payload = {
        "node": node.name,
        "period_T": node.period,
        "own_failure_rate": node.own_failure_rate,
        "recovery_rate_mu": node.recovery_rate,
        "total_hazard_Lambda": node.total_hazard,
        "survivability_Q": result["survivability"],
        "steady_state_availability_K": result["availability"],
        "impacts": [
            {
                "name": c.impact.name,
                "intensity_lambda": c.impact.intensity,
                "defeat_probability_p": c.impact.defeat_probability,
                "hazard_lambda_p": c.hazard,
                "share": c.share,
            }
            for c in result["contributions"]
        ],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# Отчёты и экспорт: система и выбор способа защиты
# ---------------------------------------------------------------------------

_TARGET_TITLES = {
    "node_fraction": "доля работоспособных узлов",
    "connectivity": "доля связных пар (компоненты связности)",
}


def _method_title(estimate) -> str:
    if estimate.method == "exact":
        return "точный перебор"
    return f"Монте-Карло, N={estimate.runs}"


def format_system_report(system: SystemInput, estimate, importance) -> str:
    """Текстовый отчёт о живучести системы Q(S)."""
    lines = [
        "",
        f"=== Результаты расчёта живучести системы: {system.name} ===",
        f"Число узлов n                            : {len(system.nodes)}",
        f"Число связей                             : {len(system.links)}",
        f"Целевая функция W(X)                     : {_TARGET_TITLES[system.target]}",
        f"Допустимый порог деградации W_доп        : {system.w_def:g}",
        f"Способ расчёта                           : {_method_title(estimate)}",
    ]
    if estimate.ci is not None:
        lines.append(f"Доверительный интервал Уилсона 95 %       : "
                     f"[{estimate.ci[0]:.6f}; {estimate.ci[1]:.6f}]")
    lines += [
        f"Живучесть системы Q(S)                   : {estimate.q:.6f}",
        "",
        "Важность узлов (критичность отказа):",
    ]
    for imp in importance:
        lines.append(
            f"  {imp.name:<40} I={imp.criticality:9.6f}  "
            f"Q(S|x=1)={imp.q_if_up:.6f}  Q(S|x=0)={imp.q_if_down:.6f}"
        )
    lines.append("")
    return "\n".join(lines)


def format_protection_report(result) -> str:
    """Текстовый отчёт о сравнении вариантов защиты и выборе g*."""
    lines = [
        "",
        "=== Сравнение способов защиты ===",
        f"Бюджет C_доп                             : {result.budget:g}",
        f"Живучесть без защиты Q_base(S)           : {result.base.q:.6f}",
        "",
        f"{'Вариант':<40} {'C(g)':>8} {'Допустим':>9} {'Q_g(S)':>10} {'ΔQ к базе':>10}",
    ]
    for row in result.rows:
        feasible = "да" if row.feasible else "НЕТ"
        mark = "*" if row is result.chosen else " "
        lines.append(
            f"{mark}{row.name:<39} {row.cost:>8g} {feasible:>9} "
            f"{row.estimate.q:>10.6f} {row.estimate.q - result.base.q:>+10.6f}"
        )
        if row.estimate.ci is not None:
            lines.append(
                f"   ДИ 95 %: [{row.estimate.ci[0]:.6f}; {row.estimate.ci[1]:.6f}]"
                f"  (N={row.estimate.runs}, N*={row.estimate.successes})"
            )
    lines += [
        "",
        f"Рациональный способ защиты g*            : {result.chosen.name}",
        f"Q_{result.chosen.name}(S)                : {result.chosen.estimate.q:.6f}",
        f"Прирост живучести ΔQ                     : {result.delta_q:+.6f}",
        f"Относительный прирост ΔQ / Q_base        : {result.relative_delta:+.2%}",
        f"Эффективность затрат ΔQ / C(g*)          : {result.cost_efficiency:.6f}",
        "",
    ]
    return "\n".join(lines)


def save_system_json(path: Path, system: SystemInput, estimate, importance) -> None:
    payload = {
        "system": system.name,
        "nodes_n": len(system.nodes),
        "links_count": len(system.links),
        "target": system.target,
        "w_def": system.w_def,
        "method": estimate.method,
        "monte_carlo_runs": estimate.runs,
        "monte_carlo_successes": estimate.successes,
        "wilson_ci_95": list(estimate.ci) if estimate.ci else None,
        "system_survivability_Q": estimate.q,
        "node_importance": [
            {
                "name": imp.name,
                "q_if_up": imp.q_if_up,
                "q_if_down": imp.q_if_down,
                "criticality_birnbaum": imp.criticality,
                "improvement_potential": imp.improvement,
            }
            for imp in importance
        ],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def save_system_csv(path: Path, system: SystemInput, estimate, importance) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["parameter", "value"])
        writer.writerow(["system", system.name])
        writer.writerow(["nodes_n", len(system.nodes)])
        writer.writerow(["links_count", len(system.links)])
        writer.writerow(["target", system.target])
        writer.writerow(["w_def", system.w_def])
        writer.writerow(["method", estimate.method])
        writer.writerow(["monte_carlo_runs", estimate.runs])
        writer.writerow(["system_survivability_Q", estimate.q])
        writer.writerow([])
        writer.writerow(["node", "q_if_up", "q_if_down",
                         "criticality_birnbaum", "improvement_potential"])
        for imp in importance:
            writer.writerow([imp.name, imp.q_if_up, imp.q_if_down,
                             imp.criticality, imp.improvement])


def save_protection_json(path: Path, result) -> None:
    payload = {
        "budget": result.budget,
        "base_survivability_Q": result.base.q,
        "base_wilson_ci_95": (list(result.base.ci) if result.base.ci else None),
        "chosen": result.chosen.name,
        "delta_q": result.delta_q,
        "relative_delta": result.relative_delta,
        "cost_efficiency": result.cost_efficiency,
        "variants": [
            {
                "name": row.name,
                "cost": row.cost,
                "feasible": row.feasible,
                "q_g": row.estimate.q,
                "method": row.estimate.method,
                "monte_carlo_runs": row.estimate.runs,
                "wilson_ci_95": (list(row.estimate.ci)
                                 if row.estimate.ci else None),
                "delta_q_vs_base": row.estimate.q - result.base.q,
            }
            for row in result.rows
        ],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def save_protection_csv(path: Path, result) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["variant", "cost", "feasible", "q_g", "method",
                         "ci_lo", "ci_hi", "delta_q_vs_base", "chosen"])
        for row in result.rows:
            writer.writerow([
                row.name, row.cost, row.feasible, row.estimate.q,
                row.estimate.method,
                row.estimate.ci[0] if row.estimate.ci else "",
                row.estimate.ci[1] if row.estimate.ci else "",
                row.estimate.q - result.base.q,
                row is result.chosen,
            ])
