"""Графическая оболочка (PyQt) над приложением survivability — задача 4.

Редактор проекта (узлы, воздействия, топология, параметры системы и
варианты защиты), расчёт Q(S) и выбор способа защиты g* в отдельном
потоке, сохранение проекта в формате CLI, экспорт схемы в SVG/DOT.

Возможности редактора топологии: роли узлов (встроенная библиотека и
пользовательские наборы), координаты узлов, цвета, фигуры, пиктограммы
(растровые изображения, встроенные в файл проекта) и подписи
каналов связи — всё это хранится в служебной секции `gui` файла проекта
(см. `survivability/project_codec.py`), которая не мешает обмену файлами
с CLI: консольная утилита её игнорирует.

Ядро приложения (model/calc/system_calc/protection/io/cli) не импортирует
этот модуль и не требует PyQt; запуск: python -m survivability.gui.
"""

from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path

try:  # PyQt6 — основная зависимость (requirements.txt)
    from PyQt6.QtCore import (QBuffer, QByteArray, QIODevice, QPoint, QPointF,
                              QRectF, Qt, QThread, pyqtSignal)
    from PyQt6.QtGui import (QAction, QBrush, QColor, QImage, QKeySequence,
                             QPainter, QPen, QPixmap, QPolygon)
    from PyQt6.QtWidgets import (QAbstractScrollArea, QApplication, QCheckBox,
                                 QColorDialog, QComboBox, QDialog,
                                 QDialogButtonBox,
                                 QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                                 QLineEdit, QListWidget, QMainWindow, QMenu,
                                 QMessageBox, QPushButton, QSpinBox,
                                 QSplitter, QStatusBar, QTableWidget,
                                 QTableWidgetItem, QTabWidget, QToolButton,
                                 QVBoxLayout, QWidget)
except ImportError:  # PySide6 — API-совместимая замена
    from PySide6.QtCore import (QBuffer, QByteArray, QIODevice, QPoint,
                                QPointF, QRectF, Qt, QThread,
                                Signal as pyqtSignal)
    from PySide6.QtGui import (QAction, QBrush, QColor, QImage, QKeySequence,
                               QPainter, QPen, QPixmap, QPolygon)
    from PySide6.QtWidgets import (QAbstractScrollArea, QApplication,
                                   QCheckBox, QColorDialog, QComboBox,
                                   QDialog, QDialogButtonBox,
                                   QDoubleSpinBox, QFileDialog, QFormLayout,
                                   QGroupBox, QHBoxLayout, QInputDialog,
                                   QLabel, QLineEdit, QListWidget,
                                   QMainWindow, QMenu, QMessageBox,
                                   QPushButton, QSpinBox, QSplitter,
                                   QStatusBar, QTableWidget,
                                   QTableWidgetItem, QTabWidget, QToolButton,
                                   QVBoxLayout, QWidget)

from .io import job_from_json, protection_from_dict, system_from_dict
from .model import ValidationError
from .project_codec import (DEFAULT_NODE_COLOR, NODE_IMAGE_MAX_BYTES,
                            NODE_IMAGE_MAX_SIZE, NODE_SHAPES, builtin_roles,
                            circle_layout, clear_node_image,
                            drop_link_from_gui, drop_node_from_gui,
                            gui_section, link_labels_from_section,
                            link_line_trim, link_style,
                            link_styles_from_section, node_image, node_style,
                            positions_from_section, project_to_dict,
                            prune_gui_section, rename_node_in_gui,
                            sanitize_gui_section, set_link_label,
                            set_link_style, set_node_image, set_node_style,
                            set_positions, topology_dot, topology_svg,
                            user_roles, validate_role)
from .system_calc import node_importance, node_survivabilities, system_qs
from .protection import compare_protection

APP_TITLE = "survivability — живучесть системы"
FILE_FILTER = "Проект survivability (*.json);;Все файлы (*)"

TARGET_TITLES = {
    "node_fraction": "node_fraction — доля работоспособных узлов",
    "connectivity": "connectivity — доля связных пар",
}

SHAPE_TITLES = {
    "rectangle": "прямоугольник",
    "oval": "овал",
    "trapezoid": "трапеция",
    "diamond": "ромб",
}

LINK_STYLE_TITLES = {
    "solid": "сплошная",
    "dash": "пунктирная",
    "dashdot": "штрих-пунктир",
}

ARROW_TITLES = {
    "none": "нет",
    "arrow": "стрелка",
}

#: Qt-стили пера для типов линий связи.
LINK_PEN_STYLES = {
    "solid": Qt.PenStyle.SolidLine,
    "dash": Qt.PenStyle.DashLine,
    "dashdot": Qt.PenStyle.DashDotLine,
}


def default_project() -> dict:
    """Шаблон нового проекта (словарь формата CLI)."""
    return {
        "system": {
            "name": "Новая система",
            "nodes": [
                {
                    "name": "Узел-1",
                    "own_failure_rate": 0.0,
                    "recovery_rate": 0.0,
                    "period": 1.0,
                    "impacts": [],
                }
            ],
            "links": [],
            "w_def": 0.5,
            "target": "node_fraction",
            "monte_carlo_runs": 100000,
        }
    }


def _png_bytes(image: QImage) -> bytes:
    """Кодирование QImage в PNG (в память)."""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def load_node_image_data_uri(path: Path) -> str:
    """Растровый файл → data-URI PNG для записи в секцию `gui`.

    Изображение крупнее NODE_IMAGE_MAX_SIZE px по длинной стороне
    ресемплируется (пропорции сохраняются, сглаживание); если данные
    не укладываются в NODE_IMAGE_MAX_BYTES, размер дополнительно
    уменьшается до влезания. Нечитаемый файл — ValidationError.
    """
    image = QImage(str(path))
    if image.isNull():
        raise ValidationError(f"Не удалось прочитать изображение: {path}")
    if max(image.width(), image.height()) > NODE_IMAGE_MAX_SIZE:
        image = image.scaled(NODE_IMAGE_MAX_SIZE, NODE_IMAGE_MAX_SIZE,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    while True:
        data = _png_bytes(image)
        if len(data) <= NODE_IMAGE_MAX_BYTES:
            break
        smaller = image.scaled(max(1, int(image.width() * 0.75)),
                               max(1, int(image.height() * 0.75)),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        if (smaller.width(), smaller.height()) == (image.width(),
                                                   image.height()):
            raise ValidationError(
                f"Изображение не укладывается в лимит "
                f"{NODE_IMAGE_MAX_BYTES // 1024} КБ даже после уменьшения")
        image = smaller
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Расчёт в фоновом потоке
# ---------------------------------------------------------------------------


class CalcWorker(QThread):
    """Оценка Q(S) или сравнение вариантов защиты без блокировки GUI."""

    done_qs = pyqtSignal(object, object)   # SystemEstimate, [NodeImportance]
    done_protection = pyqtSignal(object)   # ProtectionResult
    failed = pyqtSignal(str)

    def __init__(self, system, protection=None, parent=None):
        super().__init__(parent)
        self.system = system
        self.protection = protection

    def run(self):
        try:
            if self.protection is None:
                estimate = system_qs(self.system)
                importance = node_importance(self.system, estimate)
                self.done_qs.emit(estimate, importance)
            else:
                result = compare_protection(self.system, self.protection,
                                            seed=self.system.seed)
                self.done_protection.emit(result)
        except Exception as exc:  # ошибка показывается диалогом
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# Диалог редактирования воздействий
# ---------------------------------------------------------------------------


class ImpactsDialog(QDialog):
    """Таблица воздействий d_j (имя, λ_j, p_ij) для узла или варианта."""

    COLUMNS = ("Воздействие", "Интенсивность λ", "Вероятность поражения p")

    def __init__(self, impacts: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Деструктивные воздействия")
        self.resize(520, 320)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add_btn = QPushButton("Добавить")
        del_btn = QPushButton("Удалить")
        buttons.addWidget(add_btn)
        buttons.addWidget(del_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        add_btn.clicked.connect(self._add_row)
        del_btn.clicked.connect(self._remove_row)
        for impact in impacts:
            self._append_row(impact.get("name", ""),
                             impact.get("intensity", 0.0),
                             impact.get("defeat_probability", 0.0))

    def _append_row(self, name, intensity, probability):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(name)))
        self.table.setItem(row, 1, QTableWidgetItem(str(intensity)))
        self.table.setItem(row, 2, QTableWidgetItem(str(probability)))

    def _add_row(self):
        self._append_row("", 0.0, 0.0)

    def _remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def values(self) -> list[dict]:
        result = []
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0).text().strip()
            if not name:
                continue
            try:
                intensity = float(self.table.item(row, 1).text().strip())
                probability = float(self.table.item(row, 2).text().strip())
            except (AttributeError, ValueError) as exc:
                raise ValidationError(
                    f"Некорректные числа в строке {row + 1}: {exc}")
            result.append({
                "name": name,
                "intensity": intensity,
                "defeat_probability": probability,
            })
        return result


# ---------------------------------------------------------------------------
# Диалог добавления узла (с выбором роли)
# ---------------------------------------------------------------------------


class AddNodeDialog(QDialog):
    """Имя нового узла и роль из библиотеки (предзаполняет параметры)."""

    def __init__(self, default_name: str, roles: list[dict], parent=None,
                 existing_names=frozenset()):
        super().__init__(parent)
        self.setWindowTitle("Добавить узел")
        self._roles = {role["name"]: role for role in roles}
        self._existing = set(existing_names)
        self._default_name = default_name
        layout = QFormLayout(self)
        self.name_edit = QLineEdit(default_name)
        layout.addRow("Имя:", self.name_edit)
        self.role_combo = QComboBox()
        self.role_combo.addItem("— вручную —", None)
        for role in roles:
            self.role_combo.addItem(role["name"], role["name"])
        layout.addRow("Роль:", self.role_combo)
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        layout.addRow(self.hint_label)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addRow(box)
        self.role_combo.currentIndexChanged.connect(self._update_hint)
        self._update_hint()

    def _role_default_name(self, role_name: str) -> str:
        index = 1
        while f"{role_name}-{index}" in self._existing:
            index += 1
        return f"{role_name}-{index}"

    def _update_hint(self):
        role = self._roles.get(self.role_combo.currentData())
        if role is None:
            self.hint_label.setText(
                "Параметры задаются вручную (по умолчанию нулевые).")
        else:
            self.hint_label.setText(
                f"λ⁽⁰⁾ = {role['own_failure_rate']:g}, "
                f"μ = {role['recovery_rate']:g}, T = {role['period']:g}, "
                f"воздействий: {len(role.get('impacts', []))}. "
                "Поля узла будут предзаполнены (после создания можно менять).")
            # Имя ещё не тронуто — предлагаем имя от роли: «Сервер-1» и т.п.
            if self.name_edit.text().strip() in ("", self._default_name):
                self._default_name = self._role_default_name(role["name"])
                self.name_edit.setText(self._default_name)

    def values(self) -> tuple[str, dict | None]:
        return self.name_edit.text().strip(), \
            self._roles.get(self.role_combo.currentData())


# ---------------------------------------------------------------------------
# Диалог редактора пользовательских ролей
# ---------------------------------------------------------------------------


class RolesDialog(QDialog):
    """Наборы ролей узлов сети: имя и примерные параметры.

    Результат (values) — список нормализованных ролей для записи в
    секцию `gui` проекта; встроенные роли доступны через «Копировать».
    """

    def __init__(self, roles: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Роли узлов")
        self.resize(600, 400)
        self._loading = False
        self._values: list[dict] = []
        self._roles = copy.deepcopy(roles)

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("Пользовательские роли:"))
        self.role_list = QListWidget()
        self.role_list.currentRowChanged.connect(self._role_selected)
        left.addWidget(self.role_list)
        buttons = QHBoxLayout()
        add_btn = QPushButton("Добавить")
        add_btn.clicked.connect(self._add_role)
        del_btn = QPushButton("Удалить")
        del_btn.clicked.connect(self._remove_role)
        buttons.addWidget(add_btn)
        buttons.addWidget(del_btn)
        left.addLayout(buttons)
        copy_row = QHBoxLayout()
        self.builtin_combo = QComboBox()
        for role in builtin_roles():
            self.builtin_combo.addItem(role["name"], role)
        copy_btn = QPushButton("Копировать встроенную")
        copy_btn.clicked.connect(self._copy_builtin)
        copy_row.addWidget(self.builtin_combo)
        copy_row.addWidget(copy_btn)
        left.addLayout(copy_row)
        root.addLayout(left, 1)

        form = QFormLayout()
        form.addRow(QLabel("Параметры выбранной роли:"))
        self.name_edit = QLineEdit()
        self.name_edit.textEdited.connect(self._field_changed)
        form.addRow("Имя роли:", self.name_edit)
        self.own_spin = QDoubleSpinBox()
        self.own_spin.setRange(0.0, 1e6)
        self.own_spin.setDecimals(5)
        self.own_spin.setSingleStep(0.01)
        self.own_spin.valueChanged.connect(self._field_changed)
        form.addRow("λ⁽⁰⁾ (собств. отказы):", self.own_spin)
        self.mu_spin = QDoubleSpinBox()
        self.mu_spin.setRange(0.0, 1e6)
        self.mu_spin.setDecimals(5)
        self.mu_spin.setSingleStep(0.1)
        self.mu_spin.valueChanged.connect(self._field_changed)
        form.addRow("μ (восстановление):", self.mu_spin)
        self.period_spin = QDoubleSpinBox()
        self.period_spin.setRange(0.0001, 1e6)
        self.period_spin.setDecimals(3)
        self.period_spin.setSingleStep(0.1)
        self.period_spin.valueChanged.connect(self._field_changed)
        form.addRow("T (период):", self.period_spin)
        self.impacts_button = QPushButton("Воздействия…")
        self.impacts_button.clicked.connect(self._edit_impacts)
        form.addRow("Воздействия:", self.impacts_button)
        root.addLayout(form, 1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self._reload_list(select=0 if self._roles else -1)

    # --- список ------------------------------------------------------------
    def _reload_list(self, select=None):
        self.role_list.blockSignals(True)
        self.role_list.clear()
        for role in self._roles:
            self.role_list.addItem(role.get("name", ""))
        self.role_list.blockSignals(False)
        if self._roles:
            row = 0 if select is None else max(0, min(select, len(self._roles) - 1))
            self.role_list.setCurrentRow(row)
            self._role_selected(row)
        else:
            self._role_selected(-1)

    def _current(self) -> dict | None:
        row = self.role_list.currentRow()
        return self._roles[row] if 0 <= row < len(self._roles) else None

    def _role_selected(self, row: int):
        self._loading = True
        try:
            role = self._current()
            enabled = role is not None
            for widget in (self.name_edit, self.own_spin, self.mu_spin,
                           self.period_spin, self.impacts_button):
                widget.setEnabled(enabled)
            if role is not None:
                self.name_edit.setText(str(role.get("name", "")))
                self.own_spin.setValue(float(role.get("own_failure_rate", 0.0)))
                self.mu_spin.setValue(float(role.get("recovery_rate", 0.0)))
                self.period_spin.setValue(float(role.get("period", 1.0)))
        finally:
            self._loading = False

    def _field_changed(self, *args):
        if self._loading:
            return
        role = self._current()
        if role is None:
            return
        role["name"] = self.name_edit.text()
        role["own_failure_rate"] = self.own_spin.value()
        role["recovery_rate"] = self.mu_spin.value()
        role["period"] = self.period_spin.value()
        item = self.role_list.currentItem()
        if item is not None:
            item.setText(role["name"])

    def _add_role(self):
        names = {str(role.get("name", "")) for role in self._roles}
        index = len(names) + 1
        while f"Роль-{index}" in names:
            index += 1
        self._roles.append({"name": f"Роль-{index}", "own_failure_rate": 0.0,
                            "recovery_rate": 0.0, "period": 1.0,
                            "impacts": []})
        self._reload_list(select=len(self._roles) - 1)

    def _remove_role(self):
        row = self.role_list.currentRow()
        if 0 <= row < len(self._roles):
            del self._roles[row]
            self._reload_list(select=min(row, len(self._roles) - 1))

    def _copy_builtin(self):
        role = self.builtin_combo.currentData()
        if not role:
            return
        names = {str(r.get("name", "")) for r in self._roles}
        name = f"Копия — {role['name']}"
        index = 2
        while name in names:
            name = f"Копия {index} — {role['name']}"
            index += 1
        copied = copy.deepcopy(role)
        copied["name"] = name
        self._roles.append(copied)
        self._reload_list(select=len(self._roles) - 1)

    def _edit_impacts(self):
        role = self._current()
        if role is None:
            return
        dialog = ImpactsDialog(role.get("impacts", []), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                role["impacts"] = dialog.values()
            except ValidationError as exc:
                QMessageBox.warning(self, "Роли узлов", str(exc))

    def accept(self):
        try:
            self._values = [validate_role(role) for role in self._roles]
        except ValidationError as exc:
            QMessageBox.warning(self, "Роли узлов",
                                f"Роли не сохранены:\n{exc}")
            return
        super().accept()

    def values(self) -> list[dict]:
        return self._values


# ---------------------------------------------------------------------------
# Канвас топологии
# ---------------------------------------------------------------------------


class TopologyCanvas(QAbstractScrollArea):
    """Схема сети: перетаскивание узлов, добавление/удаление связей,
    оформление узлов (цвет, фигура, пиктограмма) и связей (цвет, тип линии,
    толщина, стрелки, подписи концов), подписи каналов по центру линии.

    Масштаб и прокрутка: `positions` хранят координаты схемы, экранная точка
    viewport'а s = p · zoom − offset, где offset — значения полос прокрутки
    (в экранных пикселях). Масштаб и прокрутка — состояние вида, в проект
    не сохраняются."""

    NODE_W, NODE_H = 130, 48
    ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 0.25, 4.0, 1.25
    SCENE_MARGIN = 40        # поле вокруг узлов в области прокрутки
    LINK_HIT_PX = 7.0        # порог попадания по связи — в экранных пикселях
    AUTOSCROLL_EDGE, AUTOSCROLL_STEP = 24, 16
    zoom_changed = pyqtSignal(float)
    structure_changed = pyqtSignal()
    style_changed = pyqtSignal()
    node_moved = pyqtSignal()
    node_clicked = pyqtSignal(str)
    node_image_requested = pyqtSignal(str)
    link_clicked = pyqtSignal(str, str)
    link_style_requested = pyqtSignal(str, str)
    link_double_clicked = pyqtSignal(str, str)

    def __init__(self, get_project, get_positions, get_gui, parent=None):
        super().__init__(parent)
        self._get_project = get_project
        self._get_positions = get_positions
        self._get_gui = get_gui
        self.selected_node: str | None = None
        self.selected_link: tuple[str, str] | None = None
        self.pending_node: str | None = None     # первый узел новой связи
        self.link_mode = False
        self.node_values: dict[str, float] | None = None
        self._image_cache: dict[str, QImage | None] = {}
        self.zoom = 1.0
        self._pan_last: QPointF | None = None  # точка начала панорамирования
        self._space_down = False
        self.setMinimumSize(420, 300)
        self.setMouseTracking(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.setSingleStep(20)

    # --- пиктограммы узлов -------------------------------------------------
    def _node_qimage(self, name: str) -> QImage | None:
        """QImage узла из data-URI секции gui (с кэшем по имени)."""
        if name in self._image_cache:
            return self._image_cache[name]
        image = None
        data_uri = node_image(self._get_gui(), name)
        if data_uri:
            payload = data_uri.split(",", 1)[1].encode("ascii")
            loaded = QImage.fromData(QByteArray(base64.b64decode(payload)))
            if not loaded.isNull():
                image = loaded
        self._image_cache[name] = image
        return image

    def invalidate_node_image(self, name: str):
        """Сброс кэша пиктограммы узла (после set/clear/rename)."""
        self._image_cache.pop(name, None)

    def clear_image_cache(self):
        """Сброс кэша пиктограмм (после загрузки проекта)."""
        self._image_cache.clear()

    def _image_target(self, image: QImage, shape: str, cx: float,
                      cy: float) -> tuple[QImage, int, int]:
        """Масштабированная с сохранением пропорций копия и координаты
        центрированного размещения в ограничивающем прямоугольнике фигуры."""
        box_w, box_h = self.NODE_W, self.NODE_H
        if shape == "diamond":  # как в _draw_shape: grow = 6
            box_w, box_h = self.NODE_W + 12, self.NODE_H + 12
        scaled = image.scaled(box_w, box_h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        return scaled, int(cx - scaled.width() / 2), \
            int(cy - scaled.height() / 2)

    # --- доступ к данным -------------------------------------------------
    def _links(self) -> list[list[str]]:
        return self._get_project()["system"].get("links", [])

    def _node_names(self) -> list[str]:
        return [n["name"] for n in self._get_project()["system"]["nodes"]]

    def set_link_mode(self, enabled: bool):
        self.link_mode = enabled
        self.pending_node = None
        self.viewport().setCursor(Qt.CursorShape.CrossCursor
                                  if enabled else Qt.CursorShape.ArrowCursor)
        self.repaint()

    def set_node_values(self, values: dict[str, float] | None):
        self.node_values = values
        self.repaint()

    def repaint(self):
        """Перерисовка схемы с пересчётом области прокрутки (её границы
        зависят от положения узлов). Вызывается после любых изменений."""
        self._sync_scrollbars()
        self.viewport().update()

    # --- масштаб и прокрутка -----------------------------------------------
    def _offset(self) -> QPointF:
        return QPointF(self.horizontalScrollBar().value(),
                       self.verticalScrollBar().value())

    def to_scene(self, point: QPointF) -> QPointF:
        """Экранная точка viewport'а → координаты схемы."""
        return (QPointF(point) + self._offset()) / self.zoom

    def to_screen(self, point: QPointF) -> QPointF:
        """Координаты схемы → экранная точка viewport'а."""
        return QPointF(point) * self.zoom - self._offset()

    def content_rect(self) -> QRectF:
        """Ограничивающий прямоугольник узлов (в координатах схемы) с полем;
        пустой QRectF, если узлов нет."""
        positions = self._get_positions()
        points = [positions[n] for n in self._node_names() if n in positions]
        if not points:
            return QRectF()
        pad_x = self.NODE_W / 2 + self.SCENE_MARGIN
        pad_y = self.NODE_H / 2 + self.SCENE_MARGIN
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return QRectF(min(xs) - pad_x, min(ys) - pad_y,
                      max(xs) - min(xs) + 2 * pad_x,
                      max(ys) - min(ys) + 2 * pad_y)

    def _sync_scrollbars(self, offset: QPointF | None = None):
        """Диапазоны полос прокрутки = узлы ∪ видимая область (поэтому
        видимая область не «прыгает» при пересчёте), затем — смещение."""
        if offset is None:
            offset = self._offset()
        view_w, view_h = self.viewport().width(), self.viewport().height()
        visible = QRectF(offset.x() / self.zoom, offset.y() / self.zoom,
                         view_w / self.zoom, view_h / self.zoom)
        content = self.content_rect()
        area = visible.united(content) if not content.isNull() else visible
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        hbar.setRange(int(area.left() * self.zoom),
                      max(int(area.left() * self.zoom),
                          round(area.right() * self.zoom - view_w)))
        vbar.setRange(int(area.top() * self.zoom),
                      max(int(area.top() * self.zoom),
                          round(area.bottom() * self.zoom - view_h)))
        hbar.setPageStep(view_w)
        vbar.setPageStep(view_h)
        hbar.setValue(round(offset.x()))
        vbar.setValue(round(offset.y()))

    def set_zoom(self, zoom: float, anchor: QPointF | None = None):
        """Масштаб с ограничением диапазоном; точка схемы под экранной точкой
        `anchor` (по умолчанию — центр viewport'а) остаётся на месте."""
        zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, zoom))
        if anchor is None:
            anchor = QPointF(self.viewport().rect().center())
        scene = self.to_scene(anchor)
        changed = abs(zoom - self.zoom) > 1e-9
        self.zoom = zoom
        self._sync_scrollbars(scene * zoom - anchor)
        self.viewport().update()
        if changed:
            self.zoom_changed.emit(self.zoom)

    def zoom_in(self):
        self.set_zoom(self.zoom * self.ZOOM_STEP)

    def zoom_out(self):
        self.set_zoom(self.zoom / self.ZOOM_STEP)

    def reset_zoom(self):
        self.set_zoom(1.0)

    def fit_to_view(self, max_zoom: float | None = None):
        """Масштаб и прокрутка, при которых все узлы видны; `max_zoom`
        ограничивает увеличение (при загрузке проекта — не больше 100 %)."""
        content = self.content_rect()
        if content.isNull():
            self.set_zoom(1.0)
            return
        view_w, view_h = self.viewport().width(), self.viewport().height()
        zoom = min(view_w / content.width(), view_h / content.height())
        zoom = min(zoom, max_zoom if max_zoom is not None else self.ZOOM_MAX)
        zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, zoom))
        changed = abs(zoom - self.zoom) > 1e-9
        self.zoom = zoom
        center = content.center() * zoom
        self._sync_scrollbars(QPointF(center.x() - view_w / 2,
                                      center.y() - view_h / 2))
        self.viewport().update()
        if changed:
            self.zoom_changed.emit(self.zoom)

    def scrollContentsBy(self, dx, dy):
        # Смещение учитывается трансформацией в paintEvent — просто перерисовка.
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_scrollbars()

    def wheelEvent(self, event):
        """Ctrl + колесо — масштаб относительно курсора, Shift + колесо —
        горизонтальная прокрутка, колесо — вертикальная."""
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            factor = self.ZOOM_STEP ** (delta / 120)
            self.set_zoom(self.zoom * factor, event.position())
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
        else:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - delta)
        event.accept()

    def _autoscroll(self, pos: QPointF):
        """Прокрутка вида, когда перетаскиваемый узел у края viewport'а."""
        edge, step = self.AUTOSCROLL_EDGE, self.AUTOSCROLL_STEP
        rect = self.viewport().rect()
        dx = (-step if pos.x() < edge else
              step if pos.x() > rect.width() - edge else 0)
        dy = (-step if pos.y() < edge else
              step if pos.y() > rect.height() - edge else 0)
        if dx or dy:
            self._sync_scrollbars(self._offset() + QPointF(dx, dy))

    # --- геометрия (координаты схемы) -----------------------------------------
    def _node_at(self, x, y) -> str | None:
        positions = self._get_positions()
        for name in reversed(self._node_names()):
            nx, ny = positions.get(name, (-9999, -9999))
            if abs(x - nx) <= self.NODE_W / 2 and abs(y - ny) <= self.NODE_H / 2:
                return name
        return None

    def _link_at(self, x, y) -> tuple[str, str] | None:
        positions = self._get_positions()
        best, best_dist = None, self.LINK_HIT_PX / self.zoom
        for a, b in self._links():
            if a not in positions or b not in positions:
                continue
            x1, y1 = positions[a]
            x2, y2 = positions[b]
            dx, dy = x2 - x1, y2 - y1
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                continue
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length_sq))
            dist = ((x1 + t * dx - x) ** 2 + (y1 + t * dy - y) ** 2) ** 0.5
            if dist < best_dist:
                best, best_dist = (a, b), dist
        return best

    # --- события мыши/клавиатуры -------------------------------------------
    def _is_pan_press(self, event) -> bool:
        return (event.button() == Qt.MouseButton.MiddleButton
                or (event.button() == Qt.MouseButton.LeftButton
                    and self._space_down))

    def mousePressEvent(self, event):
        if self._is_pan_press(event):
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        pos = self.to_scene(event.position())
        x, y = pos.x(), pos.y()
        if self.link_mode:
            node = self._node_at(x, y)
            if node is None:
                self.set_link_mode(False)
                return
            if self.pending_node is None:
                self.pending_node = node
                self.repaint()
                return
            a, b = self.pending_node, node
            self.set_link_mode(False)
            if a != b:
                links = self._links()
                key = tuple(sorted((a, b)))
                if not any(tuple(sorted(link)) == key for link in links):
                    links.append([a, b])
                    self.structure_changed.emit()
                else:
                    self._status("Такая связь уже существует")
            self.repaint()
            return
        node = self._node_at(x, y)
        if node is not None:
            self.selected_node = node
            self.selected_link = None
            self.node_clicked.emit(node)
        else:
            link = self._link_at(x, y)
            self.selected_node = None
            self.selected_link = link
            if link is not None:
                self.link_clicked.emit(*link)
        self.repaint()

    def mouseMoveEvent(self, event):
        if self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self._sync_scrollbars(self._offset() - delta)
            return
        if self.selected_node is None or self.link_mode:
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        positions = self._get_positions()
        if self.selected_node in positions:
            self._autoscroll(event.position())
            pos = self.to_scene(event.position())
            positions[self.selected_node] = (pos.x(), pos.y())
            self.node_moved.emit()
            self.repaint()

    def mouseReleaseEvent(self, event):
        if self._pan_last is not None and not (
                event.buttons() & (Qt.MouseButton.MiddleButton
                                   | Qt.MouseButton.LeftButton)):
            self._pan_last = None
            self._restore_cursor()

    def _restore_cursor(self):
        if self._space_down:
            shape = Qt.CursorShape.OpenHandCursor
        elif self.link_mode:
            shape = Qt.CursorShape.CrossCursor
        else:
            shape = Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(shape)

    def mouseDoubleClickEvent(self, event):
        if self.link_mode or self._space_down:
            return
        pos = self.to_scene(event.position())
        x, y = pos.x(), pos.y()
        if self._node_at(x, y) is not None:
            return
        link = self._link_at(x, y)
        if link is not None:
            self.selected_link = link
            self.selected_node = None
            self.link_double_clicked.emit(*link)

    def contextMenuEvent(self, event):
        if self.link_mode:
            return
        pos = self.to_scene(QPointF(event.pos()))
        x, y = pos.x(), pos.y()
        menu = QMenu(self)
        node = self._node_at(x, y)
        if node is not None:
            self.selected_node = node
            self.selected_link = None
            self.node_clicked.emit(node)
            self._add_menu_action(menu, "Цвет…",
                                  lambda: self._pick_node_color(node))
            has_image = node_image(self._get_gui(), node) is not None
            self._add_menu_action(
                menu, f"Изображение… ({'есть' if has_image else 'нет'})",
                lambda n=node: self.node_image_requested.emit(n))
            if has_image:
                self._add_menu_action(menu, "Убрать изображение",
                                      lambda n=node:
                                      self._clear_node_image(n))
            shape_menu = menu.addMenu("Фигура")
            current = node_style(self._get_gui(), node)["shape"]
            for key in NODE_SHAPES:
                act = self._add_menu_action(
                    shape_menu, SHAPE_TITLES[key],
                    lambda checked=False, k=key, n=node:
                    self._set_node_shape(n, k))
                act.setCheckable(True)
                act.setChecked(key == current)
            menu.addSeparator()
            self._add_menu_action(menu, "Удалить узел", self.delete_selected)
        else:
            link = self._link_at(x, y)
            if link is None:
                return
            self.selected_link = link
            self.selected_node = None
            self._add_menu_action(menu, "Оформление…",
                                  lambda: self.link_style_requested.emit(*link))
            self._add_menu_action(menu, "Подпись канала…",
                                  lambda: self.link_double_clicked.emit(*link))
            menu.addSeparator()
            self._add_menu_action(menu, "Удалить связь", self.delete_selected)
        self.repaint()
        menu.exec(event.globalPos())

    @staticmethod
    def _add_menu_action(menu, title, slot):
        act = QAction(title, menu)
        act.triggered.connect(slot)
        menu.addAction(act)
        return act

    def _pick_node_color(self, name: str):
        style = node_style(self._get_gui(), name)
        initial = QColor(style["color"] or DEFAULT_NODE_COLOR)
        color = QColorDialog.getColor(initial, self, f"Цвет узла «{name}»")
        if color.isValid():
            set_node_style(gui_section(self._get_project()), name,
                           color=color.name())
            self.style_changed.emit()
            self.repaint()

    def _set_node_shape(self, name: str, shape: str):
        set_node_style(gui_section(self._get_project()), name, shape=shape)
        self.style_changed.emit()
        self.repaint()

    def _clear_node_image(self, name: str):
        clear_node_image(gui_section(self._get_project()), name)
        self.invalidate_node_image(name)
        self.style_changed.emit()
        self.repaint()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.delete_selected()
        elif event.key() == Qt.Key.Key_Space:
            # пробел + левая кнопка — панорамирование
            if not event.isAutoRepeat():
                self._space_down = True
                self._restore_cursor()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            if self._pan_last is None:
                self._restore_cursor()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._space_down = False
        if self._pan_last is None:
            self._restore_cursor()
        super().focusOutEvent(event)

    def delete_selected(self):
        section = gui_section(self._get_project())
        if self.selected_node is not None:
            name = self.selected_node
            project = self._get_project()
            system = project["system"]
            system["nodes"] = [n for n in system["nodes"]
                               if n["name"] != name]
            system["links"] = [link for link in system.get("links", [])
                               if name not in link]
            protection = project.get("protection")
            if protection:
                for variant in protection.get("variants", []):
                    variant.get("nodes", {}).pop(name, None)
                    variant["links_added"] = [
                        link for link in variant.get("links_added", [])
                        if name not in link]
            self._get_positions().pop(name, None)
            drop_node_from_gui(section, name)
            self.invalidate_node_image(name)
            self.selected_node = None
            self.structure_changed.emit()
        elif self.selected_link is not None:
            a, b = self.selected_link
            links = self._links()
            links[:] = [link for link in links
                        if tuple(sorted(link)) != tuple(sorted((a, b)))]
            drop_link_from_gui(section, a, b)
            self.selected_link = None
            self.structure_changed.emit()
        self.repaint()

    def _status(self, message: str):
        window = self.window()
        if isinstance(window, MainWindow):
            window.statusBar().showMessage(message, 5000)

    # --- отрисовка ----------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.viewport().rect(), QColor("#ffffff"))
        # Далее всё рисуется в координатах схемы: s = p · zoom − offset.
        offset = self._offset()
        painter.translate(-offset.x(), -offset.y())
        painter.scale(self.zoom, self.zoom)
        positions = self._get_positions()
        gui = self._get_gui()
        labels = link_labels_from_section(gui)
        for a, b in self._links():
            if a not in positions or b not in positions:
                continue
            x1, y1 = positions[a]
            x2, y2 = positions[b]
            style = link_style(gui, a, b)
            sx, sy, ex, ey, ux, uy = link_line_trim(x1, y1, x2, y2)
            pen = QPen(QColor(style["color"]))
            pen.setWidth(style["width"])
            pen.setStyle(LINK_PEN_STYLES.get(style["style"],
                                             Qt.PenStyle.SolidLine))
            painter.setPen(pen)
            painter.drawLine(int(sx), int(sy), int(ex), int(ey))
            if style["arrow_start"] == "arrow" or style["arrow_end"] == "arrow":
                # Заполненные треугольники, размер — от толщины линии.
                size = 8 + 3 * style["width"]
                painter.setBrush(QBrush(QColor(style["color"])))
                painter.setPen(QPen(Qt.PenStyle.NoPen))
                if style["arrow_start"] == "arrow":
                    painter.drawPolygon(self._arrow(sx, sy, -ux, -uy, size))
                if style["arrow_end"] == "arrow":
                    painter.drawPolygon(self._arrow(ex, ey, ux, uy, size))
                painter.setPen(pen)
            label = labels.get(tuple(sorted((a, b))))
            if label:
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                metrics = painter.fontMetrics()
                width = metrics.horizontalAdvance(label) + 8
                painter.setPen(QColor("#333333"))
                painter.setBackground(QColor("#ffffff"))
                painter.setBackgroundMode(Qt.BGMode.OpaqueMode)
                painter.drawText(int(mx - width / 2), int(my - 10),
                                 width, 18,
                                 Qt.AlignmentFlag.AlignHCenter
                                 | Qt.AlignmentFlag.AlignVCenter, label)
                painter.setBackgroundMode(Qt.BGMode.TransparentMode)
                painter.setPen(pen)
            if style["label_start"] or style["label_end"]:
                # Подписи концов выносятся за границу узла по оси линии
                # («наружу»), чтобы не перекрывать узел и линию.
                d1 = ((sx - x1) ** 2 + (sy - y1) ** 2) ** 0.5 + 12
                d2 = ((ex - x2) ** 2 + (ey - y2) ** 2) ** 0.5 + 12
                for text, lx, ly in ((style["label_start"],
                                      x1 - ux * d1, y1 - uy * d1),
                                     (style["label_end"],
                                      x2 + ux * d2, y2 + uy * d2)):
                    if not text:
                        continue
                    metrics = painter.fontMetrics()
                    width = metrics.horizontalAdvance(text) + 8
                    painter.setPen(QColor("#333333"))
                    painter.setBackground(QColor("#ffffff"))
                    painter.setBackgroundMode(Qt.BGMode.OpaqueMode)
                    painter.drawText(int(lx - width / 2), int(ly - 9),
                                     width, 18,
                                     Qt.AlignmentFlag.AlignHCenter
                                     | Qt.AlignmentFlag.AlignVCenter, text)
                    painter.setBackgroundMode(Qt.BGMode.TransparentMode)
                    painter.setPen(pen)
        if self.selected_link is not None:
            a, b = self.selected_link
            if a in positions and b in positions:
                x1, y1 = positions[a]
                x2, y2 = positions[b]
                painter.setPen(QPen(QColor("#cc0000"), 3))
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))
        for name in self._node_names():
            nx, ny = positions.get(name, (30, 30))
            rect_x, rect_y = int(nx - self.NODE_W / 2), int(ny - self.NODE_H / 2)
            value = self.node_values.get(name) if self.node_values else None
            style = node_style(gui, name)
            if value is None:
                fill, stroke = (QColor(style["color"] or DEFAULT_NODE_COLOR),
                                QColor("#1155cc"))
            else:
                fill = self._heat(value)
                stroke = QColor("#333333")
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(stroke, 2 if name == self.selected_node else 1))
            self._draw_shape(painter, style["shape"], int(nx), int(ny))
            image = self._node_qimage(name)
            if image is not None:
                # Пиктограмма поверх заливки, внутри контура фигуры; фигура
                # и цвет остаются видимыми как рамка/фон.
                scaled, ix, iy = self._image_target(image, style["shape"],
                                                    nx, ny)
                painter.drawImage(ix, iy, scaled)
            painter.setPen(QColor("#111111"))
            text = name if len(name) <= 22 else name[:21] + "…"
            painter.drawText(rect_x, rect_y + 4, self.NODE_W, 22,
                             Qt.AlignmentFlag.AlignHCenter, text)
            if value is not None:
                painter.setPen(QColor("#333333"))
                painter.drawText(rect_x, rect_y + 24, self.NODE_W, 20,
                                 Qt.AlignmentFlag.AlignHCenter,
                                 f"{value:.3f}")
        if self.pending_node is not None and self.pending_node in positions:
            nx, ny = positions[self.pending_node]
            painter.setBrush(QBrush(QColor("#f90")))
            painter.setPen(QPen(QColor("#b45f06"), 2))
            painter.drawEllipse(int(nx) - 6, int(ny) - self.NODE_H // 2 - 12,
                                12, 12)
        painter.end()

    @staticmethod
    def _arrow(tip_x, tip_y, ux, uy, size):
        """Заполненный треугольник-стрелка: остриё у (tip_x, tip_y), направление
        (ux, uy) — единичный вектор «наружу» от линии; размер — длина основания."""
        gap = 4  # зазор острия от конца линии
        tx, ty = tip_x + ux * gap, tip_y + uy * gap
        bx, by = tip_x - ux * size, tip_y - uy * size
        px, py = -uy, ux
        wing = size * 0.42
        return QPolygon([QPoint(int(tx), int(ty)),
                         QPoint(int(bx + px * wing), int(by + py * wing)),
                         QPoint(int(bx - px * wing), int(by - py * wing))])

    def _draw_shape(self, painter, shape: str, cx: int, cy: int):
        """Фигура узла с центром в (cx, cy); размеры — NODE_W × NODE_H."""
        w, h = self.NODE_W, self.NODE_H
        x, y = cx - w // 2, cy - h // 2
        if shape == "oval":
            painter.drawEllipse(x, y, w, h)
        elif shape == "trapezoid":
            inset = 20
            painter.drawPolygon(QPolygon([
                QPoint(x + inset, y), QPoint(x + w - inset, y),
                QPoint(x + w, y + h), QPoint(x, y + h)]))
        elif shape == "diamond":
            grow = 6
            painter.drawPolygon(QPolygon([
                QPoint(cx, y - grow), QPoint(x + w + grow, cy),
                QPoint(cx, y + h + grow), QPoint(x - grow, cy)]))
        else:  # rectangle
            painter.drawRoundedRect(x, y, w, h, 9, 9)

    @staticmethod
    def _heat(value: float) -> QColor:
        value = max(0.0, min(1.0, value))
        if value < 0.5:
            t = value / 0.5
            return QColor(int(110 + 130 * t), int(200 - 10 * t),
                          int(100 - 30 * t))
        t = (value - 0.5) / 0.5
        return QColor(int(240 - 10 * t), int(190 - 110 * t),
                      int(70 - 20 * t))


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.project = default_project()
        self.positions: dict[str, tuple[float, float]] = {}
        self.current_path: Path | None = None
        self.dirty = False
        self.worker: CalcWorker | None = None
        self.last_importance: dict[str, float] | None = None
        self._loading = False

        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 800)
        self.setStatusBar(QStatusBar())

        self._build_actions()
        self._build_layout()
        self._load_project_into_widgets()
        self._layout_new_nodes()

    # ------------------------------------------------------------------
    # действия и меню
    # ------------------------------------------------------------------
    def _build_actions(self):
        def action(title, slot, shortcut=None):
            act = QAction(title, self)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(shortcut)
            return act

        self.new_action = action("Новый проект", self.new_project, "Ctrl+N")
        self.open_action = action("Открыть…", self.open_project, "Ctrl+O")
        self.save_action = action("Сохранить", self.save_project, "Ctrl+S")
        self.save_as_action = action("Сохранить как…", self.save_project_as)
        self.export_svg_action = action("Экспорт схемы SVG…",
                                        self.export_svg)
        self.export_dot_action = action("Экспорт схемы DOT…",
                                        self.export_dot)
        self.export_results_action = action("Экспорт результатов…",
                                            self.export_results)
        self.calc_action = action("Рассчитать Q(S)", self.calculate_qs, "F5")
        self.compare_action = action("Сравнить варианты защиты",
                                     self.calculate_protection, "F6")
        self.roles_action = action("Роли узлов…", self._edit_roles)
        self.quit_action = action("Выход", self.close, "Ctrl+Q")

        menu = self.menuBar().addMenu("Проект")
        for act in (self.new_action, self.open_action, self.save_action,
                    self.save_as_action):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self.export_results_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        menu_export = self.menuBar().addMenu("Экспорт")
        menu_export.addAction(self.export_svg_action)
        menu_export.addAction(self.export_dot_action)
        menu_calc = self.menuBar().addMenu("Расчёт")
        menu_calc.addAction(self.calc_action)
        menu_calc.addAction(self.compare_action)
        menu_settings = self.menuBar().addMenu("Настройки")
        menu_settings.addAction(self.roles_action)

    # ------------------------------------------------------------------
    # компоновка
    # ------------------------------------------------------------------
    def _build_layout(self):
        central = QWidget()
        root = QHBoxLayout(central)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_nodes_tab(), "Узлы")
        self.links_tab = self._build_links_tab()
        self.tabs.addTab(self.links_tab, "Связи")
        self.tabs.addTab(self._build_system_tab(), "Система")
        self.tabs.addTab(self._build_protection_tab(), "Защита")
        self.tabs.setFixedWidth(430)
        root.addWidget(self.tabs)

        right = QSplitter(Qt.Orientation.Vertical)

        canvas_box = QGroupBox("Топология сети")
        canvas_layout = QVBoxLayout(canvas_box)
        canvas_bar = QHBoxLayout()
        canvas_bar.addWidget(QLabel("Раскраска:"))
        self.color_combo = QComboBox()
        self.color_combo.addItem("по Q_i", "q")
        self.color_combo.addItem("по критичности I_i", "I")
        self.color_combo.currentIndexChanged.connect(self._recolor_canvas)
        canvas_bar.addWidget(self.color_combo)
        self.link_button = QPushButton("Добавить связь")
        self.link_button.setCheckable(True)
        self.link_button.toggled.connect(self._link_mode_toggled)
        canvas_bar.addWidget(self.link_button)
        delete_btn = QPushButton("Удалить выбранное")
        delete_btn.clicked.connect(self._delete_selected)
        canvas_bar.addWidget(delete_btn)
        canvas_layout.addLayout(canvas_bar)
        self.canvas = TopologyCanvas(lambda: self.project,
                                     lambda: self.positions,
                                     self._gui_view)
        self._build_zoom_controls(canvas_bar)
        canvas_bar.addStretch(1)
        self.canvas.structure_changed.connect(self._structure_changed)
        self.canvas.style_changed.connect(self._on_style_changed)
        self.canvas.node_moved.connect(self._mark_dirty)
        self.canvas.node_clicked.connect(self._canvas_node_clicked)
        self.canvas.node_image_requested.connect(self._choose_node_image)
        self.canvas.link_clicked.connect(self._canvas_link_clicked)
        self.canvas.link_style_requested.connect(
            self._canvas_link_style_requested)
        self.canvas.link_double_clicked.connect(self._edit_link_label)
        canvas_layout.addWidget(self.canvas)
        right.addWidget(canvas_box)

        results_box = QGroupBox("Результаты")
        results_layout = QVBoxLayout(results_box)
        self.summary_label = QLabel("Расчёт ещё не выполнялся.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        results_layout.addWidget(self.summary_label)
        self.results_tabs = QTabWidget()
        self.importance_table = QTableWidget(0, 4)
        self.importance_table.setHorizontalHeaderLabels(
            ["Узел", "I (Бирнбаум)", "Q(S|x=1)", "Q(S|x=0)"])
        self.importance_table.horizontalHeader().setStretchLastSection(True)
        self.results_tabs.addTab(self.importance_table, "Важность узлов")
        self.variants_table = QTableWidget(0, 6)
        self.variants_table.setHorizontalHeaderLabels(
            ["Вариант", "C(g)", "Допустим", "Q_g(S)", "ΔQ к базе", ""])
        self.variants_table.horizontalHeader().setStretchLastSection(True)
        self.results_tabs.addTab(self.variants_table, "Варианты защиты")
        results_layout.addWidget(self.results_tabs)
        right.addWidget(results_box)

        root.addWidget(right)
        self.setCentralWidget(central)

    def _build_zoom_controls(self, bar: QHBoxLayout):
        """Кнопки масштаба канваса и подпись с текущим масштабом; горячие
        клавиши — действия окна (работают при фокусе в любом виджете)."""
        canvas = self.canvas
        specs = (
            ("zoom_out", "−", "Уменьшить масштаб (Ctrl+−)",
             canvas.zoom_out, ["Ctrl+-"]),
            ("zoom_in", "+", "Увеличить масштаб (Ctrl+=)",
             canvas.zoom_in, ["Ctrl+=", "Ctrl++"]),
            ("zoom_reset", "100 %", "Масштаб 100 % (Ctrl+0)",
             canvas.reset_zoom, ["Ctrl+0"]),
            ("zoom_fit", "Вписать", "Показать все узлы (Ctrl+9)",
             canvas.fit_to_view, ["Ctrl+9"]),
        )
        self.zoom_label = QLabel()
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for key, text, tip, slot, shortcuts in specs:
            act = QAction(text, self)
            act.setToolTip(tip)
            act.setShortcuts([QKeySequence(sc) for sc in shortcuts])
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            act.triggered.connect(lambda checked=False, f=slot: f())
            self.addAction(act)
            button = QToolButton()
            button.setDefaultAction(act)
            bar.addWidget(button)
            setattr(self, f"{key}_action", act)
            if key == "zoom_out":  # подпись масштаба — между «−» и «+»
                bar.addWidget(self.zoom_label)
        canvas.zoom_changed.connect(self._update_zoom_controls)
        self._update_zoom_controls(canvas.zoom)

    def _update_zoom_controls(self, zoom: float):
        self.zoom_label.setText(f"{round(zoom * 100)} %")
        canvas = self.canvas
        self.zoom_out_action.setEnabled(zoom > canvas.ZOOM_MIN + 1e-9)
        self.zoom_in_action.setEnabled(zoom < canvas.ZOOM_MAX - 1e-9)

    def _build_nodes_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.node_list = QListWidget()
        self.node_list.currentRowChanged.connect(self._node_selected)
        layout.addWidget(self.node_list)
        node_buttons = QHBoxLayout()
        add_btn = QPushButton("Добавить узел")
        add_btn.clicked.connect(self._add_node)
        del_btn = QPushButton("Удалить узел")
        del_btn.clicked.connect(self._delete_node)
        node_buttons.addWidget(add_btn)
        node_buttons.addWidget(del_btn)
        layout.addLayout(node_buttons)

        form_group = QGroupBox("Параметры узла")
        form = QFormLayout(form_group)
        self.node_name_edit = QLineEdit()
        self.node_name_edit.textEdited.connect(self._node_field_changed)
        form.addRow("Имя:", self.node_name_edit)
        self.node_own_spin = self._rate_spin()
        self.node_own_spin.valueChanged.connect(self._node_field_changed)
        form.addRow("λ⁽⁰⁾ (собств. отказы):", self.node_own_spin)
        self.node_mu_spin = self._rate_spin()
        self.node_mu_spin.valueChanged.connect(self._node_field_changed)
        form.addRow("μ (восстановление):", self.node_mu_spin)
        self.node_period_spin = self._rate_spin(decimals=3)
        self.node_period_spin.valueChanged.connect(self._node_field_changed)
        form.addRow("T (период):", self.node_period_spin)
        self.node_role_combo = QComboBox()
        self.node_role_combo.currentIndexChanged.connect(self._node_role_changed)
        form.addRow("Роль:", self.node_role_combo)
        self.node_shape_combo = QComboBox()
        for key in NODE_SHAPES:
            self.node_shape_combo.addItem(SHAPE_TITLES[key], key)
        self.node_shape_combo.currentIndexChanged.connect(
            self._node_shape_changed)
        form.addRow("Фигура:", self.node_shape_combo)
        color_row = QHBoxLayout()
        self.node_color_button = QPushButton()
        self.node_color_button.clicked.connect(self._node_color_clicked)
        color_row.addWidget(self.node_color_button)
        color_row.addStretch(1)
        form.addRow("Цвет:", color_row)
        image_row = QHBoxLayout()
        self.node_image_preview = QLabel()
        self.node_image_preview.setFixedSize(64, 64)
        self.node_image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.node_image_preview.setStyleSheet(
            "border: 1px solid #999999; background: #ffffff;")
        self.node_image_preview.setText("—")
        image_row.addWidget(self.node_image_preview)
        image_buttons = QVBoxLayout()
        self.node_image_button = QPushButton("Изображение…")
        self.node_image_button.clicked.connect(self._node_image_clicked)
        image_buttons.addWidget(self.node_image_button)
        self.node_image_remove_button = QPushButton("Убрать")
        self.node_image_remove_button.setEnabled(False)
        self.node_image_remove_button.clicked.connect(
            self._node_image_remove_clicked)
        image_buttons.addWidget(self.node_image_remove_button)
        image_buttons.addStretch(1)
        image_row.addLayout(image_buttons)
        image_row.addStretch(1)
        form.addRow("Пиктограмма:", image_row)
        layout.addWidget(form_group)

        impacts_group = QGroupBox("Деструктивные воздействия узла")
        impacts_layout = QVBoxLayout(impacts_group)
        self.impacts_button = QPushButton("Изменить воздействия…")
        self.impacts_button.clicked.connect(self._edit_node_impacts)
        impacts_layout.addWidget(self.impacts_button)
        layout.addWidget(impacts_group)
        return widget

    @staticmethod
    def _rate_spin(maximum=1e6, decimals=5):
        spin = QDoubleSpinBox()
        spin.setRange(0.0, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(0.01)
        return spin

    def _build_links_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(QLabel("Связи системы:"))
        self.link_list = QListWidget()
        self.link_list.currentRowChanged.connect(self._link_selected)
        layout.addWidget(self.link_list)

        self.link_props_group = QGroupBox("Свойства связи")
        form = QFormLayout(self.link_props_group)
        self.link_pair_label = QLabel("—")
        form.addRow("Связь:", self.link_pair_label)
        self.link_color_button = QPushButton()
        self.link_color_button.clicked.connect(self._link_color_clicked)
        form.addRow("Цвет:", self.link_color_button)
        self.link_style_combo = QComboBox()
        for key, title in LINK_STYLE_TITLES.items():
            self.link_style_combo.addItem(title, key)
        self.link_style_combo.currentIndexChanged.connect(
            self._link_field_changed)
        form.addRow("Тип линии:", self.link_style_combo)
        self.link_width_spin = QSpinBox()
        self.link_width_spin.setRange(1, 10)
        self.link_width_spin.valueChanged.connect(self._link_field_changed)
        form.addRow("Толщина:", self.link_width_spin)
        self.link_arrow_start_combo = QComboBox()
        self.link_arrow_end_combo = QComboBox()
        for key, title in ARROW_TITLES.items():
            self.link_arrow_start_combo.addItem(title, key)
            self.link_arrow_end_combo.addItem(title, key)
        self.link_arrow_start_combo.currentIndexChanged.connect(
            self._link_field_changed)
        self.link_arrow_end_combo.currentIndexChanged.connect(
            self._link_field_changed)
        form.addRow("Стрелка в начале:", self.link_arrow_start_combo)
        form.addRow("Стрелка в конце:", self.link_arrow_end_combo)
        self.link_label_start_edit = QLineEdit()
        self.link_label_start_edit.textEdited.connect(self._link_field_changed)
        form.addRow("Подпись в начале:", self.link_label_start_edit)
        self.link_label_end_edit = QLineEdit()
        self.link_label_end_edit.textEdited.connect(self._link_field_changed)
        form.addRow("Подпись в конце:", self.link_label_end_edit)
        self.link_channel_edit = QLineEdit()
        self.link_channel_edit.setPlaceholderText(
            "центральная подпись канала")
        self.link_channel_edit.textEdited.connect(
            self._link_channel_label_changed)
        form.addRow("Подпись канала:", self.link_channel_edit)
        layout.addWidget(self.link_props_group)
        hint = QLabel(
            "«Начало» связи — первый узел пары; стрелки, цвета и типы линий "
            "влияют только на отображение и не меняют расчёт.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return widget

    def _current_link(self) -> tuple[str, str] | None:
        row = self.link_list.currentRow()
        links = self.project["system"].get("links", [])
        if 0 <= row < len(links):
            a, b = links[row]
            return a, b
        return None

    def _reload_link_list(self, select=None):
        current_row = self.link_list.currentRow()  # до clear(): иначе -1
        had_selection = self.link_list.currentItem() is not None
        self.link_list.blockSignals(True)
        self.link_list.clear()
        for link in self.project["system"].get("links", []):
            self.link_list.addItem(" — ".join(str(name) for name in link))
        self.link_list.blockSignals(False)
        if not self.link_list.count():
            self._link_selected(-1)
            return
        if select is None and not had_selection:
            # выбора не было и просьбы выбрать нет — оставляем список пустым
            self._link_selected(-1)
            return
        row = current_row if select is None else select
        row = max(0, min(row, self.link_list.count() - 1))
        self.link_list.setCurrentRow(row)
        self._link_selected(row)  # явно: setCurrentRow без смены строки не
        #                          # даёт сигнала


    def _link_selected(self, row: int):
        if self._loading:
            return
        self._loading = True
        try:
            links = self.project["system"].get("links", [])
            link = links[row] if 0 <= row < len(links) else None
            widgets = (self.link_pair_label, self.link_color_button,
                       self.link_style_combo, self.link_width_spin,
                       self.link_arrow_start_combo, self.link_arrow_end_combo,
                       self.link_label_start_edit, self.link_label_end_edit,
                       self.link_channel_edit)
            for widget in widgets:
                widget.setEnabled(link is not None)
            if link is not None:
                a, b = link
                self.link_pair_label.setText(f"{a} — {b}")
                style = link_style(self._gui_view(), a, b)
                self._update_link_color_button(style["color"])
                index = self.link_style_combo.findData(style["style"])
                self.link_style_combo.setCurrentIndex(max(0, index))
                self.link_width_spin.setValue(style["width"])
                index = self.link_arrow_start_combo.findData(
                    style["arrow_start"])
                self.link_arrow_start_combo.setCurrentIndex(max(0, index))
                index = self.link_arrow_end_combo.findData(style["arrow_end"])
                self.link_arrow_end_combo.setCurrentIndex(max(0, index))
                self.link_label_start_edit.setText(style["label_start"])
                self.link_label_end_edit.setText(style["label_end"])
                self.link_channel_edit.setText(
                    link_labels_from_section(self._gui_view()).get(
                        tuple(sorted((a, b))), ""))
        finally:
            self._loading = False

    def _update_link_color_button(self, color: str):
        self.link_color_button.setText(color)
        self.link_color_button.setStyleSheet(
            f"background-color: {color}; color: #111111;")

    def _link_color_clicked(self):
        link = self._current_link()
        if link is None:
            return
        style = link_style(self._gui_view(), *link)
        color = QColorDialog.getColor(QColor(style["color"]), self,
                                      f"Цвет связи {link[0]} — {link[1]}")
        if color.isValid():
            set_link_style(gui_section(self.project), *link,
                           color=color.name())
            self._update_link_color_button(color.name())
            self._mark_dirty()
            self.canvas.repaint()

    def _link_field_changed(self, *args):
        if self._loading:
            return
        link = self._current_link()
        if link is None:
            return
        set_link_style(gui_section(self.project), *link,
                       style=self.link_style_combo.currentData(),
                       width=self.link_width_spin.value(),
                       arrow_start=self.link_arrow_start_combo.currentData(),
                       arrow_end=self.link_arrow_end_combo.currentData(),
                       label_start=self.link_label_start_edit.text(),
                       label_end=self.link_label_end_edit.text())
        self._mark_dirty()
        self.canvas.repaint()

    def _link_channel_label_changed(self, *args):
        if self._loading:
            return
        link = self._current_link()
        if link is None:
            return
        set_link_label(gui_section(self.project), *link,
                       self.link_channel_edit.text())
        self._mark_dirty()
        self.canvas.repaint()

    def _build_system_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.system_name_edit = QLineEdit()
        self.system_name_edit.textEdited.connect(self._system_field_changed)
        form.addRow("Имя системы:", self.system_name_edit)
        self.w_def_spin = QDoubleSpinBox()
        self.w_def_spin.setRange(0.0, 1.0)
        self.w_def_spin.setDecimals(6)
        self.w_def_spin.setSingleStep(0.05)
        self.w_def_spin.valueChanged.connect(self._system_field_changed)
        form.addRow("W_доп (порог деградации):", self.w_def_spin)
        self.target_combo = QComboBox()
        for key, title in TARGET_TITLES.items():
            self.target_combo.addItem(title, key)
        self.target_combo.currentIndexChanged.connect(self._system_field_changed)
        form.addRow("Целевая функция:", self.target_combo)
        self.runs_spin = QSpinBox()
        self.runs_spin.setRange(1, 100_000_000)
        self.runs_spin.setSingleStep(10_000)
        self.runs_spin.valueChanged.connect(self._system_field_changed)
        form.addRow("Прогоны Монте-Карло:", self.runs_spin)
        seed_row = QHBoxLayout()
        self.seed_check = QCheckBox("фиксированное зерно")
        self.seed_check.toggled.connect(self._system_field_changed)
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2_147_483_647)
        self.seed_spin.valueChanged.connect(self._system_field_changed)
        seed_row.addWidget(self.seed_check)
        seed_row.addWidget(self.seed_spin)
        seed_row.addStretch(1)
        form.addRow("Зерно ГПСЧ:", seed_row)
        return widget

    def _build_protection_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.protection_check = QCheckBox(
            "Задача 3: сравнение вариантов защиты и выбор g*")
        self.protection_check.toggled.connect(self._protection_toggled)
        layout.addWidget(self.protection_check)
        self.protection_box = QGroupBox()
        protection_layout = QVBoxLayout(self.protection_box)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Бюджет C_доп:"))
        self.budget_spin = QDoubleSpinBox()
        self.budget_spin.setRange(0.0, 1e12)
        self.budget_spin.valueChanged.connect(self._protection_field_changed)
        top_row.addWidget(self.budget_spin)
        top_row.addStretch(1)
        protection_layout.addLayout(top_row)

        protection_layout.addWidget(QLabel("Варианты защиты:"))
        self.variant_list = QListWidget()
        self.variant_list.currentRowChanged.connect(self._variant_selected)
        protection_layout.addWidget(self.variant_list)
        variant_buttons = QHBoxLayout()
        add_variant_btn = QPushButton("Добавить вариант")
        add_variant_btn.clicked.connect(self._add_variant)
        del_variant_btn = QPushButton("Удалить вариант")
        del_variant_btn.clicked.connect(self._delete_variant)
        variant_buttons.addWidget(add_variant_btn)
        variant_buttons.addWidget(del_variant_btn)
        protection_layout.addLayout(variant_buttons)

        variant_form = QFormLayout()
        self.variant_name_edit = QLineEdit()
        self.variant_name_edit.textEdited.connect(self._variant_field_changed)
        variant_form.addRow("Имя:", self.variant_name_edit)
        self.variant_cost_spin = QDoubleSpinBox()
        self.variant_cost_spin.setRange(0.0, 1e12)
        self.variant_cost_spin.valueChanged.connect(self._variant_field_changed)
        variant_form.addRow("C(g):", self.variant_cost_spin)
        protection_layout.addLayout(variant_form)

        protection_layout.addWidget(QLabel(
            "Переопределения узлов (пусто — не меняется):"))
        self.override_table = QTableWidget(0, 4)
        self.override_table.setHorizontalHeaderLabels(
            ["Узел", "λ⁽⁰⁾", "μ", "Воздействия"])
        self.override_table.itemChanged.connect(self._override_item_changed)
        self.override_table.cellDoubleClicked.connect(self._override_cell_dbl)
        protection_layout.addWidget(self.override_table)

        added_row = QHBoxLayout()
        added_row.addWidget(QLabel("Добавляемые связи:"))
        self.added_link_a = QComboBox()
        self.added_link_b = QComboBox()
        add_link_btn = QPushButton("Добавить")
        add_link_btn.clicked.connect(self._add_variant_link)
        self.added_links_list = QListWidget()
        self.added_links_list.setMaximumHeight(70)
        remove_link_btn = QPushButton("Удалить связь")
        remove_link_btn.clicked.connect(self._remove_variant_link)
        added_row.addWidget(self.added_link_a)
        added_row.addWidget(self.added_link_b)
        added_row.addWidget(add_link_btn)
        added_row.addWidget(remove_link_btn)
        added_row.addStretch(1)
        protection_layout.addLayout(added_row)
        protection_layout.addWidget(self.added_links_list)
        layout.addWidget(self.protection_box)
        return widget

    # ------------------------------------------------------------------
    # загрузка проекта в виджеты
    # ------------------------------------------------------------------
    def _load_project_into_widgets(self):
        self._loading = True
        try:
            self._refresh_role_combos()
            system = self.project["system"]
            self.system_name_edit.setText(system.get("name", "система"))
            self.w_def_spin.setValue(system.get("w_def", 0.5))
            index = self.target_combo.findData(system.get("target", "node_fraction"))
            self.target_combo.setCurrentIndex(max(0, index))
            self.runs_spin.setValue(system.get("monte_carlo_runs", 100000))
            seed = system.get("seed")
            self.seed_check.setChecked(seed is not None)
            self.seed_spin.setEnabled(seed is not None)
            if seed is not None:
                self.seed_spin.setValue(seed)
            self._reload_node_list(select=0)
            self._reload_link_list(select=0)
            protection = self.project.get("protection")
            self.protection_check.setChecked(protection is not None)
            self.protection_box.setEnabled(protection is not None)
            if protection:
                self.budget_spin.setValue(protection.get("budget", 0.0))
                self._reload_variant_list(select=0)
            else:
                self.project.pop("protection", None)
                self._reload_variant_list(select=-1)
        finally:
            self._loading = False

    def _reload_node_list(self, select=None):
        current_row = self.node_list.currentRow()  # до clear(): иначе -1
        self.node_list.blockSignals(True)
        self.node_list.clear()
        for node in self.project["system"]["nodes"]:
            self.node_list.addItem(node["name"])
        self.node_list.blockSignals(False)
        if self.node_list.count():
            row = current_row if select is None else select
            row = max(0, min(row, self.node_list.count() - 1))
            self.node_list.setCurrentRow(row)
            self._node_selected(row)  # явно: setCurrentRow без смены строки
        else:                         # сигнала не даёт
            self._node_selected(-1)
        self._refresh_node_combos()

    def _refresh_node_combos(self):
        names = [n["name"] for n in self.project["system"]["nodes"]]
        for combo in (self.added_link_a, self.added_link_b):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            if current in names:
                combo.setCurrentText(current)
            combo.blockSignals(False)

    def _reload_variant_list(self, select=None):
        current_row = self.variant_list.currentRow()  # до clear()
        self.variant_list.blockSignals(True)
        self.variant_list.clear()
        protection = self.project.get("protection")
        if protection:
            for variant in protection.get("variants", []):
                self.variant_list.addItem(variant.get("name", ""))
        self.variant_list.blockSignals(False)
        if self.variant_list.count():
            row = current_row if select is None else select
            row = max(0, min(row, self.variant_list.count() - 1))
            self.variant_list.setCurrentRow(row)
            self._variant_selected(row)  # явно: сигнала может не быть
        else:
            self._variant_selected(-1)

    def _layout_new_nodes(self):
        names = [n["name"] for n in self.project["system"]["nodes"]]
        fresh = circle_layout(names, width=max(600, self.canvas.width()),
                              height=max(360, self.canvas.height()))
        for name, pos in fresh.items():
            self.positions.setdefault(name, pos)
        for name in list(self.positions):
            if name not in names:
                del self.positions[name]
        self.canvas.repaint()

    # ------------------------------------------------------------------
    # обработчики: узлы
    # ------------------------------------------------------------------
    def _node_selected(self, row: int):
        if self._loading:
            return
        self._loading = True
        try:
            node = (self.project["system"]["nodes"][row]
                    if 0 <= row < len(self.project["system"]["nodes"]) else None)
            enabled = node is not None
            for widget in (self.node_name_edit, self.node_own_spin,
                           self.node_mu_spin, self.node_period_spin,
                           self.node_role_combo, self.node_shape_combo,
                           self.node_color_button, self.node_image_button,
                           self.node_image_remove_button, self.impacts_button):
                widget.setEnabled(enabled)
            if node is not None:
                self.node_name_edit.setText(node["name"])
                self.node_own_spin.setValue(node.get("own_failure_rate", 0.0))
                self.node_mu_spin.setValue(node.get("recovery_rate", 0.0))
                self.node_period_spin.setValue(node.get("period", 1.0))
                style = node_style(self._gui_view(), node["name"])
                index = self.node_shape_combo.findData(style["shape"])
                self.node_shape_combo.setCurrentIndex(max(0, index))
                index = (self.node_role_combo.findData(style["role"])
                         if style["role"] else -1)
                self.node_role_combo.setCurrentIndex(max(0, index))
                self._update_color_button(style["color"])
                self._update_node_image_widgets(node["name"])
            else:
                self._update_node_image_widgets(None)
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    # секция gui: доступ и оформление узлов
    # ------------------------------------------------------------------
    def _gui_view(self) -> dict:
        """Секция gui проекта для чтения (без создания)."""
        section = self.project.get("gui")
        return section if isinstance(section, dict) else {}

    def _on_style_changed(self):
        """Оформление топологии изменилось (канвас/диалоги)."""
        node = self._current_node()
        self._update_node_image_widgets(node["name"] if node else None)
        self._mark_dirty()
        self.canvas.repaint()

    def _refresh_role_combos(self):
        """Список ролей в свойствах узла: встроенные + пользовательские."""
        roles = builtin_roles() + user_roles(self._gui_view())
        current = self.node_role_combo.currentData()
        self.node_role_combo.blockSignals(True)
        self.node_role_combo.clear()
        self.node_role_combo.addItem("— не задана —", None)
        for role in roles:
            self.node_role_combo.addItem(role["name"], role["name"])
        index = self.node_role_combo.findData(current)
        self.node_role_combo.setCurrentIndex(max(0, index))
        self.node_role_combo.blockSignals(False)

    def _find_role(self, name: str) -> dict | None:
        for role in builtin_roles() + user_roles(self._gui_view()):
            if role["name"] == name:
                return role
        return None

    def _update_color_button(self, color: str | None):
        shown = color or DEFAULT_NODE_COLOR
        self.node_color_button.setText(shown)
        self.node_color_button.setStyleSheet(
            f"background-color: {shown}; color: {'#111111' if shown.lower() not in ('#000000',) else '#ffffff'};")

    def _node_color_clicked(self):
        node = self._current_node()
        if node is None:
            return
        style = node_style(self._gui_view(), node["name"])
        initial = QColor(style["color"] or DEFAULT_NODE_COLOR)
        color = QColorDialog.getColor(initial, self,
                                      f"Цвет узла «{node['name']}»")
        if color.isValid():
            self._apply_node_color(color.name())

    def _apply_node_color(self, color_hex: str):
        node = self._current_node()
        if node is None:
            return
        set_node_style(gui_section(self.project), node["name"],
                       color=color_hex)
        self._update_color_button(color_hex)
        self._mark_dirty()
        self.canvas.repaint()

    def _node_shape_changed(self, *args):
        if self._loading:
            return
        node = self._current_node()
        if node is None:
            return
        shape = self.node_shape_combo.currentData()
        set_node_style(gui_section(self.project), node["name"], shape=shape)
        self._mark_dirty()
        self.canvas.repaint()

    # ------------------------------------------------------------------
    # пиктограммы узлов
    # ------------------------------------------------------------------
    def _update_node_image_widgets(self, name: str | None):
        """Превью и кнопка «Убрать» в свойствах узла (нет узла/картинки —
        заглушка и неактивная кнопка)."""
        if name is not None:
            image = self.canvas._node_qimage(name)
        else:
            image = None
        if image is None:
            self.node_image_preview.setPixmap(QPixmap())
            self.node_image_preview.setText("—" if name is None else "нет")
            self.node_image_remove_button.setEnabled(False)
            return
        preview = image.scaled(64, 64,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        self.node_image_preview.setPixmap(QPixmap.fromImage(preview))
        self.node_image_remove_button.setEnabled(True)

    def _node_image_clicked(self):
        node = self._current_node()
        if node is None:
            return
        self._choose_node_image(node["name"])

    def _choose_node_image(self, name: str):
        """Диалог выбора файла → data-URI в секции gui."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Изображение узла «{name}»", "",
            "Изображения (*.png *.jpg *.jpeg *.bmp);;Все файлы (*)")
        if path:
            self._apply_node_image(name, Path(path))

    def _apply_node_image(self, name: str, path: Path) -> bool:
        """Чтение файла, запись data-URI в секцию gui; ошибки — диалог,
        проект и расчёт не меняются."""
        try:
            data_uri = load_node_image_data_uri(path)
        except (ValidationError, OSError) as exc:
            self._show_error(f"Изображение не загружено:\n{exc}")
            return False
        set_node_image(gui_section(self.project), name, data_uri)
        self.canvas.invalidate_node_image(name)
        self._update_node_image_widgets(name)
        self._mark_dirty()
        self.canvas.repaint()
        self.statusBar().showMessage(
            f"Пиктограмма узла «{name}» сохранена в проекте", 5000)
        return True

    def _node_image_remove_clicked(self):
        if self._loading:
            return
        node = self._current_node()
        if node is None:
            return
        clear_node_image(gui_section(self.project), node["name"])
        self.canvas.invalidate_node_image(node["name"])
        self._update_node_image_widgets(node["name"])
        self._mark_dirty()
        self.canvas.repaint()

    def _node_role_changed(self, *args):
        if self._loading:
            return
        node = self._current_node()
        if node is None:
            return
        section = gui_section(self.project)
        role_name = self.node_role_combo.currentData()
        if role_name is None:
            set_node_style(section, node["name"], role=None)
            self._mark_dirty()
            return
        role = self._find_role(role_name)
        if role is None:
            return
        node["own_failure_rate"] = role["own_failure_rate"]
        node["recovery_rate"] = role["recovery_rate"]
        node["period"] = role["period"]
        node["impacts"] = copy.deepcopy(role.get("impacts", []))
        set_node_style(section, node["name"], role=role["name"])
        self._loading = True
        try:
            self.node_own_spin.setValue(role["own_failure_rate"])
            self.node_mu_spin.setValue(role["recovery_rate"])
            self.node_period_spin.setValue(role["period"])
        finally:
            self._loading = False
        self._mark_dirty()
        self.canvas.repaint()

    def _current_node(self) -> dict | None:
        row = self.node_list.currentRow()
        nodes = self.project["system"]["nodes"]
        return nodes[row] if 0 <= row < len(nodes) else None

    def _node_field_changed(self, *args):
        if self._loading:
            return
        node = self._current_node()
        if node is None:
            return
        old_name = node["name"]
        new_name = self.node_name_edit.text().strip()
        if new_name and new_name != old_name:
            names = [n["name"] for n in self.project["system"]["nodes"]]
            if names.count(old_name) == 1:
                self._rename_node(old_name, new_name)
                node["name"] = new_name
        node["own_failure_rate"] = self.node_own_spin.value()
        node["recovery_rate"] = self.node_mu_spin.value()
        node["period"] = self.node_period_spin.value()
        self._mark_dirty()
        item = self.node_list.currentItem()
        if item is not None:
            item.setText(node["name"])
        self.canvas.repaint()

    def _rename_node(self, old: str, new: str):
        system = self.project["system"]
        for link in system.get("links", []):
            for i in range(2):
                if link[i] == old:
                    link[i] = new
        protection = self.project.get("protection")
        if protection:
            for variant in protection.get("variants", []):
                overrides = variant.get("nodes", {})
                if old in overrides:
                    overrides[new] = overrides.pop(old)
                for link in variant.get("links_added", []):
                    for i in range(2):
                        if link[i] == old:
                            link[i] = new
        if old in self.positions:
            self.positions[new] = self.positions.pop(old)
        rename_node_in_gui(gui_section(self.project), old, new)
        self.canvas.invalidate_node_image(old)
        self.canvas.selected_node = new
        self._reload_link_list()

    def _edit_node_impacts(self):
        node = self._current_node()
        if node is None:
            return
        dialog = ImpactsDialog(node.get("impacts", []), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                node["impacts"] = dialog.values()
            except ValidationError as exc:
                self._show_error(str(exc))
                return
            self._mark_dirty()

    def _edit_roles(self):
        """Диалог пользовательских ролей; результат — в секцию gui."""
        section = gui_section(self.project)
        dialog = RolesDialog(user_roles(section), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        roles = dialog.values()
        if roles:
            section["roles"] = roles
        else:
            section.pop("roles", None)
        self._refresh_role_combos()
        self._mark_dirty()
        self.statusBar().showMessage(
            "Роли сохранены в проекте (секция gui)", 5000)

    def _edit_link_label(self, a: str, b: str):
        """Подпись канала связи (пустой текст — без подписи)."""
        section = gui_section(self.project)
        current = link_labels_from_section(section).get(
            tuple(sorted((a, b))), "")
        label, ok = QInputDialog.getText(
            self, "Подпись канала", f"Канал {a} — {b}:", text=current)
        if ok:
            set_link_label(section, a, b, label)
            self._mark_dirty()
            self.canvas.repaint()

    def _node_from_role(self, name: str, role: dict | None) -> dict:
        """Словарь узла: параметры роли (копия) или нулевые по умолчанию."""
        if role is None:
            return {"name": name, "own_failure_rate": 0.0,
                    "recovery_rate": 0.0, "period": 1.0, "impacts": []}
        return {"name": name,
                "own_failure_rate": role["own_failure_rate"],
                "recovery_rate": role["recovery_rate"],
                "period": role["period"],
                "impacts": copy.deepcopy(role.get("impacts", []))}

    def _add_node(self):
        names = {n["name"] for n in self.project["system"]["nodes"]}
        index = len(names) + 1
        while f"Узел-{index}" in names:
            index += 1
        roles = builtin_roles() + user_roles(self._gui_view())
        dialog = AddNodeDialog(f"Узел-{index}", roles, self,
                               existing_names=names)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, role = dialog.values()
        name = name or f"Узел-{index}"
        if name in names:
            self._show_error(f"Узел с именем {name!r} уже существует.")
            return
        self.project["system"]["nodes"].append(
            self._node_from_role(name, role))
        if role is not None:
            set_node_style(gui_section(self.project), name,
                           role=role["name"])
        self._mark_dirty()
        self._reload_node_list(select=self.node_list.count() - 1)
        self._layout_new_nodes()

    def _delete_node(self):
        row = self.node_list.currentRow()
        nodes = self.project["system"]["nodes"]
        if not (0 <= row < len(nodes)):
            return
        name = nodes[row]["name"]
        del nodes[row]
        system = self.project["system"]
        system["links"] = [link for link in system.get("links", [])
                           if name not in link]
        protection = self.project.get("protection")
        if protection:
            for variant in protection.get("variants", []):
                variant.get("nodes", {}).pop(name, None)
                variant["links_added"] = [
                    link for link in variant.get("links_added", [])
                    if name not in link]
        self.positions.pop(name, None)
        drop_node_from_gui(gui_section(self.project), name)
        self.canvas.invalidate_node_image(name)
        self._mark_dirty()
        self._reload_node_list(select=min(row, len(nodes) - 1))
        self._reload_link_list()
        self.canvas.repaint()

    # ------------------------------------------------------------------
    # обработчики: система и защита
    # ------------------------------------------------------------------
    def _system_field_changed(self, *args):
        if self._loading:
            return
        system = self.project["system"]
        system["name"] = self.system_name_edit.text()
        system["w_def"] = self.w_def_spin.value()
        system["target"] = self.target_combo.currentData()
        system["monte_carlo_runs"] = self.runs_spin.value()
        if self.seed_check.isChecked():
            system["seed"] = self.seed_spin.value()
        else:
            system.pop("seed", None)
        self.seed_spin.setEnabled(self.seed_check.isChecked())
        self._mark_dirty()

    def _protection_toggled(self, enabled: bool):
        self.protection_box.setEnabled(enabled)
        if self._loading:
            return
        if enabled:
            self.project["protection"] = self.project.get("protection") or {
                "budget": 0.0,
                "variants": [{"name": "Вариант-1", "cost": 0.0}],
            }
        else:
            self.project.pop("protection", None)
        self._reload_variant_list(select=0)
        self._mark_dirty()

    def _protection_field_changed(self, *args):
        if self._loading:
            return
        protection = self.project.get("protection")
        if protection:
            protection["budget"] = self.budget_spin.value()
            self._mark_dirty()

    def _current_variant(self) -> dict | None:
        row = self.variant_list.currentRow()
        protection = self.project.get("protection")
        if not protection:
            return None
        variants = protection.get("variants", [])
        return variants[row] if 0 <= row < len(variants) else None

    def _variant_selected(self, row: int):
        if self._loading:
            return
        self._loading = True
        try:
            variant = self._current_variant()
            enabled = variant is not None
            for widget in (self.variant_name_edit, self.variant_cost_spin,
                           self.override_table, self.added_links_list,
                           self.added_link_a, self.added_link_b):
                widget.setEnabled(enabled)
            if variant is not None:
                self.variant_name_edit.setText(variant.get("name", ""))
                self.variant_cost_spin.setValue(variant.get("cost", 0.0))
            self._reload_override_table(variant)
            self._reload_added_links(variant)
        finally:
            self._loading = False

    def _reload_override_table(self, variant: dict | None):
        self.override_table.blockSignals(True)
        self.override_table.setRowCount(0)
        nodes = self.project["system"]["nodes"]
        overrides = variant.get("nodes", {}) if variant else {}
        self.override_table.setRowCount(len(nodes))
        for row, node in enumerate(nodes):
            name = node["name"]
            ov = overrides.get(name, {})
            self.override_table.setItem(row, 0, QTableWidgetItem(name))
            self.override_table.item(row, 0).setFlags(Qt.ItemFlag.ItemIsEnabled)
            for col, key in ((1, "own_failure_rate"), (2, "recovery_rate")):
                value = ov.get(key)
                text = "" if value is None else repr(value)
                self.override_table.setItem(row, col, QTableWidgetItem(text))
            impacts = ov.get("impacts")
            text = "—" if impacts is None else f"{len(impacts)} воздействия"
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.override_table.setItem(row, 3, item)
        self.override_table.blockSignals(False)

    def _reload_added_links(self, variant: dict | None):
        self.added_links_list.blockSignals(True)
        self.added_links_list.clear()
        if variant:
            for link in variant.get("links_added", []):
                self.added_links_list.addItem(" — ".join(link))
        self.added_links_list.blockSignals(False)

    def _variant_field_changed(self, *args):
        if self._loading:
            return
        variant = self._current_variant()
        if variant is None:
            return
        variant["name"] = self.variant_name_edit.text()
        variant["cost"] = self.variant_cost_spin.value()
        item = self.variant_list.currentItem()
        if item is not None:
            item.setText(variant["name"])
        self._mark_dirty()

    def _override_item_changed(self, item):
        if self._loading or item.column() not in (1, 2):
            return
        variant = self._current_variant()
        if variant is None:
            return
        node_name = self.override_table.item(item.row(), 0).text()
        key = ("own_failure_rate", "recovery_rate")[item.column() - 1]
        text = item.text().strip()
        overrides = variant.setdefault("nodes", {})
        entry = overrides.setdefault(node_name, {})
        if not text:
            entry.pop(key, None)
            if not entry:
                overrides.pop(node_name, None)
        else:
            try:
                entry[key] = float(text.replace(",", "."))
            except ValueError:
                self._loading = True
                item.setText("")
                self._loading = False
                return
        self._mark_dirty()

    def _override_cell_dbl(self, row: int, column: int):
        if column != 3:
            return
        variant = self._current_variant()
        if variant is None:
            return
        node_name = self.override_table.item(row, 0).text()
        entry = variant.setdefault("nodes", {}).setdefault(node_name, {})
        dialog = ImpactsDialog(entry.get("impacts", []), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                impacts = dialog.values()
            except ValidationError as exc:
                self._show_error(str(exc))
                return
            if impacts:
                entry["impacts"] = impacts
            else:
                entry.pop("impacts", None)
                if not entry:
                    variant["nodes"].pop(node_name, None)
            self._loading = True
            item = self.override_table.item(row, 3)
            item.setText("—" if not impacts else f"{len(impacts)} воздействия")
            self._loading = False
            self._mark_dirty()

    def _add_variant(self):
        protection = self.project.setdefault("protection",
                                             {"budget": 0.0, "variants": []})
        names = {v["name"] for v in protection["variants"]}
        index = len(names) + 1
        while f"Вариант-{index}" in names:
            index += 1
        protection["variants"].append({"name": f"Вариант-{index}", "cost": 0.0})
        self._mark_dirty()
        self._reload_variant_list(select=self.variant_list.count() - 1)

    def _delete_variant(self):
        row = self.variant_list.currentRow()
        protection = self.project.get("protection")
        if not protection or not (0 <= row < len(protection["variants"])):
            return
        del protection["variants"][row]
        if not protection["variants"]:
            protection["variants"].append({"name": "Вариант-1", "cost": 0.0})
        self._mark_dirty()
        self._reload_variant_list(select=row)

    def _add_variant_link(self):
        variant = self._current_variant()
        if variant is None:
            return
        a, b = self.added_link_a.currentText(), self.added_link_b.currentText()
        if not a or not b:
            return
        if a == b:
            self._show_error("Петля (связь узла самого с собой) недопустима.")
            return
        key = tuple(sorted((a, b)))
        existing = {tuple(sorted(link)) for link in
                    self.project["system"].get("links", [])}
        for other in self.project["protection"].get("variants", []):
            if other is not variant:
                existing |= {tuple(sorted(link))
                             for link in other.get("links_added", [])}
        if key in existing:
            self._show_error("Такая связь уже существует (в системе или варианте).")
            return
        variant.setdefault("links_added", []).append([a, b])
        self._reload_added_links(variant)
        self._mark_dirty()

    def _remove_variant_link(self):
        variant = self._current_variant()
        row = self.added_links_list.currentRow()
        if variant is None or row < 0:
            return
        del variant.get("links_added", [])[row]
        self._reload_added_links(variant)
        self._mark_dirty()

    # ------------------------------------------------------------------
    # канвас
    # ------------------------------------------------------------------
    def _structure_changed(self):
        self._reload_node_list()
        self._reload_link_list()
        self._layout_new_nodes()
        self._mark_dirty()

    def _canvas_node_clicked(self, name: str):
        """Клик по узлу на канвасе выбирает его в редакторе узлов."""
        for row in range(self.node_list.count()):
            if self.node_list.item(row).text() == name:
                self.node_list.setCurrentRow(row)
                self._node_selected(row)
                break

    def _canvas_link_clicked(self, a: str, b: str):
        """Клик по связи на канвасе выбирает её в списке связей."""
        key = tuple(sorted((a, b)))
        links = self.project["system"].get("links", [])
        for row in range(self.link_list.count()):
            if tuple(sorted(links[row])) == key:
                self.link_list.setCurrentRow(row)
                self._link_selected(row)
                break

    def _canvas_link_style_requested(self, a: str, b: str):
        """«Оформление…» из меню канваса: фокус на панели свойств связи."""
        self._canvas_link_clicked(a, b)
        self.tabs.setCurrentWidget(self.links_tab)

    def _link_mode_toggled(self, checked: bool):
        self.canvas.set_link_mode(checked)
        if checked:
            self.statusBar().showMessage(
                "Режим добавления связи: кликните первый узел, затем второй", 10000)

    def _delete_selected(self):
        self.canvas.delete_selected()

    def _recolor_canvas(self, *args):
        mode = self.color_combo.currentData()
        if self.last_importance and mode == "I":
            self.canvas.set_node_values(self.last_importance)
        else:
            self.canvas.set_node_values(self._last_q_values())

    # ------------------------------------------------------------------
    # файлы проекта
    # ------------------------------------------------------------------
    def new_project(self):
        if not self._confirm_discard():
            return
        self.project = default_project()
        self.positions = {}
        self.current_path = None
        self.dirty = False
        self.canvas.clear_image_cache()
        self._load_project_into_widgets()
        self._layout_new_nodes()
        self.canvas.fit_to_view(max_zoom=1.0)
        self._update_title()

    def open_project(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "",
                                              FILE_FILTER)
        if path:
            self.load_project(Path(path))

    def load_project(self, path: Path):
        """Загрузка файла формата CLI; модель валидируется через io.

        Служебная секция `gui` (метаданные оболочки) читается отдельно:
        CLI её игнорирует, GUI восстанавливает координаты и оформление.
        """
        raw_gui = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("gui"), dict):
                raw_gui = raw["gui"]
        except (OSError, ValueError):
            pass  # повреждённый JSON разберётся ниже в job_from_json
        try:
            job = job_from_json(path)
        except (ValidationError, OSError, ValueError) as exc:
            self._show_error(f"Не удалось открыть проект:\n{exc}")
            return
        if job.system is None:
            self._show_error(
                "Файл описывает одиночный узел (без ключа system).\n"
                "Откройте файл системы — например, examples/example6_system_line.json.")
            return
        self.project = project_to_dict(job.system, job.protection)
        section = sanitize_gui_section(raw_gui)
        if section:
            self.project["gui"] = section
        self.positions = positions_from_section(section)
        self.current_path = path
        self.dirty = False
        self.canvas.clear_image_cache()
        self._load_project_into_widgets()
        self._layout_new_nodes()
        self.canvas.fit_to_view(max_zoom=1.0)
        self._update_title()
        self.statusBar().showMessage(f"Открыт: {path}", 5000)
        self._report_dropped_link_styles(raw_gui, section)
        self._report_dropped_node_images(raw_gui, section)

    def _report_dropped_link_styles(self, raw_gui, section: dict):
        """Сообщение в статусную строку о проигнорированных записях
        оформления связей (секция gui читается «best effort»)."""
        if not isinstance(raw_gui, dict):
            return
        raw = raw_gui.get("link_styles")
        if not isinstance(raw, list):
            return
        raw_count = sum(1 for record in raw if isinstance(record, dict))
        clean_count = len(section.get("link_styles", []))
        if raw_count > clean_count:
            self.statusBar().showMessage(
                f"Проигнорировано повреждённых записей оформления связей: "
                f"{raw_count - clean_count}", 8000)

    def _report_dropped_node_images(self, raw_gui, section: dict):
        """Сообщение в статусную строку о проигнорированных пиктограммах
        узлов (секция gui читается «best effort»)."""
        if not isinstance(raw_gui, dict):
            return
        raw_nodes = raw_gui.get("nodes")
        if not isinstance(raw_nodes, dict):
            return
        dropped = sum(
            1 for name, entry in raw_nodes.items()
            if isinstance(entry, dict) and "image" in entry
            and node_image(section, str(name)) is None)
        if dropped:
            self.statusBar().showMessage(
                f"Проигнорировано повреждённых изображений узлов: {dropped}",
                8000)

    def save_project(self):
        if self.current_path is None:
            return self.save_project_as()
        return self._write_project(self.current_path)

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", "",
                                              FILE_FILTER)
        if not path:
            return False
        return self._write_project(Path(path))

    def _write_project(self, path: Path) -> bool:
        try:
            self._collect_models()  # валидация перед записью
        except ValidationError as exc:
            self._show_error(f"Проект не валиден:\n{exc}")
            return False
        self._sync_gui_to_project()
        try:
            path.write_text(json.dumps(self.project, ensure_ascii=False,
                                       indent=2), encoding="utf-8")
        except OSError as exc:
            self._show_error(f"Ошибка записи файла:\n{exc}")
            return False
        self.current_path = path
        self.dirty = False
        self._update_title()
        self.statusBar().showMessage(f"Сохранено: {path}", 5000)
        return True

    def _sync_gui_to_project(self):
        """Координаты канваса → секция gui; удаление записей об исчезнувших
        узлах/связях; пустая секция не пишется."""
        section = gui_section(self.project)
        set_positions(section, self.positions)
        names = [n["name"] for n in self.project["system"]["nodes"]]
        links = self.project["system"].get("links", [])
        prune_gui_section(section, names, links)
        if not section:
            self.project.pop("gui", None)

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Несохранённые изменения",
            "Проект изменён. Сохранить перед продолжением?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return answer == QMessageBox.StandardButton.Discard

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    def _mark_dirty(self):
        self.dirty = True
        self._update_title()

    def _update_title(self):
        name = self.current_path.name if self.current_path else "без имени"
        mark = "*" if self.dirty else ""
        self.setWindowTitle(f"{APP_TITLE} — {name}{mark}")

    # ------------------------------------------------------------------
    # расчёт
    # ------------------------------------------------------------------
    def _collect_models(self):
        """Словарь проекта → валидированные модели (через io)."""
        system = system_from_dict(self.project["system"])
        protection = None
        if self.project.get("protection"):
            protection = protection_from_dict(self.project["protection"],
                                              system)
        return system, protection

    def calculate_qs(self):
        self._start_calc(protection_mode=False)

    def calculate_protection(self):
        if not self.project.get("protection"):
            self._show_error("Ключ protection отсутствует: задайте бюджет и "
                             "варианты на вкладке «Защита».")
            return
        self._start_calc(protection_mode=True)

    def _start_calc(self, protection_mode: bool):
        if self.worker is not None and self.worker.isRunning():
            self.statusBar().showMessage("Расчёт уже выполняется…")
            return
        try:
            system, protection = self._collect_models()
        except ValidationError as exc:
            self._show_error(f"Проект не валиден:\n{exc}")
            return
        self.calc_action.setEnabled(False)
        self.compare_action.setEnabled(False)
        self.statusBar().showMessage("Расчёт выполняется…")
        self.worker = CalcWorker(
            system, protection if protection_mode else None, parent=self)
        self.worker.done_qs.connect(self._on_qs_done)
        self.worker.done_protection.connect(self._on_protection_done)
        self.worker.failed.connect(self._on_calc_failed)
        self.worker.finished.connect(self._on_calc_finished)
        self.worker.start()

    def _on_calc_finished(self):
        self.calc_action.setEnabled(True)
        self.compare_action.setEnabled(True)
        self.statusBar().clearMessage()

    def _on_qs_done(self, estimate, importance):
        ci = (f"\nДоверительный интервал Уилсона 95 %: "
              f"[{estimate.ci[0]:.6f}; {estimate.ci[1]:.6f}]"
              if estimate.ci else "")
        method = ("точный перебор" if estimate.method == "exact"
                  else f"Монте-Карло, N={estimate.runs}")
        self.summary_label.setText(
            f"Живучесть системы Q(S) = {estimate.q:.6f}\nСпособ расчёта: {method}{ci}")
        self._fill_importance_table(importance)
        self.last_importance = {item.name: item.criticality
                                for item in importance}
        self._last_qs = (estimate, importance)
        self._last_protection = None
        self._recolor_canvas()
        self.results_tabs.setCurrentIndex(0)

    def _on_protection_done(self, result):
        budget = f"{result.budget:g}"
        lines = [
            f"Живучесть без защиты Q_base(S) = {result.base.q:.6f}",
            f"Бюджет C_доп = {budget}",
            f"Рациональный способ защиты g* = {result.chosen.name}",
            f"Q_g*(S) = {result.chosen.estimate.q:.6f}",
            f"Прирост ΔQ = {result.delta_q:+.6f} "
            f"({result.relative_delta:+.2%})",
            f"Эффективность затрат ΔQ / C(g*) = {result.cost_efficiency:.6f}",
        ]
        self.summary_label.setText("\n".join(lines))
        self._fill_variants_table(result)
        self._last_protection = result
        q_values = self._last_q_values()
        if q_values:
            self.canvas.set_node_values(q_values)
        self.results_tabs.setCurrentIndex(1)

    def _on_calc_failed(self, message: str):
        self._show_error(f"Ошибка расчёта:\n{message}")

    def _fill_importance_table(self, importance):
        self.importance_table.setRowCount(0)
        for item in importance:
            row = self.importance_table.rowCount()
            self.importance_table.insertRow(row)
            for col, text in enumerate((
                    item.name, f"{item.criticality:.6f}",
                    f"{item.q_if_up:.6f}", f"{item.q_if_down:.6f}")):
                self.importance_table.setItem(row, col, QTableWidgetItem(text))

    def _fill_variants_table(self, result):
        self.variants_table.setRowCount(0)
        for row_data in result.rows:
            row = self.variants_table.rowCount()
            self.variants_table.insertRow(row)
            values = (
                row_data.name, f"{row_data.cost:g}",
                "да" if row_data.feasible else "НЕТ",
                f"{row_data.estimate.q:.6f}",
                f"{row_data.estimate.q - result.base.q:+.6f}",
                "★ g*" if row_data is result.chosen else "",
            )
            for col, text in enumerate(values):
                self.variants_table.setItem(row, col, QTableWidgetItem(text))

    _last_qs = None
    _last_protection = None

    def _last_q_values(self) -> dict[str, float] | None:
        if self._last_qs is None and self._last_protection is None:
            return None
        try:
            system, _ = self._collect_models()
        except ValidationError:
            return None
        q = node_survivabilities(system)
        return {node.name: value for node, value in zip(system.nodes, q)}

    # ------------------------------------------------------------------
    # экспорт
    # ------------------------------------------------------------------
    def export_svg(self):
        try:
            system, _ = self._collect_models()
        except ValidationError as exc:
            self._show_error(f"Проект не валиден:\n{exc}")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт схемы SVG", "topology.svg",
            "SVG (*.svg);;Все файлы (*)")
        if not path:
            return
        color_mode = self.color_combo.currentData()
        values = None
        color_title = "Q"
        if color_mode == "I" and self.last_importance:
            values = self.last_importance
            color_title = "I"
        elif self._last_q_values():
            values = self._last_q_values()
            color_title = "Q"
        section = self._gui_view()
        styles = {node.name: node_style(section, node.name)
                  for node in system.nodes}
        images: dict[str, str] = {}
        for node in system.nodes:
            data_uri = node_image(section, node.name)
            if data_uri:
                images[node.name] = data_uri
        svg = topology_svg(system, node_values=values,
                           color_by=color_title,
                           positions=dict(self.positions),
                           styles=styles,
                           link_labels=link_labels_from_section(section),
                           link_styles=link_styles_from_section(section),
                           images=images)
        try:
            Path(path).write_text(svg, encoding="utf-8")
        except OSError as exc:
            self._show_error(f"Ошибка записи файла:\n{exc}")
            return
        self.statusBar().showMessage(f"Схема сохранена: {path}", 5000)

    def export_dot(self):
        try:
            system, _ = self._collect_models()
        except ValidationError as exc:
            self._show_error(f"Проект не валиден:\n{exc}")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт схемы DOT", "topology.dot",
            "Graphviz DOT (*.dot);;Все файлы (*)")
        if not path:
            return
        try:
            section = self._gui_view()
            styles = {node.name: node_style(section, node.name)
                      for node in system.nodes}
            Path(path).write_text(
                topology_dot(system, styles=styles,
                             link_styles=link_styles_from_section(section)),
                encoding="utf-8")
        except OSError as exc:
            self._show_error(f"Ошибка записи файла:\n{exc}")
            return
        self.statusBar().showMessage(f"DOT сохранён: {path}", 5000)

    def export_results(self):
        if self._last_qs is None and self._last_protection is None:
            self._show_error("Нет результатов: выполните расчёт (F5 или F6).")
            return
        from .io import (save_protection_csv, save_protection_json,
                         save_system_csv, save_system_json)
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт результатов", "results.json",
            "JSON (*.json);;CSV (*.csv);;Все файлы (*)")
        if not path:
            return
        try:
            system, _ = self._collect_models()
            target = Path(path)
            if self._last_protection is not None:
                if target.suffix.lower() == ".csv":
                    save_protection_csv(target, self._last_protection)
                else:
                    save_protection_json(target, self._last_protection)
            else:
                estimate, importance = self._last_qs
                if target.suffix.lower() == ".csv":
                    save_system_csv(target, system, estimate, importance)
                else:
                    save_system_json(target, system, estimate, importance)
        except (ValidationError, OSError) as exc:
            self._show_error(f"Ошибка экспорта:\n{exc}")
            return
        self.statusBar().showMessage(f"Результаты сохранены: {path}", 5000)

    def _show_error(self, message: str):
        QMessageBox.critical(self, "survivability", message)


# ---------------------------------------------------------------------------
# Точка входа: python -m survivability.gui
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("survivability")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
