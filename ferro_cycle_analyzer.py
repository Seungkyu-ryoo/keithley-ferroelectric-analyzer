import contextlib
import io
import re
import sys
import warnings
from functools import lru_cache
from pathlib import Path

import matplotlib.cm as cm
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy.integrate as integrate
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from scipy.interpolate import interp1d


DEFAULT_FOLDER = r"C:\Users\ryoo\Desktop\LDRD\Data\Kiethley\20260504\ALD_HO_716V_10.1ms\Rep5_3V_4V"
DEFAULT_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]


@lru_cache(maxsize=256)
def read_excel_quiet_cached(path_text, modified_time, sheet_name, header):
    with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
        warnings.simplefilter("ignore")
        return pd.read_excel(path_text, sheet_name=sheet_name, header=header)


def read_excel_quiet(path, sheet_name, header=0):
    resolved = Path(path).resolve()
    return read_excel_quiet_cached(
        str(resolved), resolved.stat().st_mtime, sheet_name, header
    ).copy()


def get_setting(settings_df, name):
    rows = settings_df[settings_df.iloc[:, 0].astype(str).str.strip() == name]
    if rows.empty:
        raise ValueError(f"Missing setting: {name}")
    return float(rows.iloc[0, 3])


def parse_cycle_from_name(path):
    stem = Path(path).stem.lower()
    if "pristine" in stem:
        return 1
    cycle_match = re.search(
        r"(\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(?:_|-|\s)*cycle\b", stem
    )
    if cycle_match:
        return cycle_text_to_number(cycle_match.group(1))
    tokens = re.findall(r"(?<![a-z])\d+(?:\.\d+)?(?:e[+-]?\d+)?", stem)
    if not tokens:
        return None
    return cycle_text_to_number(tokens[-1])


def cycle_text_to_number(text):
    value = float(text.replace("e", "E"))
    return int(value) if value.is_integer() else value


def cycle_label(cycle):
    if cycle == 0:
        return "0"
    exp = np.log10(cycle)
    if abs(exp - round(exp)) < 1e-9 and cycle >= 10:
        return f"1e{int(round(exp))}"
    return f"{cycle:g}"


def list_excel_files(folder):
    folder = Path(folder)
    if not folder.exists():
        return []
    return sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".xls", ".xlsx")]
    )


def find_excel_files(folder):
    folder = Path(folder)
    if not folder.exists():
        return []
    return sorted(
        [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".xls", ".xlsx")]
    )


def subfolder(base_folder, name):
    base = Path(base_folder)
    if base.name.lower() == name.lower():
        return base
    for child in base.iterdir() if base.exists() else []:
        if child.is_dir() and child.name.lower() == name.lower():
            return child
    if list_excel_files(base):
        return base
    return base / name


def classify_excel_file(path):
    try:
        data_df = read_excel_quiet(path, "Data", header=0)
    except Exception:
        return None
    columns = set(map(str, data_df.columns))
    if {"pundEndurance", "iteration", "Psw", "Qsw"}.issubset(columns):
        return "Endurance"
    if {"pundTest", "V", "I", "t"}.issubset(columns):
        return "PUND"
    if {"doubleSweepSeg", "Vforce", "Charge"}.issubset(columns):
        return "PV"
    return None


def files_for_mode(base_folder, mode):
    base = Path(base_folder)
    preferred = subfolder(base, mode)
    candidates = []
    seen = set()
    top_level = list_excel_files(base)
    preferred_files = [] if preferred.resolve() == base.resolve() else list_excel_files(preferred)
    for path in top_level + preferred_files + find_excel_files(base):
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)
    return [path for path in candidates if classify_excel_file(path) == mode]


def charge_to_polarization(charge, area_um2):
    return charge / (area_um2 * 1e-8) * 1e6


def current_to_density(current, area_um2):
    return current / (area_um2 * 1e-8)


def voltage_to_field(voltage, thickness_nm):
    return voltage * 10.0 / thickness_nm


class FerroCycleQtApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ferroelectric Cycle Analyzer - Qt")
        self.resize(1500, 950)

        self.cycle_file_map = {}
        self.available_cycles = []
        self.current_df = None
        self.last_mode = "Endurance"
        self.canvas = None
        self.toolbar = None
        self.fig = None
        self.ax = None

        self.build_ui()
        self.refresh_files()
        self.new_figure()

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        top_panel = QFrame()
        top_layout = QGridLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setHorizontalSpacing(10)
        top_layout.setVerticalSpacing(8)
        main_layout.addWidget(top_panel)

        folder_box = QGroupBox("Folder / Mode")
        folder_layout = QVBoxLayout(folder_box)
        self.folder_edit = QLineEdit(DEFAULT_FOLDER)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_folder)
        refresh_btn = QPushButton("Refresh files")
        refresh_btn.clicked.connect(self.refresh_files)
        row = QHBoxLayout()
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse_btn)
        row.addWidget(refresh_btn)
        folder_layout.addLayout(row)

        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        for idx, name in enumerate(["Endurance", "PUND", "PV"]):
            btn = QPushButton(name)
            btn.setCheckable(True)
            if idx == 0:
                btn.setChecked(True)
            self.mode_group.addButton(btn, idx)
            mode_row.addWidget(btn)
        self.mode_group.idClicked.connect(lambda _: self.refresh_files())
        folder_layout.addLayout(mode_row)
        top_layout.addWidget(folder_box, 0, 0, 1, 3)

        settings_box = QGroupBox("Analysis settings")
        form = QFormLayout(settings_box)
        self.area_spin = self.double_spin(400.0, 1.0, 1e9, 1)
        self.thick_spin = self.double_spin(10.0, 0.1, 1e6, 2)
        self.x_axis_combo = QComboBox()
        self.x_axis_combo.addItems(["Voltage", "Field"])
        self.pv_y_combo = QComboBox()
        self.pv_y_combo.addItems(["Polarization", "Current density"])
        self.pv_plot_type_combo = QComboBox()
        self.pv_plot_type_combo.addItems(["Loop", "Ec vs cycle", "Pr vs cycle"])
        self.pv_plot_type_combo.currentTextChanged.connect(self.update_pv_metric_options)
        form.addRow("Area (um^2)", self.area_spin)
        form.addRow("Thickness (nm)", self.thick_spin)
        form.addRow("PUND/PV X-axis", self.x_axis_combo)
        form.addRow("PV Y-axis", self.pv_y_combo)
        form.addRow("PUND/PV plot type", self.pv_plot_type_combo)
        top_layout.addWidget(settings_box, 1, 0)

        plot_box = QGroupBox("Plot settings")
        plot_grid = QGridLayout(plot_box)
        self.fig_w_spin = self.double_spin(5.0, 1.0, 20.0, 2)
        self.fig_h_spin = self.double_spin(4.0, 1.0, 20.0, 2)
        self.dpi_spin = self.int_spin(120, 50, 600)
        self.title_size_spin = self.int_spin(18, 6, 60)
        self.axis_size_spin = self.int_spin(14, 6, 60)
        self.tick_size_spin = self.int_spin(12, 6, 60)
        self.legend_size_spin = self.int_spin(11, 6, 60)
        self.line_width_spin = self.double_spin(2.2, 0.1, 20.0, 2)
        self.marker_size_spin = self.double_spin(5.5, 0.0, 30.0, 1)
        self.endurance_axis_combo = QComboBox()
        self.endurance_axis_combo.addItems(["Normal", "Broken"])
        self.break_after_spin = self.int_spin(1, 1, 999)
        self.grid_check = QCheckBox("Grid")
        self.grid_check.setChecked(True)
        self.center_check = QCheckBox("Center dotted axes")
        self.center_check.setChecked(True)
        self.lock_size_check = QCheckBox("Lock canvas to figure size")
        self.lock_size_check.setChecked(True)
        self.show_title_check = QCheckBox("Title")
        self.show_title_check.setChecked(True)
        self.show_x_label_check = QCheckBox("X label")
        self.show_x_label_check.setChecked(True)
        self.show_y_label_check = QCheckBox("Y label")
        self.show_y_label_check.setChecked(True)
        self.x_min_edit = QLineEdit()
        self.x_max_edit = QLineEdit()
        self.y_min_edit = QLineEdit()
        self.y_max_edit = QLineEdit()
        self.x_min_edit.setPlaceholderText("auto")
        self.x_max_edit.setPlaceholderText("auto")
        self.y_min_edit.setPlaceholderText("auto")
        self.y_max_edit.setPlaceholderText("auto")

        plot_grid.addWidget(QLabel("W"), 0, 0)
        plot_grid.addWidget(self.fig_w_spin, 0, 1)
        plot_grid.addWidget(QLabel("H"), 0, 2)
        plot_grid.addWidget(self.fig_h_spin, 0, 3)
        plot_grid.addWidget(QLabel("DPI"), 0, 4)
        plot_grid.addWidget(self.dpi_spin, 0, 5)
        plot_grid.addWidget(QLabel("Title"), 0, 6)
        plot_grid.addWidget(self.title_size_spin, 0, 7)
        plot_grid.addWidget(QLabel("Axis"), 0, 8)
        plot_grid.addWidget(self.axis_size_spin, 0, 9)
        plot_grid.addWidget(QLabel("Tick"), 0, 10)
        plot_grid.addWidget(self.tick_size_spin, 0, 11)

        plot_grid.addWidget(QLabel("Legend"), 1, 0)
        plot_grid.addWidget(self.legend_size_spin, 1, 1)
        plot_grid.addWidget(QLabel("Line"), 1, 2)
        plot_grid.addWidget(self.line_width_spin, 1, 3)
        plot_grid.addWidget(QLabel("Marker"), 1, 4)
        plot_grid.addWidget(self.marker_size_spin, 1, 5)
        plot_grid.addWidget(self.grid_check, 1, 6)
        plot_grid.addWidget(self.center_check, 1, 7)
        plot_grid.addWidget(self.lock_size_check, 1, 8, 1, 2)
        plot_grid.addWidget(self.show_title_check, 1, 10)
        plot_grid.addWidget(self.show_x_label_check, 1, 11)
        plot_grid.addWidget(self.show_y_label_check, 1, 12)

        plot_grid.addWidget(QLabel("X min/max"), 2, 0)
        plot_grid.addWidget(self.x_min_edit, 2, 1)
        plot_grid.addWidget(self.x_max_edit, 2, 2)
        plot_grid.addWidget(QLabel("Y min/max"), 2, 3)
        plot_grid.addWidget(self.y_min_edit, 2, 4)
        plot_grid.addWidget(self.y_max_edit, 2, 5)
        plot_grid.addWidget(QLabel("Endurance X"), 2, 6)
        plot_grid.addWidget(self.endurance_axis_combo, 2, 7)
        plot_grid.addWidget(QLabel("Break after item"), 2, 8)
        plot_grid.addWidget(self.break_after_spin, 2, 9)
        top_layout.addWidget(plot_box, 1, 1, 1, 2)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)
        splitter.addWidget(controls)

        plot_host = QWidget()
        self.plot_layout = QVBoxLayout(plot_host)
        self.plot_layout.setContentsMargins(10, 10, 10, 10)
        splitter.addWidget(plot_host)
        splitter.setSizes([340, 1160])

        color_box = QGroupBox("Colors")
        color_layout = QVBoxLayout(color_box)
        self.color_mode_combo = QComboBox()
        self.color_mode_combo.addItems(["Custom", "Colormap", "Single + alpha"])
        self.cmap_combo = QComboBox()
        self.cmap_combo.addItems(["viridis", "plasma", "inferno", "magma", "cividis", "turbo", "tab10", "Set2"])
        self.color_list = QListWidget()
        self.color_list.setFixedHeight(120)
        self.reset_color_list()
        color_btn_row = QHBoxLayout()
        add_color_btn = QPushButton("Add color")
        add_color_btn.clicked.connect(self.add_color)
        remove_color_btn = QPushButton("Remove")
        remove_color_btn.clicked.connect(self.remove_color)
        reset_color_btn = QPushButton("Reset")
        reset_color_btn.clicked.connect(self.reset_color_list)
        color_btn_row.addWidget(add_color_btn)
        color_btn_row.addWidget(remove_color_btn)
        color_btn_row.addWidget(reset_color_btn)
        color_layout.addWidget(self.color_mode_combo)
        color_layout.addWidget(self.cmap_combo)
        color_layout.addWidget(self.color_list)
        color_layout.addLayout(color_btn_row)
        controls_layout.addWidget(color_box)

        cycle_box = QGroupBox("Cycle selection")
        cycle_layout = QVBoxLayout(cycle_box)
        self.cycle_list = QListWidget()
        self.cycle_list.setSelectionMode(QListWidget.MultiSelection)
        self.cycle_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.cycle_list.setDefaultDropAction(Qt.MoveAction)
        cycle_layout.addWidget(self.cycle_list)
        cycle_btn_row = QHBoxLayout()
        select_all_btn = QPushButton("Select all")
        select_all_btn.clicked.connect(self.select_all_cycles)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.cycle_list.clearSelection)
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(lambda: self.move_selected_items(-1))
        down_btn = QPushButton("Down")
        down_btn.clicked.connect(lambda: self.move_selected_items(1))
        cycle_btn_row.addWidget(select_all_btn)
        cycle_btn_row.addWidget(clear_btn)
        cycle_btn_row.addWidget(up_btn)
        cycle_btn_row.addWidget(down_btn)
        cycle_layout.addLayout(cycle_btn_row)
        controls_layout.addWidget(cycle_box, 1)

        metric_box = QGroupBox("PUND/PV metric selection")
        metric_layout = QVBoxLayout(metric_box)
        self.metric_list = QListWidget()
        self.metric_list.setSelectionMode(QListWidget.MultiSelection)
        metric_layout.addWidget(self.metric_list)
        metric_btn_row = QHBoxLayout()
        select_metric_btn = QPushButton("Select all")
        select_metric_btn.clicked.connect(self.select_all_metrics)
        clear_metric_btn = QPushButton("Clear")
        clear_metric_btn.clicked.connect(self.metric_list.clearSelection)
        metric_btn_row.addWidget(select_metric_btn)
        metric_btn_row.addWidget(clear_metric_btn)
        metric_layout.addLayout(metric_btn_row)
        controls_layout.addWidget(metric_box)
        self.metric_box = metric_box
        self.update_pv_metric_options()

        action_row = QHBoxLayout()
        plot_btn = QPushButton("Plot")
        plot_btn.clicked.connect(self.plot_current_mode)
        save_fig_btn = QPushButton("Save figure")
        save_fig_btn.clicked.connect(self.save_figure)
        action_row.addWidget(plot_btn)
        action_row.addWidget(save_fig_btn)
        controls_layout.addLayout(action_row)

        save_row = QHBoxLayout()
        save_csv_btn = QPushButton("Save CSV")
        save_csv_btn.clicked.connect(self.save_csv)
        save_xlsx_btn = QPushButton("Save Excel")
        save_xlsx_btn.clicked.connect(self.save_excel)
        save_row.addWidget(save_csv_btn)
        save_row.addWidget(save_xlsx_btn)
        controls_layout.addLayout(save_row)

        self.status_label = QLabel("Ready")
        controls_layout.addWidget(self.status_label)

    def double_spin(self, value, minimum, maximum, decimals):
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setValue(value)
        spin.setSingleStep(0.1)
        return spin

    def int_spin(self, value, minimum, maximum):
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    def mode(self):
        return self.mode_group.checkedButton().text()

    def set_status(self, text):
        self.status_label.setText(text)

    def update_pv_metric_options(self):
        if not hasattr(self, "metric_list"):
            return
        current = set(self.selected_metrics())
        self.metric_list.clear()
        if self.pv_plot_type_combo.currentText() == "Pr vs cycle":
            options = ["Pr+", "Pr-", "|Pr+|", "|Pr-|", "|Pr+|-|Pr-|", "2Pr"]
        elif self.pv_plot_type_combo.currentText() == "Ec vs cycle":
            options = ["Ec+", "Ec-", "|Ec+|", "|Ec-|", "|Ec+|-|Ec-|", "2Ec"]
        else:
            options = []
        for metric in options:
            item = QListWidgetItem(metric)
            item.setData(Qt.UserRole, metric)
            self.metric_list.addItem(item)
            item.setSelected(metric in current or not current)
        if options and not self.selected_metrics():
            self.select_all_metrics()
        self.metric_box.setEnabled(bool(options))

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select base folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)
            self.refresh_files()

    def refresh_files(self):
        self.cycle_list.clear()
        self.cycle_file_map = {}
        mode = self.mode()
        files = files_for_mode(self.folder_edit.text(), mode)
        if mode == "Endurance":
            for path in files:
                try:
                    settings_df = read_excel_quiet(path, "Settings", header=None)
                    max_loops = int(get_setting(settings_df, "max_loops"))
                    label = f"{max_loops:g} loops   ({path.name})"
                except Exception:
                    max_loops = None
                    label = f"? loops   ({path.name})"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, str(path))
                item.setData(Qt.UserRole + 1, max_loops)
                self.cycle_list.addItem(item)
                item.setSelected(True)
            self.set_status(f"Endurance: {len(files)} files found. Drag or use Up/Down to set concat order.")
            return
        for path in files:
            cycle = parse_cycle_from_name(path)
            if cycle is None:
                continue
            self.cycle_file_map[cycle] = path
        self.available_cycles = sorted(self.cycle_file_map)
        for cycle in self.available_cycles:
            item = QListWidgetItem(f"{cycle_label(cycle)}   ({self.cycle_file_map[cycle].name})")
            item.setData(Qt.UserRole, cycle)
            self.cycle_list.addItem(item)
            item.setSelected(True)
        self.set_status(f"{mode}: {len(self.available_cycles)} cycle files found")

    def select_all_cycles(self):
        for idx in range(self.cycle_list.count()):
            self.cycle_list.item(idx).setSelected(True)

    def select_all_metrics(self):
        for idx in range(self.metric_list.count()):
            self.metric_list.item(idx).setSelected(True)

    def selected_cycles(self):
        cycles = []
        for idx in range(self.cycle_list.count()):
            item = self.cycle_list.item(idx)
            if item.isSelected():
                cycles.append(item.data(Qt.UserRole))
        return cycles

    def selected_metrics(self):
        metrics = []
        if not hasattr(self, "metric_list"):
            return metrics
        for idx in range(self.metric_list.count()):
            item = self.metric_list.item(idx)
            if item.isSelected():
                metrics.append(item.data(Qt.UserRole))
        return metrics

    def selected_endurance_paths(self):
        paths = []
        for idx in range(self.cycle_list.count()):
            item = self.cycle_list.item(idx)
            if item.isSelected():
                paths.append(Path(item.data(Qt.UserRole)))
        return paths

    def move_selected_items(self, offset):
        rows = sorted({idx.row() for idx in self.cycle_list.selectedIndexes()})
        if not rows:
            return
        if offset < 0:
            iterator = rows
        else:
            iterator = reversed(rows)
        for row in iterator:
            new_row = row + offset
            if new_row < 0 or new_row >= self.cycle_list.count():
                continue
            item = self.cycle_list.takeItem(row)
            self.cycle_list.insertItem(new_row, item)
            item.setSelected(True)

    def new_figure(self):
        if self.canvas is not None:
            self.plot_layout.removeWidget(self.canvas)
            self.canvas.setParent(None)
        if self.toolbar is not None:
            self.plot_layout.removeWidget(self.toolbar)
            self.toolbar.setParent(None)

        self.fig = Figure(
            figsize=(self.fig_w_spin.value(), self.fig_h_spin.value()),
            dpi=self.dpi_spin.value(),
            facecolor="white",
        )
        self.ax = self.fig.add_subplot(111)
        self.axes = [self.ax]
        self.figure_title = None
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        if self.lock_size_check.isChecked():
            width = int(self.fig_w_spin.value() * self.dpi_spin.value())
            height = int(self.fig_h_spin.value() * self.dpi_spin.value())
            self.canvas.setFixedSize(width, height)
            self.canvas.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        else:
            self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.plot_layout.addWidget(self.toolbar)
        self.plot_layout.addWidget(self.canvas, 0, Qt.AlignCenter)

    def plot_current_mode(self):
        try:
            self.new_figure()
            mode = self.mode()
            if mode == "Endurance":
                self.plot_endurance()
            elif mode == "PUND":
                self.plot_pund()
            else:
                self.plot_pv()
            self.finish_plot()
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))
            self.set_status(f"Error: {exc}")

    def custom_colors(self):
        colors = [
            self.color_list.item(idx).data(Qt.UserRole)
            for idx in range(self.color_list.count())
        ]
        return colors or DEFAULT_COLORS

    def color_cycle(self, n):
        if self.color_mode_combo.currentText() == "Colormap":
            cmap = cm.get_cmap(self.cmap_combo.currentText())
            return [(cmap(i / max(n - 1, 1)), 1.0) for i in range(n)]
        colors = self.custom_colors()
        if self.color_mode_combo.currentText() == "Single + alpha":
            base = mcolors.to_rgb(colors[0])
            return [((*base, 1.0), 0.25 + 0.7 * i / max(n - 1, 1)) for i in range(n)]
        return [(colors[i % len(colors)], 1.0) for i in range(n)]

    def add_color(self):
        color = QColorDialog.getColor(QColor("#1f77b4"), self, "Choose color")
        if color.isValid():
            self.add_color_item(color.name())

    def remove_color(self):
        for item in self.color_list.selectedItems():
            self.color_list.takeItem(self.color_list.row(item))

    def reset_color_list(self):
        if not hasattr(self, "color_list"):
            return
        self.color_list.clear()
        for color in DEFAULT_COLORS:
            self.add_color_item(color)

    def add_color_item(self, color):
        item = QListWidgetItem(f"  {color}")
        item.setData(Qt.UserRole, color)
        item.setBackground(QColor(color))
        item.setForeground(QColor("black" if self.is_light_color(color) else "white"))
        self.color_list.addItem(item)

    def is_light_color(self, color):
        qcolor = QColor(color)
        return (0.299 * qcolor.red() + 0.587 * qcolor.green() + 0.114 * qcolor.blue()) > 150

    def optional_float(self, edit):
        text = edit.text().strip()
        if not text:
            return None
        return float(text)

    def apply_manual_limits(self):
        x_min = self.optional_float(self.x_min_edit)
        x_max = self.optional_float(self.x_max_edit)
        y_min = self.optional_float(self.y_min_edit)
        y_max = self.optional_float(self.y_max_edit)
        for ax in getattr(self, "axes", [self.ax]):
            if x_min is not None or x_max is not None:
                left, right = ax.get_xlim()
                left = x_min if x_min is not None else left
                right = x_max if x_max is not None else right
                if ax.get_xscale() == "log":
                    if left <= 0:
                        left = ax.get_xlim()[0]
                    if right <= 0:
                        right = ax.get_xlim()[1]
                if left < right:
                    ax.set_xlim(left, right)
            if y_min is not None or y_max is not None:
                bottom, top = ax.get_ylim()
                bottom = y_min if y_min is not None else bottom
                top = y_max if y_max is not None else top
                if bottom < top:
                    ax.set_ylim(bottom, top)

    def finish_plot(self):
        self.apply_manual_limits()
        if not self.show_title_check.isChecked():
            if self.figure_title is not None:
                self.figure_title.set_text("")
            for ax in getattr(self, "axes", [self.ax]):
                ax.set_title("")
        if not self.show_x_label_check.isChecked():
            for ax in getattr(self, "axes", [self.ax]):
                ax.set_xlabel("")
        if not self.show_y_label_check.isChecked():
            for ax in getattr(self, "axes", [self.ax]):
                ax.set_ylabel("")
        if self.figure_title is not None:
            self.figure_title.set_fontsize(self.title_size_spin.value())
            self.figure_title.set_fontweight("semibold")
        for ax in getattr(self, "axes", [self.ax]):
            if self.grid_check.isChecked():
                ax.grid(True, which="major", linestyle="-", linewidth=0.7, alpha=0.28)
                ax.grid(True, which="minor", linestyle=":", linewidth=0.5, alpha=0.18)
            else:
                ax.grid(False)
            ax.minorticks_on()
            ax.tick_params(direction="in", top=True, right=True, labelsize=self.tick_size_spin.value())
            ax.tick_params(which="minor", direction="in", top=True, right=True)
            ax.title.set_fontsize(self.title_size_spin.value())
            ax.title.set_fontweight("semibold")
            ax.xaxis.label.set_fontsize(self.axis_size_spin.value())
            ax.yaxis.label.set_fontsize(self.axis_size_spin.value())
            for side in ("top", "right", "left", "bottom"):
                ax.spines[side].set_visible(True)
                ax.spines[side].set_linewidth(1.0)
            if self.center_check.isChecked():
                ax.axhline(0, color="0.15", linewidth=0.9, linestyle=":", zorder=0.4)
                if ax.get_xscale() != "log":
                    ax.axvline(0, color="0.15", linewidth=0.9, linestyle=":", zorder=0.4)
            legend = ax.get_legend()
            if legend is not None:
                for text in legend.get_texts():
                    text.set_fontsize(self.legend_size_spin.value())
        if len(getattr(self, "axes", [self.ax])) > 1:
            self.fig.subplots_adjust(left=0.12, right=0.97, bottom=0.15, top=0.86, wspace=0.06)
        else:
            self.fig.tight_layout(rect=(0, 0, 1, 0.94) if self.figure_title is not None else None)
        self.canvas.draw_idle()

    def draw_break_marks(self, left_ax, right_ax):
        size = 0.018
        kwargs = dict(transform=left_ax.transAxes, color="0.05", clip_on=False, linewidth=1.4)
        left_ax.plot((1 - size, 1 + size), (-size, size), **kwargs)
        left_ax.plot((1 - size, 1 + size), (1 - size, 1 + size), **kwargs)
        kwargs.update(transform=right_ax.transAxes)
        right_ax.plot((-size, size), (size, -size), **kwargs)
        right_ax.plot((-size, size), (1 + size, 1 - size), **kwargs)

    def plot_endurance(self):
        infos = []
        paths = self.selected_endurance_paths()
        if not paths:
            paths = files_for_mode(self.folder_edit.text(), "Endurance")
        for order, path in enumerate(paths):
            try:
                data_df = read_excel_quiet(path, "Data")
                settings_df = read_excel_quiet(path, "Settings", header=None)
                infos.append((order, int(get_setting(settings_df, "max_loops")), path, data_df))
            except Exception:
                continue
        if not infos:
            raise ValueError("No readable Endurance files found.")
        blocks = []
        previous_end = 0
        break_idx = min(max(self.break_after_spin.value(), 1), max(len(infos) - 1, 1))
        broken_axis = self.endurance_axis_combo.currentText() == "Broken" and len(infos) > 1
        segment_end = 0
        for idx, (_, max_loops, path, data_df) in enumerate(infos):
            if broken_axis and idx == break_idx:
                segment_end = 0
            block = data_df.copy()
            block.insert(0, "file", path.name)
            block.insert(1, "global_cycle", previous_end + block["iteration"])
            block["plot_cycle"] = block["global_cycle"].clip(lower=1)
            block["axis_segment"] = 1 if broken_axis and idx >= break_idx else 0
            block["axis_cycle"] = (segment_end + block["iteration"]).clip(lower=1)
            block["Psw_uC_cm2"] = charge_to_polarization(block["Psw"], self.area_spin.value())
            block["Qsw_uC_cm2"] = charge_to_polarization(block["Qsw"], self.area_spin.value())
            blocks.append(block)
            previous_end += max_loops
            segment_end += max_loops
        df = pd.concat(blocks, ignore_index=True)
        self.current_df = df
        colors = self.color_cycle(2)
        if broken_axis:
            self.fig.clear()
            left_ax, right_ax = self.fig.subplots(
                1, 2, sharey=True, gridspec_kw={"width_ratios": [1, 1], "wspace": 0.06}
            )
            self.ax = left_ax
            self.axes = [left_ax, right_ax]
            self.figure_title = self.fig.suptitle("Combined Endurance")
            for axis_idx, ax in enumerate(self.axes):
                part = df[df["axis_segment"] == axis_idx]
                if part.empty:
                    continue
                ax.plot(
                    part["axis_cycle"], part["Psw_uC_cm2"], "o-", color=colors[0][0], alpha=colors[0][1],
                    linewidth=self.line_width_spin.value(), markersize=self.marker_size_spin.value(),
                    label="Psw" if axis_idx == 0 else None
                )
                ax.plot(
                    part["axis_cycle"], part["Qsw_uC_cm2"], "s-", color=colors[1][0], alpha=colors[1][1],
                    linewidth=self.line_width_spin.value(), markersize=self.marker_size_spin.value(),
                    label="Qsw" if axis_idx == 0 else None
                )
                ax.set_xscale("log")
                ax.set_xlabel("Cycle" if axis_idx == 0 else "Cycle after break")
            left_ax.set_ylabel(r"Switched polarization ($\mu\mathrm{C}/\mathrm{cm}^{2}$)")
            right_ax.tick_params(labelleft=False)
            left_ax.legend()
            self.draw_break_marks(left_ax, right_ax)
        else:
            self.ax.plot(
                df["plot_cycle"], df["Psw_uC_cm2"], "o-", color=colors[0][0], alpha=colors[0][1],
                linewidth=self.line_width_spin.value(), markersize=self.marker_size_spin.value(), label="Psw"
            )
            self.ax.plot(
                df["plot_cycle"], df["Qsw_uC_cm2"], "s-", color=colors[1][0], alpha=colors[1][1],
                linewidth=self.line_width_spin.value(), markersize=self.marker_size_spin.value(), label="Qsw"
            )
            self.ax.set_xscale("log")
            self.ax.set_xlabel("Cycle")
            self.ax.set_ylabel(r"Switched polarization ($\mu\mathrm{C}/\mathrm{cm}^{2}$)")
            self.ax.set_title("Combined Endurance")
            self.ax.legend()
        self.set_status(f"Endurance plotted: {len(df)} rows, final cycle {df['global_cycle'].max():g}")

    def calculate_pund_file(self, path):
        data_df = read_excel_quiet(path, "Data")
        settings_df = read_excel_quiet(path, "Settings", header=None)
        times = data_df["t"].values
        volts = data_df["V"].values
        currents = data_df["I"].values
        tp = get_setting(settings_df, "tp")
        td = get_setting(settings_df, "td")
        trf = get_setting(settings_df, "trf")
        pulse_duration = trf + tp + trf
        pulse_interval = pulse_duration + td
        starts = [td, td + pulse_interval, td + 2 * pulse_interval, td + 3 * pulse_interval]
        t_common = np.linspace(0, pulse_duration, 2000)

        def interp_pulse(start):
            mask = (times >= start) & (times <= start + pulse_duration)
            t_rel = times[mask] - start
            return (
                interp1d(t_rel, volts[mask], bounds_error=False, fill_value=0)(t_common),
                interp1d(t_rel, currents[mask], bounds_error=False, fill_value=0)(t_common),
            )

        v_p, i_p = interp_pulse(starts[0])
        _, i_u = interp_pulse(starts[1])
        v_n, i_n = interp_pulse(starts[2])
        _, i_d = interp_pulse(starts[3])
        q_pos = integrate.cumulative_trapezoid(i_p - i_u, t_common, initial=0)
        q_neg = integrate.cumulative_trapezoid(i_n - i_d, t_common, initial=0)
        p_pos = charge_to_polarization(q_pos, self.area_spin.value())
        p_neg = charge_to_polarization(q_neg, self.area_spin.value())
        p_pos -= (p_pos[0] + p_pos[-1]) / 2.0
        p_neg -= (p_neg[0] + p_neg[-1]) / 2.0
        return pd.DataFrame({"Voltage_Pos_V": v_p, "P_Pos_uC_cm2": p_pos, "Voltage_Neg_V": v_n, "P_Neg_uC_cm2": p_neg})

    def metric_row_from_loop(self, cycle, file_name, pos_voltage, pos_pol, neg_voltage, neg_pol):
        pos_x = pos_voltage if self.x_axis_combo.currentText() == "Voltage" else voltage_to_field(pos_voltage, self.thick_spin.value())
        neg_x = neg_voltage if self.x_axis_combo.currentText() == "Voltage" else voltage_to_field(neg_voltage, self.thick_spin.value())
        ec_pos_candidates = [value for value in self.crossing_values(pos_x, pos_pol, 0.0) if value > 0]
        ec_neg_candidates = [value for value in self.crossing_values(neg_x, neg_pol, 0.0) if value < 0]
        ec_pos = min(ec_pos_candidates, key=abs) if ec_pos_candidates else np.nan
        ec_neg = min(ec_neg_candidates, key=abs) if ec_neg_candidates else np.nan
        pos_v = np.asarray(pos_voltage, dtype=float)
        pos_p = np.asarray(pos_pol, dtype=float)
        neg_v = np.asarray(neg_voltage, dtype=float)
        neg_p = np.asarray(neg_pol, dtype=float)
        pos_start = int(np.nanargmax(pos_v)) if len(pos_v) else 0
        neg_start = int(np.nanargmin(neg_v)) if len(neg_v) else 0
        pr_pos = pos_p[pos_start + int(np.nanargmin(np.abs(pos_v[pos_start:])))] if len(pos_v[pos_start:]) else np.nan
        pr_neg = neg_p[neg_start + int(np.nanargmin(np.abs(neg_v[neg_start:])))] if len(neg_v[neg_start:]) else np.nan
        return {
            "cycle": cycle,
            "file": file_name,
            "Ec+": ec_pos,
            "Ec-": ec_neg,
            "|Ec+|": abs(ec_pos) if np.isfinite(ec_pos) else np.nan,
            "|Ec-|": abs(ec_neg) if np.isfinite(ec_neg) else np.nan,
            "|Ec+|-|Ec-|": abs(ec_pos) - abs(ec_neg) if np.isfinite([ec_pos, ec_neg]).all() else np.nan,
            "2Ec": abs(ec_pos) + abs(ec_neg) if np.isfinite([ec_pos, ec_neg]).all() else np.nan,
            "Pr+": pr_pos,
            "Pr-": pr_neg,
            "|Pr+|": abs(pr_pos) if np.isfinite(pr_pos) else np.nan,
            "|Pr-|": abs(pr_neg) if np.isfinite(pr_neg) else np.nan,
            "|Pr+|-|Pr-|": abs(pr_pos) - abs(pr_neg) if np.isfinite([pr_pos, pr_neg]).all() else np.nan,
            "2Pr": pr_pos - pr_neg if np.isfinite([pr_pos, pr_neg]).all() else np.nan,
        }

    def plot_metric_summary(self, summary, source_name):
        if self.pv_plot_type_combo.currentText() == "Ec vs cycle":
            all_metrics = ["Ec+", "Ec-", "|Ec+|", "|Ec-|", "|Ec+|-|Ec-|", "2Ec"]
            ylabel = "Coercive voltage (V)" if self.x_axis_combo.currentText() == "Voltage" else "Coercive field (MV/cm)"
            title = f"{source_name} Ec vs Cycle"
        else:
            all_metrics = ["Pr+", "Pr-", "|Pr+|", "|Pr-|", "|Pr+|-|Pr-|", "2Pr"]
            ylabel = r"Remanent polarization ($\mu\mathrm{C}/\mathrm{cm}^{2}$)"
            title = f"{source_name} Pr vs Cycle"
        metrics = [metric for metric in self.selected_metrics() if metric in all_metrics]
        if not metrics:
            raise ValueError(f"Select at least one {source_name} metric.")
        colors = self.color_cycle(len(metrics))
        for idx, metric in enumerate(metrics):
            color, alpha = colors[idx]
            self.ax.plot(
                summary["cycle"], summary[metric], "o-",
                color=color, alpha=alpha,
                linewidth=self.line_width_spin.value(),
                markersize=self.marker_size_spin.value(),
                label=metric,
            )
        self.ax.set_xscale("log")
        self.ax.set_xlabel("Cycle")
        self.ax.set_ylabel(ylabel)
        self.ax.set_title(title)
        self.ax.legend()
        self.set_status(f"{title}: {len(summary)} cycles, {len(metrics)} metrics")

    def plot_pund_metrics(self, cycles):
        rows = []
        for cycle in cycles:
            path = self.cycle_file_map[cycle]
            try:
                df = self.calculate_pund_file(path)
                rows.append(self.metric_row_from_loop(
                    cycle, path.name,
                    df["Voltage_Pos_V"], df["P_Pos_uC_cm2"],
                    df["Voltage_Neg_V"], df["P_Neg_uC_cm2"],
                ))
            except Exception:
                continue
        if not rows:
            raise ValueError("No readable PUND metric files found.")
        summary = pd.DataFrame(rows).sort_values("cycle")
        self.current_df = summary
        self.plot_metric_summary(summary, "PUND")

    def plot_pund(self):
        cycles = self.selected_cycles()
        if not cycles:
            raise ValueError("Select at least one PUND cycle.")
        if self.pv_plot_type_combo.currentText() != "Loop":
            self.plot_pund_metrics(cycles)
            return
        colors = self.color_cycle(len(cycles))
        frames = []
        for idx, cycle in enumerate(cycles):
            color, alpha = colors[idx]
            path = self.cycle_file_map[cycle]
            df = self.calculate_pund_file(path)
            df.insert(0, "cycle", cycle)
            df.insert(1, "file", path.name)
            frames.append(df)
            x_pos = df["Voltage_Pos_V"] if self.x_axis_combo.currentText() == "Voltage" else voltage_to_field(df["Voltage_Pos_V"], self.thick_spin.value())
            x_neg = df["Voltage_Neg_V"] if self.x_axis_combo.currentText() == "Voltage" else voltage_to_field(df["Voltage_Neg_V"], self.thick_spin.value())
            self.ax.plot(x_pos, df["P_Pos_uC_cm2"], color=color, alpha=alpha, linewidth=self.line_width_spin.value(), label=cycle_label(cycle))
            self.ax.plot(x_neg, df["P_Neg_uC_cm2"], color=color, alpha=alpha, linewidth=self.line_width_spin.value())
        self.current_df = pd.concat(frames, ignore_index=True)
        self.ax.set_xlabel("Voltage (V)" if self.x_axis_combo.currentText() == "Voltage" else "Electric field (MV/cm)")
        self.ax.set_ylabel(r"Polarization ($\mu\mathrm{C}/\mathrm{cm}^{2}$)")
        self.ax.set_title("PUND PV Loops")
        self.ax.legend()
        self.set_status(f"PUND plotted: {len(cycles)} cycles")

    def pv_offset(self, voltage, polarization):
        minus_pr = polarization.iloc[0]
        plus_pr = None
        threshold = 0
        for idx in range(len(voltage)):
            if voltage.iloc[idx] > 0.2:
                threshold = idx
                break
        for idx in range(threshold, len(voltage)):
            if voltage.iloc[idx] < 0:
                plus_pr = polarization.iloc[idx]
                break
        return polarization.median() if plus_pr is None else (minus_pr + plus_pr) / 2.0

    def crossing_values(self, x_data, y_data, target):
        x = np.asarray(x_data, dtype=float)
        y = np.asarray(y_data, dtype=float)
        values = []
        for idx in range(len(x) - 1):
            x0, x1 = x[idx], x[idx + 1]
            y0, y1 = y[idx], y[idx + 1]
            if not np.isfinite([x0, x1, y0, y1]).all():
                continue
            d0 = y0 - target
            d1 = y1 - target
            if d0 == 0:
                values.append(x0)
            elif d0 * d1 < 0 and y1 != y0:
                values.append(x0 + (target - y0) * (x1 - x0) / (y1 - y0))
        return values

    def interpolate_at_x(self, x_data, y_data, target):
        x = np.asarray(x_data, dtype=float)
        y = np.asarray(y_data, dtype=float)
        values = []
        for idx in range(len(x) - 1):
            x0, x1 = x[idx], x[idx + 1]
            y0, y1 = y[idx], y[idx + 1]
            if not np.isfinite([x0, x1, y0, y1]).all():
                continue
            d0 = x0 - target
            d1 = x1 - target
            if d0 == 0:
                values.append(y0)
            elif d0 * d1 < 0 and x1 != x0:
                values.append(y0 + (target - x0) * (y1 - y0) / (x1 - x0))
        return values

    def calculate_pv_metric_file(self, path, cycle):
        df = read_excel_quiet(path, "Data")
        if not {"Vforce", "Charge"}.issubset(df.columns):
            raise ValueError(f"Missing PV columns: {path.name}")
        voltage = df["Vforce"]
        polarization = charge_to_polarization(df["Charge"], self.area_spin.value())
        polarization -= self.pv_offset(voltage, polarization)
        ec_axis = voltage if self.x_axis_combo.currentText() == "Voltage" else voltage_to_field(voltage, self.thick_spin.value())
        ec_crossings = self.crossing_values(ec_axis, polarization, 0.0)
        ec_pos_values = [value for value in ec_crossings if value > 0]
        ec_neg_values = [value for value in ec_crossings if value < 0]
        ec_pos = min(ec_pos_values, key=abs) if ec_pos_values else np.nan
        ec_neg = min(ec_neg_values, key=abs) if ec_neg_values else np.nan
        pr_crossings = self.interpolate_at_x(voltage, polarization, 0.0)
        pr_pos_values = [value for value in pr_crossings if value > 0]
        pr_neg_values = [value for value in pr_crossings if value < 0]
        pr_pos = max(pr_pos_values) if pr_pos_values else np.nan
        pr_neg = min(pr_neg_values) if pr_neg_values else np.nan
        return {
            "cycle": cycle,
            "file": path.name,
            "Ec+": ec_pos,
            "Ec-": ec_neg,
            "|Ec+|": abs(ec_pos) if np.isfinite(ec_pos) else np.nan,
            "|Ec-|": abs(ec_neg) if np.isfinite(ec_neg) else np.nan,
            "|Ec+|-|Ec-|": abs(ec_pos) - abs(ec_neg) if np.isfinite([ec_pos, ec_neg]).all() else np.nan,
            "2Ec": abs(ec_pos) + abs(ec_neg) if np.isfinite([ec_pos, ec_neg]).all() else np.nan,
            "Pr+": pr_pos,
            "Pr-": pr_neg,
            "|Pr+|": abs(pr_pos) if np.isfinite(pr_pos) else np.nan,
            "|Pr-|": abs(pr_neg) if np.isfinite(pr_neg) else np.nan,
            "|Pr+|-|Pr-|": abs(pr_pos) - abs(pr_neg) if np.isfinite([pr_pos, pr_neg]).all() else np.nan,
            "2Pr": pr_pos - pr_neg if np.isfinite([pr_pos, pr_neg]).all() else np.nan,
        }

    def plot_pv_metrics(self, cycles):
        rows = []
        for cycle in cycles:
            path = self.cycle_file_map[cycle]
            try:
                rows.append(self.calculate_pv_metric_file(path, cycle))
            except Exception:
                continue
        if not rows:
            raise ValueError("No readable PV metric files found.")
        summary = pd.DataFrame(rows).sort_values("cycle")
        self.current_df = summary
        self.plot_metric_summary(summary, "PV")

    def plot_pv(self):
        cycles = self.selected_cycles()
        if not cycles:
            raise ValueError("Select at least one PV cycle.")
        if self.pv_plot_type_combo.currentText() != "Loop":
            self.plot_pv_metrics(cycles)
            return
        colors = self.color_cycle(len(cycles))
        frames = []
        for idx, cycle in enumerate(cycles):
            color, alpha = colors[idx]
            path = self.cycle_file_map[cycle]
            df = read_excel_quiet(path, "Data")
            needed = {"Vforce", "Charge"}
            if self.pv_y_combo.currentText() == "Current density":
                needed.add("Imeas")
            if not needed.issubset(df.columns):
                continue
            voltage = df["Vforce"]
            x_data = voltage if self.x_axis_combo.currentText() == "Voltage" else voltage_to_field(voltage, self.thick_spin.value())
            polarization = charge_to_polarization(df["Charge"], self.area_spin.value())
            polarization -= self.pv_offset(voltage, polarization)
            current_density = current_to_density(df["Imeas"], self.area_spin.value()) if "Imeas" in df.columns else pd.Series(np.nan, index=df.index)
            y_data = current_density if self.pv_y_combo.currentText() == "Current density" else polarization
            frames.append(pd.DataFrame({
                "cycle": cycle, "file": path.name, "Voltage_V": voltage,
                "Field_MV_cm": voltage_to_field(voltage, self.thick_spin.value()),
                "P_uC_cm2": polarization, "J_A_cm2": current_density,
            }))
            self.ax.plot(x_data, y_data, color=color, alpha=alpha, linewidth=self.line_width_spin.value(), label=cycle_label(cycle))
        if not frames:
            raise ValueError("No readable PV files found.")
        self.current_df = pd.concat(frames, ignore_index=True)
        self.ax.set_xlabel("Voltage (V)" if self.x_axis_combo.currentText() == "Voltage" else "Electric field (MV/cm)")
        if self.pv_y_combo.currentText() == "Current density":
            self.ax.set_ylabel(r"Current density ($\mathrm{A}/\mathrm{cm}^{2}$)")
            self.ax.set_title("PV Current Density Loops")
        else:
            self.ax.set_ylabel(r"Polarization ($\mu\mathrm{C}/\mathrm{cm}^{2}$)")
            self.ax.set_title("PV Loops")
        self.ax.legend()
        self.set_status(f"PV plotted: {len(frames)} cycles")

    def save_csv(self):
        if self.current_df is None:
            QMessageBox.information(self, "Save CSV", "Plot data first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", f"{self.mode()}_result.csv", "CSV files (*.csv)")
        if path:
            self.current_df.to_csv(path, index=False)
            self.set_status(f"Saved CSV: {path}")

    def save_excel(self):
        if self.current_df is None:
            QMessageBox.information(self, "Save Excel", "Plot data first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Excel", f"{self.mode()}_result.xlsx", "Excel files (*.xlsx)")
        if path:
            self.current_df.to_excel(path, index=False)
            self.set_status(f"Saved Excel: {path}")

    def save_figure(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Figure", f"{self.mode()}_plot.png", "PNG files (*.png);;PDF files (*.pdf);;SVG files (*.svg)"
        )
        if path:
            self.fig.savefig(path, dpi=self.dpi_spin.value())
            self.set_status(f"Saved figure: {path}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = FerroCycleQtApp()
    window.show()
    sys.exit(app.exec())
