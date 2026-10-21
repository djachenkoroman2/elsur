"""Командная строка приложения: python -m survivability [--demo|--file F] [--output F]."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .calc import impact_contributions, steady_state_availability, survivability
from .io import (InputJob, demo_node, format_protection_report,
                 format_report, format_system_report, job_from_json,
                 prompt_node, save_csv, save_json, save_protection_csv,
                 save_protection_json, save_system_csv, save_system_json)
from .model import ValidationError
from .protection import compare_protection
from .system_calc import node_importance, system_qs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="survivability",
        description="Расчёт живучести узла компьютерной сети "
                    "Q_i = P(x_i(T)=1) по марковской модели «отказ–восстановление»; "
                    "живучести системы Q(S) и выбор способа защиты.",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true",
                     help="расчёт на встроенном демонстрационном примере")
    src.add_argument("--file", type=Path, metavar="JSON",
                     help="чтение исходных данных из JSON-файла (узел, система "
                          "или система с вариантами защиты)")
    parser.add_argument("--output", type=Path, metavar="FILE",
                        help="сохранить результаты в файл (.csv или .json)")
    parser.add_argument("--seed", type=int, metavar="INT",
                        help="зерно ГПСЧ для Монте-Карло (перекрывает поле seed "
                             "во входном файле; без него результат случаен)")
    return parser


def compute(node) -> dict:
    return {
        "survivability": survivability(node),
        "availability": steady_state_availability(node),
        "contributions": impact_contributions(node),
    }


def _run_node_job(node, output: Path | None) -> int:
    result = compute(node)
    print(format_report(node, result))
    if output:
        try:
            if output.suffix.lower() == ".json":
                save_json(output, node, result)
            else:
                save_csv(output, node, result)
            print(f"Результаты сохранены в {output}")
        except OSError as exc:
            print(f"Ошибка записи файла: {exc}", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Коды возврата: 0 — расчёт выполнен; 2 — ошибка входных данных
    (невалидный JSON-файл, нарушение ограничений, файл не найден,
    пустое допустимое множество вариантов защиты);
    1 — ошибка записи файла результатов.
    """
    args = build_parser().parse_args(argv)
    try:
        if args.demo:
            return _run_node_job(demo_node(), args.output)
        if args.file:
            job = job_from_json(args.file)
        else:
            job = InputJob(node=prompt_node())
    except ValidationError as exc:
        print(f"Ошибка ввода: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Ошибка чтения файла: {exc}", file=sys.stderr)
        return 2

    if job.node is not None:
        return _run_node_job(job.node, args.output)

    system = job.system
    if job.protection is not None:
        try:
            result = compare_protection(system, job.protection, seed=args.seed)
        except ValidationError as exc:
            print(f"Ошибка ввода: {exc}", file=sys.stderr)
            return 2
        print(format_protection_report(result))
        if args.output:
            try:
                if args.output.suffix.lower() == ".json":
                    save_protection_json(args.output, result)
                else:
                    save_protection_csv(args.output, result)
                print(f"Результаты сохранены в {args.output}")
            except OSError as exc:
                print(f"Ошибка записи файла: {exc}", file=sys.stderr)
                return 1
        return 0

    estimate = system_qs(system)
    importance = node_importance(system, estimate)
    print(format_system_report(system, estimate, importance))
    if args.output:
        try:
            if args.output.suffix.lower() == ".json":
                save_system_json(args.output, system, estimate, importance)
            else:
                save_system_csv(args.output, system, estimate, importance)
            print(f"Результаты сохранены в {args.output}")
        except OSError as exc:
            print(f"Ошибка записи файла: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
