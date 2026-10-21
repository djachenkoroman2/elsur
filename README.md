# survivability — расчёт живучести узла и системы компьютерной сети

Консольное приложение на Python для количественной оценки живучести узла и
системы компьютерной сети в условиях деструктивных воздействий (собственные
отказы, атаки, разрывы каналов) по марковской модели «отказ–восстановление»
(задача 1) и оценки живучести системы $Q(S)$ по топологии сети (задача 2),
а также выбора рационального способа защиты $g^*$ при ограничении на затраты
(задача 3).

Живучесть узла — вероятность того, что узел останется работоспособным
(либо будет восстановлен) к концу заданного периода оценивания $T$:

$$Q_i = P\!\left(x_i(T) = 1\right)$$

Живучесть системы — вероятность того, что функция эффективности $W(X)$ не
опустится ниже допустимого порога деградации $W_{\text{доп}}$:

$$Q(S) = P\!\left(W(X) \geq W_{\text{доп}}\right), \qquad
g^* = \arg\max_{g \in G} Q_g(S) \;\; \text{при} \;\; C(g) \leq C_{\text{доп}}$$

Документация (каталог `doc/`):

- [Быстрый старт](doc/quickstart.md) — установка и первый запуск за 5 минут;
- [Руководство пользователя](doc/user-guide.md) — графическая оболочка и форматы данных;
- [Руководство программиста](doc/developer-guide.md) — архитектура, модули, алгоритмы;
- вывод формул — [doc/formulas.pdf](doc/formulas.pdf) (источник [doc/formulas.tex](doc/formulas.tex));
- расчёт $Q(S)$ с учётом топологии (формулы, алгоритм, пример, разобранный
  вручную) — [doc/system_topology.md](doc/system_topology.md) и инфографика
  [doc/qs_topology.svg](doc/qs_topology.svg);
- блок-схемы алгоритмов — [doc/algorithm.svg](doc/algorithm.svg) (узел),
  [doc/algorithm_system.svg](doc/algorithm_system.svg) (система и выбор защиты);
- применимость методики к типам узлов — [doc/node_types.md](doc/node_types.md);
- практическое применение и эффект внедрения — [doc/answ0001.md](doc/answ0001.md).

## Установка и запуск

Окружение собирается через [uv](https://docs.astral.sh/uv/) по
`pyproject.toml` и `uv.lock`. Нужен Python 3.11+ (uv скачает подходящий
интерпретатор сам, если в системе его нет). Ядро расчётов использует только
стандартную библиотеку; PyQt6 для графической оболочки входит в группу
зависимостей `gui`, которая ставится по умолчанию.

```bash
uv sync                          # создать .venv и установить зависимости
uv run python -m survivability --demo
```

`uv sync --no-group gui` — окружение только для консоли (без PyQt6);
`uv sync --group build` — дополнительно PyInstaller для сборки
исполняемого файла GUI (`uv run pyinstaller survivability-gui.spec`).

## Режимы работы

```bash
# Демонстрационный пример (без ручного ввода):
uv run python -m survivability --demo

# Исходные данные из JSON-файла (формат — examples/input.json,
# система — examples/example6_system_line.json):
uv run python -m survivability --file examples/input.json

# Интерактивный ввод:
uv run python -m survivability

# Сохранение результатов (.csv или .json по расширению):
uv run python -m survivability --demo --output results.json

# Графическая оболочка (редактор топологии, роли узлов, оформление
# схемы — цвета, фигуры, подписи каналов; расчёт; экспорт SVG/DOT):
uv run python -m survivability.gui
```

## Графическая оболочка

`uv run python -m survivability.gui` — редактор проекта с канвасом топологии:
создание узлов по ролям из библиотеки (сервер, маршрутизатор, …) или
вручную, пользовательские наборы ролей, перетаскивание узлов (координаты
сохраняются в файле проекта), цвета и фигуры узлов, подписи каналов связи.
Файл проекта остаётся совместимым с CLI: метаданные оболочки хранятся
в служебной секции `gui`, которую консольная утилита игнорирует.

## Тесты

```bash
uv run python -m unittest discover -s tests -v
```

## Структура проекта

```
survivability/      пакет приложения
  model.py          модель данных (Impact, NodeInput, SystemInput, ProtectionInput) и валидация
  calc.py           расчётные формулы задачи 1 (Q_i, K, вклады воздействий)
  system_calc.py    задача 2: Q(S) — перебор / Монте-Карло, W(X), важность узлов
  protection.py     задача 3: варианты защиты, бюджет, выбор g*
  project_codec.py  проект ↔ JSON-словарь, экспорт топологии SVG/DOT (без Qt)
  io.py             ввод/вывод: интерактив, JSON, CSV
  cli.py            командная строка
  gui.py            графическая оболочка PyQt (python -m survivability.gui)
  __main__.py       точка входа: python -m survivability
tests/              модульные тесты (unittest)
pyproject.toml      метаданные проекта и зависимости (uv); uv.lock — зафиксированные версии
doc/                документация: quickstart / user-guide / developer-guide, формулы LaTeX, схемы SVG
examples/           примеры входных данных (узел — 1–5, система — 6–7, защита — 8–9)
```
