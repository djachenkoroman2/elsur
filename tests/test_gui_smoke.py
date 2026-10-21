"""Smoke-тест графической оболочки: пропускается, если Qt/дисплей недоступны."""

import json
import os
import unittest

EXAMPLE6 = "examples/example6_system_line.json"


def _import_gui():
    try:
        from survivability import gui
        return gui
    except ImportError:
        return None


gui = _import_gui()


@unittest.skipIf(gui is None, "PyQt6/PySide6 не установлены")
class TestGuiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "minimal")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(["smoke-test"])

    def test_window_shows_and_closes(self):
        window = gui.MainWindow()
        window.show()
        self.app.processEvents()
        self.assertTrue(window.isVisible())
        window.close()
        self.app.processEvents()

    def test_example6_loads_and_calculates(self):
        with open(EXAMPLE6, encoding="utf-8") as fh:
            payload = json.load(fh)
        window = gui.MainWindow()
        # подмена диалога открытия файла: грузим словарь напрямую
        from survivability.io import job_from_json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            path.write_text(json.dumps(payload, ensure_ascii=False),
                            encoding="utf-8")
            job = job_from_json(path)
        window.project = gui.project_to_dict(job.system, job.protection)
        window._load_project_into_widgets()
        window._layout_new_nodes()
        self.assertEqual(len(window.project["system"]["nodes"]), 3)
        # синхронный расчёт (без потока) через того же воркера
        system, _ = window._collect_models()
        captured = []
        worker = gui.CalcWorker(system, None)
        worker.done_qs.connect(lambda est, imp: captured.append((est, imp)))
        worker.run()  # прямой вызов в тестовом потоке
        self.assertEqual(len(captured), 1)
        estimate, importance = captured[0]
        self.assertAlmostEqual(estimate.q, 0.701266, places=6)
        self.assertEqual(len(importance), 3)

    def test_rename_node_keeps_position_and_name(self):
        # Регрессия: переименование не должно терять позицию на канвасе
        window = gui.MainWindow()
        window._layout_new_nodes()
        old_name = window.project["system"]["nodes"][0]["name"]
        old_pos = window.positions[old_name]
        window.node_list.setCurrentRow(0)
        window.node_name_edit.setText("Сервер-Х")
        window._node_field_changed()
        node = window.project["system"]["nodes"][0]
        self.assertEqual(node["name"], "Сервер-Х")
        self.assertIn("Сервер-Х", window.positions)
        self.assertNotIn(old_name, window.positions)
        self.assertEqual(window.positions["Сервер-Х"], old_pos)
        # Имя в списке тоже обновилось
        self.assertEqual(window.node_list.item(0).text(), "Сервер-Х")

    def test_rename_updates_links(self):
        window = gui.MainWindow()
        window.project["system"]["nodes"].append(
            {"name": "Узел-2", "own_failure_rate": 0.0, "recovery_rate": 0.0,
             "period": 1.0, "impacts": []})
        window.project["system"]["links"] = [["Узел-1", "Узел-2"]]
        window._load_project_into_widgets()
        window._layout_new_nodes()
        window.node_list.setCurrentRow(0)
        window.node_name_edit.setText("ЦОИ")
        window._node_field_changed()
        self.assertEqual(window.project["system"]["links"], [["ЦОИ", "Узел-2"]])

    def test_reload_node_list_preserves_selection(self):
        # Регрессия: currentRow читался после clear() → выбор сбрасывался
        window = gui.MainWindow()
        window.node_list.setCurrentRow(0)
        window._reload_node_list()
        self.assertEqual(window.node_list.currentRow(), 0)
        self.assertTrue(window.node_name_edit.isEnabled())

    def test_canvas_click_selects_node_in_editor(self):
        window = gui.MainWindow()
        window._layout_new_nodes()
        name = window.project["system"]["nodes"][0]["name"]
        window._canvas_node_clicked(name)
        self.assertEqual(window.node_list.currentRow(), 0)
        self.assertEqual(window.node_name_edit.text(), name)

    # ------------------------------------------------------------------
    # роли, оформление, секция gui
    # ------------------------------------------------------------------
    def test_add_node_dialog_returns_role(self):
        from survivability.project_codec import builtin_roles
        roles = builtin_roles()
        dialog = gui.AddNodeDialog("Узел-9", roles,
                                   existing_names={"Сервер-1"})
        index = dialog.role_combo.findText("Сервер")
        self.assertGreaterEqual(index, 0)
        dialog.role_combo.setCurrentIndex(index)
        # имя ещё не тронуто — предлагается имя от роли с учётом занятых
        self.assertEqual(dialog.name_edit.text(), "Сервер-2")
        name, role = dialog.values()
        self.assertEqual(name, "Сервер-2")
        self.assertEqual(role["name"], "Сервер")
        self.assertGreater(len(role["impacts"]), 0)
        # «вручную» — роль не выбрана
        dialog.role_combo.setCurrentIndex(0)
        _, role = dialog.values()
        self.assertIsNone(role)

    def test_node_from_role_prefills_and_copies(self):
        from survivability.project_codec import builtin_roles
        role = next(r for r in builtin_roles() if r["name"] == "Сервер")
        window = gui.MainWindow()
        node = window._node_from_role("Сервер-1", role)
        self.assertEqual(node["own_failure_rate"], role["own_failure_rate"])
        self.assertEqual(node["recovery_rate"], role["recovery_rate"])
        self.assertEqual(node["impacts"], role["impacts"])
        node["impacts"][0]["intensity"] = 999.0  # копия, не ссылка
        self.assertNotEqual(node["impacts"][0]["intensity"],
                            role["impacts"][0]["intensity"])
        manual = window._node_from_role("Узел-X", None)
        self.assertEqual(manual["impacts"], [])
        self.assertEqual(manual["own_failure_rate"], 0.0)

    def test_style_widgets_present_in_nodes_tab(self):
        window = gui.MainWindow()
        self.assertGreaterEqual(window.node_shape_combo.count(), 4)
        self.assertGreaterEqual(window.node_role_combo.count(), 6)  # 5 + «не задана»

    def test_apply_role_via_combo_updates_node(self):
        window = gui.MainWindow()
        window.node_list.setCurrentRow(0)
        index = window.node_role_combo.findText("Маршрутизатор")
        self.assertGreaterEqual(index, 0)
        window.node_role_combo.setCurrentIndex(index)
        node = window.project["system"]["nodes"][0]
        self.assertEqual(node["own_failure_rate"], 0.02)
        self.assertTrue(node["impacts"])
        from survivability.project_codec import gui_section, node_style
        style = node_style(gui_section(window.project), node["name"])
        self.assertEqual(style["role"], "Маршрутизатор")

    def test_color_and_shape_handlers_update_gui_section(self):
        from survivability.project_codec import gui_section, node_style
        window = gui.MainWindow()
        window.node_list.setCurrentRow(0)
        window._apply_node_color("#ff0000")
        name = window.project["system"]["nodes"][0]["name"]
        self.assertEqual(node_style(gui_section(window.project), name)["color"],
                         "#ff0000")
        index = window.node_shape_combo.findText("ромб")
        window.node_shape_combo.setCurrentIndex(index)
        self.assertEqual(node_style(gui_section(window.project), name)["shape"],
                         "diamond")

    def _two_node_window(self):
        """Окно с двумя узлами, связью, стилями и подписью канала."""
        from survivability.project_codec import (gui_section, set_link_label,
                                                 set_node_style)
        window = gui.MainWindow()
        system = window.project["system"]
        system["nodes"].append(
            {"name": "Узел-2", "own_failure_rate": 0.0, "recovery_rate": 0.0,
             "period": 1.0, "impacts": []})
        system["links"] = [["Узел-1", "Узел-2"]]
        section = gui_section(window.project)
        set_node_style(section, "Узел-1", color="#ff8800", shape="oval",
                       role="Сервер")
        set_link_label(section, "Узел-1", "Узел-2", "оптоволокно")
        window.positions = {"Узел-1": (111.0, 222.0), "Узел-2": (333.0, 444.0)}
        return window

    def test_project_file_roundtrip_keeps_gui_metadata(self):
        import tempfile
        from pathlib import Path
        from survivability.project_codec import (gui_section, node_style,
                                                 link_labels_from_section)
        window = self._two_node_window()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            self.assertTrue(window._write_project(path))
            self.assertIn('"gui"', path.read_text(encoding="utf-8"))
            other = gui.MainWindow()
            other.load_project(path)
        self.assertEqual(other.positions, window.positions)
        section = gui_section(other.project)
        self.assertEqual(node_style(section, "Узел-1"),
                         {"color": "#ff8800", "shape": "oval",
                          "role": "Сервер"})
        self.assertEqual(link_labels_from_section(section),
                         {("Узел-1", "Узел-2"): "оптоволокно"})
        # CLI-часть файла не пострадала
        system, _ = other._collect_models()
        self.assertEqual(len(system.nodes), 2)
        self.assertEqual(len(system.links), 1)

    def test_rename_node_keeps_gui_metadata(self):
        from survivability.project_codec import (gui_section, node_style,
                                                 link_labels_from_section)
        window = self._two_node_window()
        window.node_list.setCurrentRow(0)
        window.node_name_edit.setText("ЦОИ")
        window._node_field_changed()
        section = gui_section(window.project)
        self.assertEqual(window.project["system"]["links"], [["ЦОИ", "Узел-2"]])
        self.assertEqual(window.positions["ЦОИ"], (111.0, 222.0))
        self.assertEqual(node_style(section, "ЦОИ")["color"], "#ff8800")
        self.assertEqual(link_labels_from_section(section),
                         {tuple(sorted(("ЦОИ", "Узел-2"))): "оптоволокно"})

    def test_delete_node_drops_gui_metadata(self):
        from survivability.project_codec import gui_section, node_style
        window = self._two_node_window()
        window._load_project_into_widgets()
        window._layout_new_nodes()
        # удаляем второй узел (с инцидентной связью и подписью)
        row = next(i for i, n in enumerate(window.project["system"]["nodes"])
                   if n["name"] == "Узел-2")
        window.node_list.setCurrentRow(row)
        window._delete_node()
        section = gui_section(window.project)
        self.assertNotIn("link_labels", section)
        self.assertEqual(window.project["system"]["links"], [])
        self.assertNotIn("Узел-2", window.positions)
        # Первый узел не пострадал
        self.assertEqual(node_style(section, "Узел-1")["shape"], "oval")

    def test_roles_dialog_accept_normalizes(self):
        from survivability.project_codec import gui_section, user_roles
        window = gui.MainWindow()
        dialog = gui.RolesDialog([], window)
        dialog._roles.append({"name": "Шлюз", "own_failure_rate": 0.01,
                              "recovery_rate": 4.0, "period": 1.0,
                              "impacts": [{"name": "DDoS", "intensity": 1.0,
                                           "defeat_probability": 0.2}]})
        dialog.accept()
        window.project["gui"] = {"roles": dialog.values()}
        roles = user_roles(gui_section(window.project))
        self.assertEqual([r["name"] for r in roles], ["Шлюз"])
        self.assertEqual(roles[0]["impacts"][0]["defeat_probability"], 0.2)


if __name__ == "__main__":
    unittest.main()
