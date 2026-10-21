"""Тесты кодека проекта и экспорта топологии — без Qt (стандартный unittest).

Проверяется: round-trip словарь проекта ↔ модели (тот же формат, что у CLI),
секция `gui` (роли, координаты, цвета, фигуры, подписи каналов), рендереры
SVG/DOT, а также независимость ядра приложения от PyQt.
"""

import importlib
import json
import sys
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path

from survivability.io import (job_from_json, protection_from_dict,
                              system_from_dict)
from survivability.model import ValidationError
from survivability.project_codec import (
    DEFAULT_NODE_COLOR, DEFAULT_ROLES, NODE_SHAPES, builtin_roles,
    circle_layout, drop_link_from_gui, drop_node_from_gui, gui_section,
    link_labels_from_section, node_style, normalize_shape, positions_from_section,
    prune_gui_section, project_to_dict, rename_node_in_gui,
    sanitize_gui_section, sanitize_color, set_link_label, set_node_style,
    set_positions, system_to_dict, topology_dot, topology_svg, user_roles,
    validate_role)
from survivability.system_calc import node_importance, system_qs

PROJECT_DICT = {
    "system": {
        "name": "тестовая сеть",
        "nodes": [
            {"name": "А", "own_failure_rate": 0.01, "recovery_rate": 4.0,
             "period": 1.0,
             "impacts": [{"name": "DDoS", "intensity": 2.0,
                          "defeat_probability": 0.3}]},
            {"name": "Б", "own_failure_rate": 0.02, "recovery_rate": 2.0,
             "period": 1.0, "impacts": []},
            {"name": "В", "own_failure_rate": 0.05, "recovery_rate": 1.0,
             "period": 1.0,
             "impacts": [{"name": "фишинг", "intensity": 0.3,
                          "defeat_probability": 0.4}]},
        ],
        "links": [["А", "Б"], ["Б", "В"]],
        "w_def": 0.3333333333333333,
        "target": "connectivity",
        "monte_carlo_runs": 5000,
        "seed": 42,
    },
    "protection": {
        "budget": 50,
        "variants": [
            {"name": "экранирование", "cost": 25,
             "nodes": {"А": {"impacts": [
                 {"name": "DDoS", "intensity": 2.0,
                  "defeat_probability": 0.1}]}},
             "links_added": [["А", "В"]]},
            {"name": "пустой", "cost": 5},
        ],
    },
}


class TestRoundTrip(unittest.TestCase):
    def test_dict_model_dict_no_losses(self):
        # Словарь → модели → словарь: данные не теряются (формат CLI)
        system = system_from_dict(PROJECT_DICT["system"])
        protection = protection_from_dict(PROJECT_DICT["protection"], system)
        restored = project_to_dict(system, protection)
        self.assertEqual(restored, PROJECT_DICT)

    def test_system_only_project(self):
        data = {"system": PROJECT_DICT["system"]}
        system = system_from_dict(data["system"])
        restored = project_to_dict(system)
        self.assertEqual(restored, data)
        self.assertNotIn("protection", restored)

    def test_seed_none_omitted(self):
        data = json.loads(json.dumps(PROJECT_DICT["system"]))
        del data["seed"]
        system = system_from_dict(data)
        self.assertIsNone(system.seed)
        self.assertNotIn("seed", system_to_dict(system))

    def test_saved_project_opens_in_cli_parser(self):
        # Файл, сохранённый GUI-моделью, читается штатным разборщиком CLI
        import tempfile
        from pathlib import Path
        system = system_from_dict(PROJECT_DICT["system"])
        protection = protection_from_dict(PROJECT_DICT["protection"], system)
        payload = project_to_dict(system, protection)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            path.write_text(json.dumps(payload, ensure_ascii=False),
                            encoding="utf-8")
            job = job_from_json(path)
        self.assertIsNotNone(job.system)
        self.assertIsNotNone(job.protection)
        self.assertEqual(len(job.system.nodes), 3)
        self.assertEqual(len(job.protection.variants), 2)


class TestSvgExport(unittest.TestCase):
    def setUp(self):
        self.system = system_from_dict(PROJECT_DICT["system"])
        estimate = system_qs(self.system)
        self.importance = {item.name: item.criticality
                           for item in node_importance(self.system, estimate)}

    def test_svg_is_valid_xml(self):
        svg = topology_svg(self.system)
        doc = xml.dom.minidom.parseString(svg)
        self.assertEqual(doc.documentElement.tagName, "svg")

    def test_svg_contains_nodes_and_edges(self):
        q_values = {"А": 0.9, "Б": 0.8, "В": 0.7}
        svg = topology_svg(self.system, node_values=q_values, color_by="Q")
        for name in ("А", "Б", "В"):
            self.assertIn(name, svg)
        for value in ("0.900", "0.800", "0.700"):
            self.assertIn(f"Q = {value}", svg)
        # Число рёбер = числу связей (линий столько же, сколько пар в links)
        self.assertEqual(svg.count("<line "), len(self.system.links))
        # Легенда раскраски
        self.assertIn("раскраска по Q", svg)

    def test_svg_importance_coloring(self):
        svg = topology_svg(self.system, node_values=self.importance,
                           color_by="I")
        self.assertIn("раскраска по I", svg)

    def test_svg_escapes_markup_in_names(self):
        system = system_from_dict({
            "nodes": [{"name": "Узел <X> & \"Y\"", "own_failure_rate": 0.1,
                       "period": 1.0, "impacts": []}],
            "w_def": 0.5,
        })
        svg = topology_svg(system)
        xml.dom.minidom.parseString(svg)  # не должно падать на < > & "
        self.assertNotIn("<X>", svg)

    def test_custom_positions_and_layout(self):
        names = ["a", "b", "c"]
        layout = circle_layout(names)
        self.assertEqual(set(layout), set(names))
        svg = topology_svg(self.system, positions={
            "А": (100.0, 100.0), "Б": (300.0, 100.0), "В": (200.0, 260.0)})
        self.assertIn('x1="100"', svg)
        self.assertIn('x2="300"', svg)


class TestDotExport(unittest.TestCase):
    def test_dot_contains_nodes_and_edges(self):
        system = system_from_dict(PROJECT_DICT["system"])
        dot = topology_dot(system)
        self.assertIn('"А"', dot)
        self.assertIn('"Б"', dot)
        self.assertIn("--", dot)
        lines = [line for line in dot.splitlines() if "--" in line]
        self.assertEqual(len(lines), len(system.links))


class TestCoreIndependentOfQt(unittest.TestCase):
    def test_cli_and_calc_work_without_pyqt(self):
        # Блокируем импорт Qt-пакетов: ядро и CLI должны работать без GUI
        blocked = {}
        for name in list(sys.modules):
            if name.split(".")[0] in ("PyQt6", "PySide6"):
                blocked[name] = sys.modules.pop(name)
        saved = {key: sys.modules.get(key) for key in ("PyQt6", "PySide6")}
        sys.modules["PyQt6"] = None
        sys.modules["PySide6"] = None
        try:
            with self.assertRaises(ImportError):
                importlib.import_module("PyQt6.QtWidgets")
            for module in ("survivability", "survivability.cli",
                           "survivability.system_calc",
                           "survivability.protection"):
                importlib.import_module(module)
            estimate = system_qs(system_from_dict(PROJECT_DICT["system"]))
            self.assertGreater(estimate.q, 0.0)
        finally:
            sys.modules.pop("PyQt6", None)
            sys.modules.pop("PySide6", None)
            for key, value in saved.items():
                if value is not None:
                    sys.modules[key] = value
            sys.modules.update(blocked)


class TestGuiSection(unittest.TestCase):
    """Секция `gui`: очистка, round-trip, метаданные узлов и связей."""

    def setUp(self):
        self.system = system_from_dict(PROJECT_DICT["system"])

    def gui_sample(self) -> dict:
        return {
            "positions": {"А": [100.0, 120.0], "Б": [300.0, 120.0]},
            "nodes": {"А": {"color": "#FF8800", "shape": "oval",
                            "role": "Сервер"}},
            "link_labels": [{"a": "А", "b": "Б", "label": "оптоволокно"}],
            "roles": [{"name": "Шлюз", "own_failure_rate": 0.01,
                       "recovery_rate": 4.0, "period": 1.0, "impacts": []}],
        }

    def test_sanitize_roundtrip_via_json(self):
        section = sanitize_gui_section(self.gui_sample())
        restored = sanitize_gui_section(
            json.loads(json.dumps(section, ensure_ascii=False)))
        self.assertEqual(restored, section)

    def test_sanitize_drops_garbage(self):
        dirty = {"positions": {"А": ["x", 1], "Б": [1, 2, 3], "В": [1, 2]},
                 "nodes": {"А": {"color": "красный", "shape": "круг"},
                           "Б": "не словарь",
                           "В": {"shape": "diamond", "role": "  "}},
                 "link_labels": [{"a": "А", "b": "А", "label": "петля"},
                                 {"a": "А", "b": "Б"},
                                 {"a": "А", "b": "Б", "label": "  "},
                                 {"a": "А", "b": "Б", "label": "VPN"}],
                 "roles": [{"name": "плохая", "own_failure_rate": -1},
                           {"name": "хорошая", "period": 1.0}]}
        section = sanitize_gui_section(dirty)
        self.assertEqual(section["positions"], {"В": [1.0, 2.0]})
        self.assertEqual(section["nodes"], {"В": {"shape": "diamond"}})
        self.assertEqual(section["link_labels"],
                         [{"a": "А", "b": "Б", "label": "VPN"}])
        self.assertEqual([r["name"] for r in section["roles"]], ["хорошая"])

    def test_sanitize_tolerates_anything(self):
        for garbage in (None, 42, "gui", [1, 2], {"positions": "x"}):
            self.assertEqual(sanitize_gui_section(garbage), {})

    def test_positions_from_and_to_section(self):
        section = sanitize_gui_section(self.gui_sample())
        positions = positions_from_section(section)
        self.assertEqual(positions, {"А": (100.0, 120.0),
                                     "Б": (300.0, 120.0)})
        section2: dict = {}
        set_positions(section2, positions)
        self.assertEqual(section2["positions"],
                         {"А": [100.0, 120.0], "Б": [300.0, 120.0]})
        set_positions(section2, {})
        self.assertNotIn("positions", section2)

    def test_node_style_defaults_and_overrides(self):
        section = sanitize_gui_section(self.gui_sample())
        self.assertEqual(node_style(section, "А"),
                         {"color": "#ff8800", "shape": "oval",
                          "role": "Сервер"})
        self.assertEqual(node_style(section, "Б"),
                         {"color": None, "shape": "rectangle",
                          "role": None})
        self.assertEqual(node_style({}, "Чужой"),
                         {"color": None, "shape": "rectangle",
                          "role": None})

    def test_set_node_style_and_reset(self):
        section: dict = {}
        set_node_style(section, "А", color="#FF0000", shape="diamond",
                       role="Сервер")
        self.assertEqual(node_style(section, "А"),
                         {"color": "#ff0000", "shape": "diamond",
                          "role": "Сервер"})
        set_node_style(section, "А", color=None, role=None)
        self.assertEqual(node_style(section, "А"),
                         {"color": None, "shape": "diamond", "role": None})
        set_node_style(section, "А", shape="rectangle")
        # Явно заданная фигура по умолчанию — допустимая запись
        self.assertEqual(node_style(section, "А"),
                         {"color": None, "shape": "rectangle", "role": None})

    def test_normalize_shape_and_color(self):
        self.assertEqual(normalize_shape("oval"), "oval")
        self.assertEqual(normalize_shape("круг"), "rectangle")
        self.assertEqual(sanitize_color("#Ab12f0"), "#ab12f0")
        self.assertIsNone(sanitize_color("red"))
        self.assertIsNone(sanitize_color("#12345"))

    def test_link_label_lifecycle(self):
        section: dict = {}
        set_link_label(section, "Б", "А", "оптоволокно")  # порядок не важен
        self.assertEqual(link_labels_from_section(section),
                         {("А", "Б"): "оптоволокно"})
        set_link_label(section, "А", "Б", "радиоканал")
        self.assertEqual(link_labels_from_section(section),
                         {("А", "Б"): "радиоканал"})
        set_link_label(section, "А", "Б", "  ")
        self.assertEqual(link_labels_from_section(section), {})
        self.assertNotIn("link_labels", section)

    def test_rename_moves_metadata(self):
        section = sanitize_gui_section(self.gui_sample())
        rename_node_in_gui(section, "А", "А-новый")
        self.assertIn("А-новый", section["positions"])
        self.assertIn("А-новый", section["nodes"])
        self.assertNotIn("А", section["positions"])
        self.assertEqual(section["link_labels"][0]["a"], "А-новый")

    def test_drop_node_removes_incident_labels(self):
        section = sanitize_gui_section(self.gui_sample())
        drop_node_from_gui(section, "Б")
        self.assertNotIn("Б", section["positions"])
        self.assertNotIn("link_labels", section)  # пустой список не хранится
        # А и его оформление не пострадали
        self.assertIn("А", section["nodes"])

    def test_drop_link_removes_label(self):
        section = sanitize_gui_section(self.gui_sample())
        drop_link_from_gui(section, "Б", "А")
        self.assertEqual(link_labels_from_section(section), {})

    def test_prune_removes_stale_entries(self):
        section = sanitize_gui_section(self.gui_sample())
        prune_gui_section(section, ["А"], [])  # Б и связь А—Б исчезли
        self.assertNotIn("Б", section["positions"])
        self.assertNotIn("link_labels", section)
        self.assertIn("А", section["nodes"])

    def test_cli_ignores_gui_section(self):
        # Файл с секцией gui читается CLI так же, как без неё
        payload = project_to_dict(self.system)
        payload["gui"] = self.gui_sample()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            path.write_text(json.dumps(payload, ensure_ascii=False),
                            encoding="utf-8")
            job_with = job_from_json(path)
        plain = project_to_dict(self.system)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            path.write_text(json.dumps(plain, ensure_ascii=False),
                            encoding="utf-8")
            job_without = job_from_json(path)
        self.assertEqual(system_to_dict(job_with.system),
                         system_to_dict(job_without.system))


class TestSvgStylesAndLabels(unittest.TestCase):
    """SVG/DOT: фигуры узлов, ручные цвета, подписи каналов."""

    def setUp(self):
        self.system = system_from_dict(PROJECT_DICT["system"])

    def test_svg_shapes(self):
        styles = {"А": {"shape": "oval"},
                  "Б": {"shape": "trapezoid"},
                  "В": {"shape": "diamond"}}
        svg = topology_svg(self.system, styles=styles)
        xml.dom.minidom.parseString(svg)  # валидный XML
        self.assertIn("<ellipse", svg)
        self.assertEqual(svg.count("<polygon"), 2)
        self.assertNotIn("<rect", svg)  # прямоугольников среди узлов нет

    def test_svg_default_shape_is_rectangle(self):
        svg = topology_svg(self.system)
        self.assertEqual(svg.count("<rect"), 3)  # все узлы — rect

    def test_svg_manual_color(self):
        styles = {"А": {"color": "#FF0000"}}
        svg = topology_svg(self.system, styles=styles)
        self.assertIn('fill="#ff0000"', svg)
        # Остальные — цвет по умолчанию
        self.assertIn(f'fill="{DEFAULT_NODE_COLOR}"', svg)

    def test_svg_heatmap_overrides_manual_color(self):
        styles = {"А": {"color": "#FF0000"}}
        svg = topology_svg(self.system, node_values={"А": 0.5},
                           styles=styles)
        self.assertNotIn('fill="#ff0000"', svg)

    def test_svg_link_labels(self):
        labels = {("А", "Б"): "оптоволокно & <коаксиал>"}
        svg = topology_svg(self.system, link_labels=labels)
        xml.dom.minidom.parseString(svg)
        self.assertIn("оптоволокно", svg)
        self.assertNotIn("<коаксиал>", svg)  # экранирование разметки
        self.assertIn('class="lbl"', svg)

    def test_dot_shape_mapping(self):
        styles = {"А": {"shape": "oval"}, "Б": {"shape": "trapezoid"},
                  "В": {"shape": "diamond"}}
        dot = topology_dot(self.system, styles=styles)
        self.assertIn("shape=ellipse", dot)
        self.assertIn("shape=trapezium", dot)
        self.assertIn("shape=diamond", dot)
        dot_default = topology_dot(self.system)
        self.assertNotIn("shape=ellipse", dot_default)


class TestRoles(unittest.TestCase):
    """Встроенная библиотека ролей и их валидация."""

    def test_builtin_roles_are_valid(self):
        roles = builtin_roles()
        self.assertGreaterEqual(len(roles), 5)
        for role in roles:
            normalized = validate_role(role)
            self.assertEqual(normalized, role)

    def test_builtin_roles_are_copies(self):
        roles = builtin_roles()
        roles[0]["name"] = "испорчено"
        self.assertNotEqual(builtin_roles()[0]["name"], "испорчено")

    def test_validate_role_rejects_bad_params(self):
        with self.assertRaises(ValidationError):
            validate_role({"name": "плохая", "own_failure_rate": -1})
        with self.assertRaises(ValidationError):
            validate_role({"name": "плохая",
                           "impacts": [{"name": "x", "intensity": 1,
                                        "defeat_probability": 2}]})
        with self.assertRaises(ValidationError):
            validate_role({"name": "  "})
        with self.assertRaises(ValidationError):
            validate_role({"name": "плохая", "period": 0})

    def test_user_roles_skips_invalid(self):
        section = {"roles": [{"name": "хорошая", "period": 1.0},
                             {"name": "плохая", "own_failure_rate": -1}]}
        self.assertEqual([r["name"] for r in user_roles(section)], ["хорошая"])
        self.assertEqual(user_roles({}), [])
        self.assertEqual(user_roles({"roles": "не список"}), [])

    def test_default_roles_shape(self):
        self.assertEqual(len(DEFAULT_ROLES), len(builtin_roles()))


if __name__ == "__main__":
    unittest.main()
