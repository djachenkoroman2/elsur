"""Кодек проекта и экспорт топологии — слой между моделью и представлением.

Модуль намеренно не зависит от Qt: функции преобразования модели данных
в JSON-совместимый словарь, служебные функции секции `gui` (роли узлов,
координаты, цвета, фигуры, подписи каналов) и рендереры SVG/DOT
используются как графической оболочкой (`survivability/gui.py`), так и
модульными тестами без создания QApplication.

Формат словаря — тот же, что у CLI (см. `survivability/io.py`,
`doc/user-guide.md`): ключ `system` (обязательный), опциональный
ключ `protection` и опциональный служебный ключ `gui` с метаданными
оболочки (CLI его игнорирует, см. `sanitize_gui_section`).
"""

from __future__ import annotations

import base64
import copy
import math
from xml.sax.saxutils import escape

from .model import (Impact, NodeInput, ProtectionInput, ProtectionVariant,
                    SystemInput, ValidationError)

# ---------------------------------------------------------------------------
# Модель → словарь (обратное преобразование к io.system_from_dict /
# io.protection_from_dict — совместное использование даёт round-trip)
# ---------------------------------------------------------------------------


def node_to_dict(node: NodeInput) -> dict:
    return {
        "name": node.name,
        "own_failure_rate": node.own_failure_rate,
        "recovery_rate": node.recovery_rate,
        "period": node.period,
        "impacts": [
            {
                "name": imp.name,
                "intensity": imp.intensity,
                "defeat_probability": imp.defeat_probability,
            }
            for imp in node.impacts
        ],
    }


def system_to_dict(system: SystemInput) -> dict:
    return {
        "name": system.name,
        "nodes": [node_to_dict(node) for node in system.nodes],
        "links": [[a, b] for a, b in system.links],
        "w_def": system.w_def,
        "target": system.target,
        "monte_carlo_runs": system.monte_carlo_runs,
        **({"seed": system.seed} if system.seed is not None else {}),
    }


def protection_to_dict(protection: ProtectionInput) -> dict:
    variants = []
    for variant in protection.variants:
        item: dict = {"name": variant.name, "cost": variant.cost}
        overrides = {}
        for node_name, override in variant.node_overrides.items():
            ov: dict = {}
            if override.own_failure_rate is not None:
                ov["own_failure_rate"] = override.own_failure_rate
            if override.recovery_rate is not None:
                ov["recovery_rate"] = override.recovery_rate
            if override.impacts is not None:
                ov["impacts"] = [
                    {
                        "name": imp.name,
                        "intensity": imp.intensity,
                        "defeat_probability": imp.defeat_probability,
                    }
                    for imp in override.impacts
                ]
            overrides[node_name] = ov
        if overrides:
            item["nodes"] = overrides
        if variant.links_added:
            item["links_added"] = [[a, b] for a, b in variant.links_added]
        variants.append(item)
    return {"budget": protection.budget, "variants": variants}


def project_to_dict(system: SystemInput,
                     protection: ProtectionInput | None = None) -> dict:
    """Словарь проекта в формате входного JSON приложения."""
    project = {"system": system_to_dict(system)}
    if protection is not None:
        project["protection"] = protection_to_dict(protection)
    return project


# ---------------------------------------------------------------------------
# Секция `gui`: метаданные оболочки (роли, координаты, цвета, фигуры,
# подписи каналов). CLI эту секцию не читает — формат обмена с ним
# не меняется. Все функции «best effort»: повреждённые записи отбрасываются.
# ---------------------------------------------------------------------------

#: Доступные фигуры узла на канвасе и в SVG.
NODE_SHAPES = ("rectangle", "oval", "trapezoid", "diamond")
DEFAULT_NODE_SHAPE = "rectangle"
DEFAULT_NODE_COLOR = "#cfe2f3"

#: Маппинг фигур на атрибуты Graphviz (`topology_dot`).
DOT_SHAPES = {"rectangle": "box", "oval": "ellipse",
              "trapezoid": "trapezium", "diamond": "diamond"}

#: Оформление связей (чисто отображение; расчётную модель не меняет).
LINK_LINE_STYLES = ("solid", "dash", "dashdot")
LINK_ARROW_STYLES = ("none", "arrow")
DEFAULT_LINK_STYLE = "solid"
DEFAULT_ARROW_STYLE = "none"
DEFAULT_LINK_COLOR = "#666666"
DEFAULT_LINK_WIDTH = 2
LINK_WIDTH_RANGE = (1, 10)

#: Изображение узла (пиктограмма) — data-URI с этим MIME; анимации нет.
NODE_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/bmp")
#: Лимиты изображения узла: линейный размер и объём данных после декодирования.
NODE_IMAGE_MAX_SIZE = 512
NODE_IMAGE_MAX_BYTES = 256 * 1024

#: Шаблоны пунктира для `stroke-dasharray` в SVG (`topology_svg`).
SVG_DASHARRAY = {"dash": "7,5", "dashdot": "10,4,2,4"}

#: Встроенная библиотека ролей узлов (параметры «примерные», типичные
#: для роли; применение роли предзаполняет поля узла, после чего их
#: можно менять вручную). Пользовательские роли хранятся в `gui.roles`.
DEFAULT_ROLES: tuple[dict, ...] = (
    {"name": "Сервер", "own_failure_rate": 0.01, "recovery_rate": 4.0,
     "period": 1.0,
     "impacts": [{"name": "Массовая DDoS-атака", "intensity": 2.0,
                  "defeat_probability": 0.3},
                 {"name": "Целевая кибератака", "intensity": 0.1,
                  "defeat_probability": 0.7}]},
    {"name": "Маршрутизатор", "own_failure_rate": 0.02, "recovery_rate": 6.0,
     "period": 1.0,
     "impacts": [{"name": "РЭП / разрыв канала", "intensity": 0.2,
                  "defeat_probability": 0.5},
                 {"name": "Перегрузка трафиком", "intensity": 1.0,
                  "defeat_probability": 0.2}]},
    {"name": "Коммутатор", "own_failure_rate": 0.02, "recovery_rate": 6.0,
     "period": 1.0,
     "impacts": [{"name": "Перегрузка трафиком", "intensity": 1.0,
                  "defeat_probability": 0.2},
                 {"name": "Отказ питания", "intensity": 0.05,
                  "defeat_probability": 0.3}]},
    {"name": "Рабочая станция", "own_failure_rate": 0.05, "recovery_rate": 2.0,
     "period": 1.0,
     "impacts": [{"name": "Фишинг / вредоносное ПО", "intensity": 0.3,
                  "defeat_probability": 0.4},
                 {"name": "DDoS-атака", "intensity": 0.5,
                  "defeat_probability": 0.1}]},
    {"name": "Датчик / терминал", "own_failure_rate": 0.08,
     "recovery_rate": 1.0, "period": 1.0,
     "impacts": [{"name": "РЭП / разрыв канала", "intensity": 0.3,
                  "defeat_probability": 0.5},
                 {"name": "Отказ питания", "intensity": 0.1,
                  "defeat_probability": 0.4}]},
)

_UNSET = object()


def builtin_roles() -> list[dict]:
    """Копии встроенных ролей (защита от случайной порчи константы)."""
    return copy.deepcopy(list(DEFAULT_ROLES))


def validate_role(role: dict) -> dict:
    """Проверка роли через конструктор NodeInput; нормализованный словарь.

    Роль валидна, если её параметры допустимы для узла (интенсивности ≥ 0,
    p ∈ [0; 1], T > 0); имя непустое.
    """
    if not isinstance(role, dict):
        raise ValidationError(f"Роль должна быть словарём: {role!r}")
    name = str(role.get("name", "")).strip()
    if not name:
        raise ValidationError("Роль должна иметь непустое имя")
    try:
        impacts = tuple(
            Impact(name=str(item.get("name", "воздействие")),
                   intensity=float(item.get("intensity", 0.0)),
                   defeat_probability=float(item.get("defeat_probability", 0.0)))
            for item in role.get("impacts") or []
        )
        node = NodeInput(name=name,
                         own_failure_rate=float(role.get("own_failure_rate", 0.0)),
                         impacts=impacts,
                         recovery_rate=float(role.get("recovery_rate", 0.0)),
                         period=float(role.get("period", 1.0)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"Роль {name!r}: некорректные параметры: {exc}")
    return {"name": node.name,
            "own_failure_rate": node.own_failure_rate,
            "recovery_rate": node.recovery_rate,
            "period": node.period,
            "impacts": [{"name": imp.name, "intensity": imp.intensity,
                         "defeat_probability": imp.defeat_probability}
                        for imp in node.impacts]}


def normalize_shape(shape) -> str:
    """Неизвестная фигура откатывается к прямоугольнику."""
    return shape if shape in NODE_SHAPES else DEFAULT_NODE_SHAPE


def sanitize_color(value) -> str | None:
    """Цвет вида '#rrggbb' или None."""
    if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
        try:
            int(value[1:], 16)
        except ValueError:
            return None
        return value.lower()
    return None


def sanitize_node_image(value) -> str | None:
    """Data-URI изображения узла или None.

    Строка вида 'data:<mime>;base64,<данные>', <mime> — из
    NODE_IMAGE_MIME_TYPES; base64 должен декодироваться, объём данных —
    не более NODE_IMAGE_MAX_BYTES байт. Разметка формата Qt-free: проверяется
    только структура записи, не содержимое картинки.
    """
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    try:
        meta, payload = value[5:].split(",", 1)
    except ValueError:
        return None
    if not meta.endswith(";base64"):
        return None
    if meta[:-len(";base64")] not in NODE_IMAGE_MIME_TYPES:
        return None
    try:
        raw = base64.b64decode(payload.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if not raw or len(raw) > NODE_IMAGE_MAX_BYTES:
        return None
    return value


def normalize_line_style(value) -> str:
    """Неизвестный тип линии откатывается к сплошной."""
    return value if value in LINK_LINE_STYLES else DEFAULT_LINK_STYLE


def normalize_arrow_style(value) -> str:
    """Неизвестное оформление конца откатывается к «без стрелки»."""
    return value if value in LINK_ARROW_STYLES else DEFAULT_ARROW_STYLE


def normalize_link_width(value) -> int:
    """Толщина линии — целое из [1; 10]: вне диапазона — граница."""
    if isinstance(value, bool):
        return DEFAULT_LINK_WIDTH
    try:
        width = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LINK_WIDTH
    lo, hi = LINK_WIDTH_RANGE
    return max(lo, min(hi, width))


def gui_section(project: dict) -> dict:
    """Секция gui проекта (создаётся при отсутствии)."""
    section = project.get("gui")
    if not isinstance(section, dict):
        section = {}
        project["gui"] = section
    return section


def sanitize_gui_section(data) -> dict:
    """Очистка секции `gui`, прочитанной из файла: только известные ключи,
    только корректные записи; на входе — что угодно, на выходе — словарь."""
    section: dict = {}
    if not isinstance(data, dict):
        return section
    positions = data.get("positions")
    if isinstance(positions, dict):
        clean = {}
        for name, pair in positions.items():
            if not isinstance(name, str) or not isinstance(pair, (list, tuple)):
                continue
            if len(pair) != 2:
                continue
            try:
                clean[name] = [float(pair[0]), float(pair[1])]
            except (TypeError, ValueError):
                continue
        if clean:
            section["positions"] = clean
    nodes = data.get("nodes")
    if isinstance(nodes, dict):
        clean = {}
        for name, entry in nodes.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            item: dict = {}
            color = sanitize_color(entry.get("color"))
            if color:
                item["color"] = color
            if entry.get("shape") in NODE_SHAPES:
                item["shape"] = entry["shape"]
            role = entry.get("role")
            if isinstance(role, str) and role.strip():
                item["role"] = role.strip()
            image = sanitize_node_image(entry.get("image"))
            if image:
                item["image"] = image
            if item:
                clean[name] = item
        if clean:
            section["nodes"] = clean
    roles = data.get("roles")
    if isinstance(roles, list):
        clean = []
        for role in roles:
            try:
                clean.append(validate_role(role))
            except ValidationError:
                continue
        if clean:
            section["roles"] = clean
    labels = data.get("link_labels")
    if isinstance(labels, list):
        clean = []
        for record in labels:
            if not isinstance(record, dict):
                continue
            a, b = record.get("a"), record.get("b")
            label = record.get("label")
            if (isinstance(a, str) and isinstance(b, str) and a != b
                    and isinstance(label, str) and label.strip()):
                clean.append({"a": a, "b": b, "label": label.strip()})
        if clean:
            section["link_labels"] = clean
    styles = data.get("link_styles")
    if isinstance(styles, list):
        clean = []
        for record in styles:
            item = _clean_link_style_record(record)
            if item is not None:
                clean.append(item)
        if clean:
            section["link_styles"] = clean
    return section


def _clean_link_style_record(record) -> dict | None:
    """Очистка одной записи `link_styles`; None — запись повреждена."""
    if not isinstance(record, dict):
        return None
    a, b = record.get("a"), record.get("b")
    if not (isinstance(a, str) and isinstance(b, str) and a != b):
        return None
    item: dict = {"a": a, "b": b}
    color = sanitize_color(record.get("color"))
    if color:
        item["color"] = color
    if record.get("style") in LINK_LINE_STYLES:
        item["style"] = record["style"]
    if not isinstance(record.get("width"), bool):
        try:
            width = int(record["width"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            lo, hi = LINK_WIDTH_RANGE
            if lo <= width <= hi:
                item["width"] = width
    for key in ("arrow_start", "arrow_end"):
        if record.get(key) in LINK_ARROW_STYLES:
            item[key] = record[key]
    for key in ("label_start", "label_end"):
        label = record.get(key)
        if isinstance(label, str) and label.strip():
            item[key] = label.strip()
    return item


def user_roles(section: dict) -> list[dict]:
    """Пользовательские роли секции (невалидные пропускаются)."""
    roles = section.get("roles")
    if not isinstance(roles, list):
        return []
    clean = []
    for role in roles:
        try:
            clean.append(validate_role(role))
        except ValidationError:
            continue
    return clean


def positions_from_section(section: dict) -> dict[str, tuple[float, float]]:
    """Координаты центров узлов {имя: (x, y)} из секции gui."""
    positions: dict[str, tuple[float, float]] = {}
    raw = section.get("positions")
    if not isinstance(raw, dict):
        return positions
    for name, pair in raw.items():
        if isinstance(name, str) and isinstance(pair, (list, tuple)) \
                and len(pair) == 2:
            try:
                positions[name] = (float(pair[0]), float(pair[1]))
            except (TypeError, ValueError):
                continue
    return positions


def set_positions(section: dict,
                  positions: dict[str, tuple[float, float]]) -> None:
    """Запись координат в секцию gui (массивы чисел для JSON)."""
    if positions:
        section["positions"] = {name: [float(x), float(y)]
                                for name, (x, y) in positions.items()}
    else:
        section.pop("positions", None)


def node_style(section: dict, name: str) -> dict:
    """Оформление узла: {'color': str|None, 'shape': str, 'role': str|None}."""
    nodes = section.get("nodes")
    entry = nodes.get(name) if isinstance(nodes, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    role = entry.get("role")
    return {"color": sanitize_color(entry.get("color")),
            "shape": normalize_shape(entry.get("shape")),
            "role": role.strip() if isinstance(role, str) and role.strip()
                    else None}


def set_node_style(section: dict, name: str, *, color=_UNSET, shape=_UNSET,
                   role=_UNSET) -> None:
    """Изменение оформления узла. Аргумент, равный None, сбрасывает поле;
    аргумент по умолчанию (_UNSET) — не меняет."""
    nodes = section.setdefault("nodes", {})
    entry = nodes.setdefault(name, {})
    if color is not _UNSET:
        cleaned = sanitize_color(color)
        if cleaned:
            entry["color"] = cleaned
        else:
            entry.pop("color", None)
    if shape is not _UNSET:
        entry["shape"] = normalize_shape(shape)
    if role is not _UNSET:
        cleaned_role = role.strip() if isinstance(role, str) else ""
        if cleaned_role:
            entry["role"] = cleaned_role
        else:
            entry.pop("role", None)
    if not entry:
        nodes.pop(name, None)
    if not nodes:
        section.pop("nodes", None)


def node_image(section: dict, name: str) -> str | None:
    """Data-URI изображения узла (пиктограммы) или None."""
    nodes = section.get("nodes")
    entry = nodes.get(name) if isinstance(nodes, dict) else None
    if not isinstance(entry, dict):
        return None
    return sanitize_node_image(entry.get("image"))


def set_node_image(section: dict, name: str, data_uri) -> None:
    """Запись изображения узла; невалидный data-URI отбрасывается
    (ключ удаляется) — по образцу сброса цвета в set_node_style."""
    nodes = section.setdefault("nodes", {})
    entry = nodes.setdefault(name, {})
    cleaned = sanitize_node_image(data_uri)
    if cleaned:
        entry["image"] = cleaned
    else:
        entry.pop("image", None)
    if not entry:
        nodes.pop(name, None)
    if not nodes:
        section.pop("nodes", None)


def clear_node_image(section: dict, name: str) -> None:
    """Удаление изображения узла."""
    set_node_image(section, name, None)


def link_labels_from_section(section: dict) -> dict[tuple[str, str], str]:
    """Подписи каналов {(a, b) отсортированная пара: текст}."""
    labels: dict[tuple[str, str], str] = {}
    records = section.get("link_labels")
    if not isinstance(records, list):
        return labels
    for record in records:
        if not isinstance(record, dict):
            continue
        a, b, label = record.get("a"), record.get("b"), record.get("label")
        if isinstance(a, str) and isinstance(b, str) and a != b \
                and isinstance(label, str) and label.strip():
            labels[tuple(sorted((a, b)))] = label.strip()
    return labels


def set_link_label(section: dict, a: str, b: str, label: str) -> None:
    """Установка/замена подписи канала; пустой текст — удаление."""
    key = tuple(sorted((a, b)))
    records = [record for record in section.get("link_labels", [])
               if isinstance(record, dict)
               and tuple(sorted((str(record.get("a")), str(record.get("b"))))) != key]
    if label.strip():
        records.append({"a": a, "b": b, "label": label.strip()})
    if records:
        section["link_labels"] = records
    else:
        section.pop("link_labels", None)


def link_styles_from_section(section: dict) -> dict[tuple[str, str], dict]:
    """Оформление связей {(a, b) отсортированная пара: очищенная запись}."""
    styles: dict[tuple[str, str], dict] = {}
    records = section.get("link_styles")
    if not isinstance(records, list):
        return styles
    for record in records:
        item = _clean_link_style_record(record)
        if item is not None:
            styles[tuple(sorted((item["a"], item["b"])))] = item
    return styles


def link_style(section: dict, a: str, b: str) -> dict:
    """Оформление связи с дефолтами: {'color', 'style', 'width',
    'arrow_start', 'arrow_end', 'label_start', 'label_end'}."""
    record = link_styles_from_section(section).get(tuple(sorted((a, b)))) or {}
    start = record.get("label_start")
    end = record.get("label_end")
    return {"color": sanitize_color(record.get("color")) or DEFAULT_LINK_COLOR,
            "style": normalize_line_style(record.get("style")),
            "width": normalize_link_width(record.get("width", DEFAULT_LINK_WIDTH)),
            "arrow_start": normalize_arrow_style(record.get("arrow_start")),
            "arrow_end": normalize_arrow_style(record.get("arrow_end")),
            "label_start": start.strip() if isinstance(start, str) else "",
            "label_end": end.strip() if isinstance(end, str) else ""}


def set_link_style(section: dict, a: str, b: str, **attrs) -> None:
    """Изменение оформления связи. Переданный ключ меняет поле; значение None
    (для подписей — также пустая строка) сбрасывает его к дефолту."""
    key = tuple(sorted((a, b)))
    records = section.get("link_styles")
    if not isinstance(records, list):
        records = []
    entry = None
    for record in records:
        if isinstance(record, dict) \
                and tuple(sorted((str(record.get("a")), str(record.get("b"))))) == key:
            entry = record
            break
    if entry is None:
        entry = {"a": a, "b": b}
        records.append(entry)
    if "color" in attrs:
        cleaned = sanitize_color(attrs["color"])
        if cleaned:
            entry["color"] = cleaned
        else:
            entry.pop("color", None)
    if "style" in attrs:
        if attrs["style"] is None:
            entry.pop("style", None)
        else:
            entry["style"] = normalize_line_style(attrs["style"])
    if "width" in attrs:
        if attrs["width"] is None:
            entry.pop("width", None)
        else:
            entry["width"] = normalize_link_width(attrs["width"])
    for name in ("arrow_start", "arrow_end"):
        if name in attrs:
            if attrs[name] is None:
                entry.pop(name, None)
            else:
                entry[name] = normalize_arrow_style(attrs[name])
    for name in ("label_start", "label_end"):
        if name in attrs:
            label = attrs[name]
            label = label.strip() if isinstance(label, str) else ""
            if label:
                entry[name] = label
            else:
                entry.pop(name, None)
    if not any(name in entry for name in
               ("color", "style", "width", "arrow_start", "arrow_end",
                "label_start", "label_end")):
        records.remove(entry)
    if records:
        section["link_styles"] = records
    else:
        section.pop("link_styles", None)


def drop_link_style(section: dict, a: str, b: str) -> None:
    """Удаление записи оформления связи."""
    key = tuple(sorted((a, b)))
    records = [record for record in section.get("link_styles", [])
               if isinstance(record, dict)
               and tuple(sorted((str(record.get("a")), str(record.get("b"))))) != key]
    if records:
        section["link_styles"] = records
    else:
        section.pop("link_styles", None)


def link_line_trim(x1: float, y1: float, x2: float, y2: float,
                   pad: float = 6.0) -> tuple[float, float, float, float,
                                              float, float]:
    """Концы линии, подрезанные у границ прямоугольников узлов, и единичный
    вектор направления от первого узла ко второму. Стрелки и подписи концов
    ставятся на подрезанные концы, чтобы не залезать на узлы."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return x1, y1, x2, y2, 0.0, 0.0
    ux, uy = dx / length, dy / length
    half_w, half_h = NODE_W / 2.0, NODE_H / 2.0
    reach = min(half_w / abs(ux) if abs(ux) > 1e-9 else math.inf,
                half_h / abs(uy) if abs(uy) > 1e-9 else math.inf)
    # на коротких связях подрезки не должны пересечься
    reach = min(reach, length / 2.0)
    return (x1 + ux * (reach + pad), y1 + uy * (reach + pad),
            x2 - ux * (reach + pad), y2 - uy * (reach + pad), ux, uy)


def rename_node_in_gui(section: dict, old: str, new: str) -> None:
    """Перенос метаданных узла при переименовании."""
    for key in ("positions", "nodes"):
        mapping = section.get(key)
        if isinstance(mapping, dict) and old in mapping:
            mapping[new] = mapping.pop(old)
    for key in ("link_labels", "link_styles"):
        for record in section.get(key, []):
            if not isinstance(record, dict):
                continue
            if record.get("a") == old:
                record["a"] = new
            if record.get("b") == old:
                record["b"] = new


def drop_node_from_gui(section: dict, name: str) -> None:
    """Удаление метаданных узла и записей инцидентных связей."""
    for key in ("positions", "nodes"):
        mapping = section.get(key)
        if isinstance(mapping, dict):
            mapping.pop(name, None)
    records = [record for record in section.get("link_labels", [])
               if isinstance(record, dict)
               and name not in (record.get("a"), record.get("b"))]
    if records:
        section["link_labels"] = records
    else:
        section.pop("link_labels", None)
    drop_link_records = [record for record in section.get("link_styles", [])
                         if isinstance(record, dict)
                         and name not in (record.get("a"), record.get("b"))]
    if drop_link_records:
        section["link_styles"] = drop_link_records
    else:
        section.pop("link_styles", None)


def drop_link_from_gui(section: dict, a: str, b: str) -> None:
    """Удаление метаданных связи: подпись канала и оформление."""
    set_link_label(section, a, b, "")
    drop_link_style(section, a, b)


def prune_gui_section(section: dict, names, links) -> None:
    """Отбрасывание записей об узлах/связях, которых больше нет в системе."""
    keep = set(names)
    for key in ("positions", "nodes"):
        mapping = section.get(key)
        if isinstance(mapping, dict):
            for stale in [name for name in mapping if name not in keep]:
                del mapping[stale]
    link_keys = {tuple(sorted((str(a), str(b)))) for a, b in links}
    records = [record for record in section.get("link_labels", [])
               if isinstance(record, dict)
               and tuple(sorted((str(record.get("a")), str(record.get("b")))))
               in link_keys]
    if records:
        section["link_labels"] = records
    else:
        section.pop("link_labels", None)
    style_records = [record for record in section.get("link_styles", [])
                     if isinstance(record, dict)
                     and tuple(sorted((str(record.get("a")), str(record.get("b")))))
                     in link_keys]
    if style_records:
        section["link_styles"] = style_records
    else:
        section.pop("link_styles", None)


# ---------------------------------------------------------------------------
# Макет по умолчанию: узлы по кругу (если явные координаты не заданы)
# ---------------------------------------------------------------------------


def circle_layout(names: list[str], width: float = 900.0,
                  height: float = 640.0, margin: float = 110.0) -> dict[str, tuple[float, float]]:
    """Координаты центров узлов по окружности (для канваса и SVG)."""
    n = len(names)
    if n == 0:
        return {}
    if n == 1:
        return {names[0]: (width / 2, height / 2)}
    cx, cy = width / 2, height / 2
    radius = max(60.0, min(width, height) / 2 - margin)
    return {
        name: (cx + radius * math.cos(2 * math.pi * i / n - math.pi / 2),
               cy + radius * math.sin(2 * math.pi * i / n - math.pi / 2))
        for i, name in enumerate(names)
    }


# ---------------------------------------------------------------------------
# SVG: структурная схема сети
# ---------------------------------------------------------------------------

NODE_W, NODE_H = 130.0, 48.0


def _heat_color(value: float) -> str:
    """Шкала зелёный → жёлтый → красный для значений из [0; 1]."""
    value = max(0.0, min(1.0, value))
    if value < 0.5:
        # зелёный → жёлтый
        t = value / 0.5
        r, g, b = 110 + (240 - 110) * t, 200 + (190 - 200) * t, 100 - 30 * t
    else:
        # жёлтый → красный
        t = (value - 0.5) / 0.5
        r, g, b = 240 - 10 * t, 190 - 110 * t, 70 - 20 * t
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def _node_shape_markup(shape: str, x: float, y: float, attrs: str) -> str:
    """Разметка контура фигуры узла с центром в (x, y); attrs — хвост
    атрибутов (заливка/обводка либо пустая строка для clipPath)."""
    if shape == "oval":
        return (f'<ellipse cx="{x:.0f}" cy="{y:.0f}" '
                f'rx="{NODE_W / 2:.0f}" ry="{NODE_H / 2:.0f}" {attrs}/>')
    if shape == "trapezoid":
        inset = 20.0
        points = [(x - NODE_W / 2 + inset, y - NODE_H / 2),
                  (x + NODE_W / 2 - inset, y - NODE_H / 2),
                  (x + NODE_W / 2, y + NODE_H / 2),
                  (x - NODE_W / 2, y + NODE_H / 2)]
    elif shape == "diamond":
        grow = 8.0
        points = [(x, y - NODE_H / 2 - grow),
                  (x + NODE_W / 2 + grow, y),
                  (x, y + NODE_H / 2 + grow),
                  (x - NODE_W / 2 - grow, y)]
    else:  # rectangle
        return (f'<rect x="{x - NODE_W / 2:.0f}" y="{y - NODE_H / 2:.0f}" '
                f'width="{NODE_W:.0f}" height="{NODE_H:.0f}" rx="9" {attrs}/>')
    pts = " ".join(f"{px:.0f},{py:.0f}" for px, py in points)
    return f'<polygon points="{pts}" {attrs}/>'


def _node_shape_svg(shape: str, x: float, y: float,
                    fill: str, stroke: str) -> str:
    """SVG-элемент фигуры узла с центром в (x, y)."""
    return "  " + _node_shape_markup(
        shape, x, y, f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"')


def _node_image_box(shape: str, x: float, y: float) -> tuple[float, float,
                                                              float, float]:
    """Ограничивающий прямоугольник фигуры (x, y, ширина, высота) —
    область размещения пиктограммы внутри контура."""
    if shape == "diamond":
        grow = 8.0
        return (x - NODE_W / 2 - grow, y - NODE_H / 2 - grow,
                NODE_W + 2 * grow, NODE_H + 2 * grow)
    return x - NODE_W / 2, y - NODE_H / 2, NODE_W, NODE_H


def topology_svg(system: SystemInput,
                 node_values: dict[str, float] | None = None,
                 color_by: str = "q",
                 positions: dict[str, tuple[float, float]] | None = None,
                 title: str | None = None,
                 styles: dict[str, dict] | None = None,
                 link_labels: dict[tuple[str, str], str] | None = None,
                 link_styles: dict[tuple[str, str], dict] | None = None,
                 images: dict[str, str] | None = None
                 ) -> str:
    """Схема топологии в SVG.

    node_values — значения раскраски по узлам (Q_i или критичность I_i);
    color_by — подпись величины в легенде и на узлах ("q" или "I");
    positions — явный макет {имя: (x, y)}, иначе — раскладка по кругу;
    styles — оформление узлов {имя: {"color": "#rrggbb"|None,
    "shape": "rectangle"|"oval"|"trapezoid"|"diamond"}} (прочее игнорируется);
    link_labels — подписи каналов {(a, b): текст};
    link_styles — оформление связей {(a, b): запись link_styles} (цвет, тип
    линии, толщина, стрелки, подписи концов); связь без записи рисуется
    прежним стилем `.e` (дефолтный серый);
    images — пиктограммы узлов {имя: data-URI}: рисуются внутри контура
    фигуры (`<image>` + `<clipPath>` по тому же контуру).
    """
    names = [node.name for node in system.nodes]
    if positions is None:
        positions = circle_layout(names)
    width, height = 900.0, 640.0
    if positions:
        width = max(x for x, _ in positions.values()) + NODE_W + 60
        height = max(y for _, y in positions.values()) + NODE_H + 90
    title = title if title is not None else f"Топология: {system.name}"
    image_map = {name: sanitize_node_image(uri)
                 for name, uri in (images or {}).items()}
    image_map = {name: uri for name, uri in image_map.items() if uri}
    has_images = any(name in image_map for name in names)

    xlink_ns = (' xmlns:xlink="http://www.w3.org/1999/xlink"'
                if has_images else '')
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg"{xlink_ns} '
        f'width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'font-family="Segoe UI, Arial, sans-serif">',
        '  <style>.t { font-size:13px; fill:#111; } '
        '.s { font-size:11px; fill:#222; } .e { stroke:#666; stroke-width:2; } '
        '.tt { font-size:16px; font-weight:bold; fill:#111; } '
        '.lg { font-size:11px; fill:#333; } '
        '.lbl { font-size:11px; fill:#333; stroke:#fff; stroke-width:3px; '
        'paint-order:stroke; }</style>',
        f'  <text x="20" y="28" class="tt">{escape(title)}</text>',
    ]
    labels = link_labels or {}
    style_map = link_styles or {}
    link_parts: list[str] = []
    marker_defs: list[str] = []
    image_defs: list[str] = []
    for index, (a, b) in enumerate(system.links):
        if a not in positions or b not in positions:
            continue
        x1, y1 = positions[a]
        x2, y2 = positions[b]
        label = labels.get(tuple(sorted((a, b))))
        record = style_map.get(tuple(sorted((a, b))))
        if record is None:
            # Связь без оформления — прежний вывод (дефолтный стиль `.e`).
            link_parts.append(
                f'  <line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" class="e"/>'
            )
        else:
            color = sanitize_color(record.get("color")) or DEFAULT_LINK_COLOR
            line_style = normalize_line_style(record.get("style"))
            line_width = normalize_link_width(
                record.get("width", DEFAULT_LINK_WIDTH))
            arrow_start = normalize_arrow_style(record.get("arrow_start"))
            arrow_end = normalize_arrow_style(record.get("arrow_end"))
            sx, sy, ex, ey, ux, uy = link_line_trim(x1, y1, x2, y2)
            attrs = f'stroke="{color}" stroke-width="{line_width}"'
            dash = SVG_DASHARRAY.get(line_style)
            if dash:
                attrs += f' stroke-dasharray="{dash}"'
            if arrow_start == "arrow" or arrow_end == "arrow":
                # Один маркер на оба конца: для начала отражается
                # orient="auto-start-reverse". Размер — от толщины линии.
                marker_id = f"la{index}"
                marker_size = 3 + line_width
                marker_defs.append(
                    f'    <marker id="{marker_id}" viewBox="0 0 10 10" '
                    f'refX="8.5" refY="5" markerWidth="{marker_size}" '
                    f'markerHeight="{marker_size}" '
                    f'orient="auto-start-reverse">'
                    f'<path d="M 0 0.8 L 8.5 5 L 0 9.2 z" fill="{color}"/>'
                    f'</marker>')
                if arrow_start == "arrow":
                    attrs += f' marker-start="url(#{marker_id})"'
                if arrow_end == "arrow":
                    attrs += f' marker-end="url(#{marker_id})"'
            link_parts.append(
                f'  <line x1="{sx:.0f}" y1="{sy:.0f}" x2="{ex:.0f}" '
                f'y2="{ey:.0f}" {attrs}/>')
            start_label = record.get("label_start")
            end_label = record.get("label_end")
            if (isinstance(start_label, str) and start_label.strip()) or \
                    (isinstance(end_label, str) and end_label.strip()):
                # Подписи выносятся за границу узла по оси линии («наружу»),
                # чтобы не перекрывать узел и линию.
                d1 = math.hypot(sx - x1, sy - y1) + 12
                d2 = math.hypot(ex - x2, ey - y2) + 12
                for text, lx, ly in (
                        (start_label, x1 - ux * d1, y1 - uy * d1),
                        (end_label, x2 + ux * d2, y2 + uy * d2)):
                    if isinstance(text, str) and text.strip():
                        link_parts.append(
                            f'  <text x="{lx:.0f}" y="{ly + 4:.0f}" '
                            f'text-anchor="middle" class="lbl">'
                            f'{escape(text.strip())}</text>')
        if label:
            link_parts.append(
                f'  <text x="{(x1 + x2) / 2:.0f}" y="{(y1 + y2) / 2 - 4:.0f}" '
                f'text-anchor="middle" class="lbl">{escape(label)}</text>'
            )
    parts.extend(link_parts)
    node_parts: list[str] = []
    for name in names:
        x, y = positions[name]
        value = node_values.get(name) if node_values else None
        style = (styles or {}).get(name) or {}
        color = sanitize_color(style.get("color"))
        shape = normalize_shape(style.get("shape"))
        fill = (_heat_color(value) if value is not None
                else color or DEFAULT_NODE_COLOR)
        stroke = "#333" if value is not None else "#1155cc"
        node_parts.append(_node_shape_svg(shape, x, y, fill, stroke))
        image = image_map.get(name)
        if image:
            # Пиктограмма поверх заливки, обрезанная контуром фигуры;
            # href + xlink:href — совместимость с браузерами и Inkscape.
            clip_id = f"nic{len(image_defs)}"
            image_defs.append(
                f'    <clipPath id="{clip_id}">'
                f'{_node_shape_markup(shape, x, y, "")}</clipPath>')
            ix, iy, iw, ih = _node_image_box(shape, x, y)
            node_parts.append(
                f'  <image x="{ix:.0f}" y="{iy:.0f}" width="{iw:.0f}" '
                f'height="{ih:.0f}" preserveAspectRatio="xMidYMid meet" '
                f'clip-path="url(#{clip_id})" '
                f'href="{image}" xlink:href="{image}"/>')
        label = name if len(name) <= 20 else name[:19] + "…"
        node_parts.append(f'  <text x="{x:.0f}" y="{y - 3:.0f}" text-anchor="middle" class="t">'
                          f'{escape(label)}</text>')
        if value is not None:
            node_parts.append(
                f'  <text x="{x:.0f}" y="{y + 14:.0f}" text-anchor="middle" class="s">'
                f'{color_by} = {value:.3f}</text>'
            )
    if marker_defs or image_defs:
        parts.append("  <defs>")
        parts.extend(marker_defs)
        parts.extend(image_defs)
        parts.append("  </defs>")
    parts.extend(node_parts)
    if node_values:
        # Легенда: градиентная шкала от минимума до максимума значения.
        vals = list(node_values.values())
        lo, hi = min(vals), max(vals)
        parts.append('  <defs><linearGradient id="heat" x1="0" y1="0" x2="1" y2="0">'
                     '<stop offset="0" stop-color="#6ec864"/>'
                     '<stop offset="0.5" stop-color="#f0be46"/>'
                     '<stop offset="1" stop-color="#e65a32"/></linearGradient></defs>')
        parts.append(f'  <rect x="20" y="{height - 44:.0f}" width="180" height="12" fill="url(#heat)"/>')
        parts.append(f'  <text x="20" y="{height - 50:.0f}" class="lg">раскраска по {escape(color_by)}</text>')
        parts.append(f'  <text x="20" y="{height - 22:.0f}" class="lg">{lo:.3f}</text>')
        parts.append(f'  <text x="170" y="{height - 22:.0f}" text-anchor="end" class="lg">{hi:.3f}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Graphviz DOT (опциональный текстовый графовый формат для внешней отрисовки)
# ---------------------------------------------------------------------------


def _dot_link_attrs(record: dict | None) -> str:
    """Атрибуты ребра Graphviz из записи оформления связи."""
    if not isinstance(record, dict):
        return ""
    attrs: list[str] = []
    color = sanitize_color(record.get("color"))
    if color:
        attrs.append(f'color="{color}"')
    gv_style = []
    if normalize_line_style(record.get("style")) in ("dash", "dashdot"):
        gv_style.append("dashed")
    if normalize_link_width(record.get("width", DEFAULT_LINK_WIDTH)) >= 4:
        gv_style.append("bold")
    if gv_style:
        attrs.append(f'style="{",".join(gv_style)}"')
    start = normalize_arrow_style(record.get("arrow_start"))
    end = normalize_arrow_style(record.get("arrow_end"))
    if start == "arrow" and end == "arrow":
        attrs.append("dir=both")
    elif start == "arrow":
        attrs.append("dir=back")
    elif end == "arrow":
        attrs.append("dir=forward")
    if start == "arrow":
        attrs.append("arrowtail=normal")
    if end == "arrow":
        attrs.append("arrowhead=normal")
    return ", ".join(attrs)


def topology_dot(system: SystemInput,
                 styles: dict[str, dict] | None = None,
                 link_styles: dict[tuple[str, str], dict] | None = None) -> str:
    """Топология в формате Graphviz DOT (неориентированный граф).

    styles — оформление узлов: фигуры маппятся на атрибут shape Graphviz;
    link_styles — оформление связей {(a, b): запись link_styles}
    (цвет, style=dashed/bold, стрелки — как метаданные ребра).
    """
    index = {node.name: f"n{i}" for i, node in enumerate(system.nodes)}
    lines = [f'graph "{system.name}" {{', "  node [shape=box, style=rounded];"]
    for name, node_id in index.items():
        shape = DOT_SHAPES[normalize_shape(
            ((styles or {}).get(name) or {}).get("shape"))]
        shape_attr = "" if shape == "box" else f", shape={shape}"
        lines.append(f'  {node_id} [label="{escape(name)}"{shape_attr}];')
    style_map = link_styles or {}
    for a, b in system.links:
        attrs = _dot_link_attrs(style_map.get(tuple(sorted((a, b)))))
        attr_text = f" [{attrs}]" if attrs else ""
        lines.append(f"  {index[a]} -- {index[b]}{attr_text};")
    lines.append("}")
    return "\n".join(lines)
