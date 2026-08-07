"""PySide6 桌面 GUI —— 盘中实时模拟盘主界面。

基于 PaperTradingEngine 的数据查询接口构建：
  - 顶部账户概览卡片（净值/现金/市值/盈亏/回撤/持仓数）
  - 标签页：当前持仓 / 交易记录 / 净值曲线 / 信号日志
  - 工具栏：选择股票 + 回放演示 + 刷新（实时模式阶段2接入后自动刷新）

回放在 QThread 中执行，避免阻塞 UI；完成后通过信号回到主线程刷新。
"""
from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
from typing import Optional

from PySide6.QtCore import QUrl, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QInputDialog, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

logger = logging.getLogger(__name__)

# A股惯例：红涨绿跌
RED = "#d8413c"
GREEN = "#21a675"
GRAY = "#888"


class StrategyConfigDialog(QDialog):
    """Desktop strategy settings editor backed by config/config.yaml."""

    TIMING_OPTIONS = [
        ("BollingerStrategy", "bollinger"),
        ("RSIStrategy", "rsi"),
        ("TurtleStrategy", "turtle"),
        ("SupportStrategy", "support"),
        ("ShunShiBaoStrategy", "macd_bollinger"),
    ]

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("策略配置")
        self.setMinimumWidth(500)

        paper = engine.config
        root = QVBoxLayout(self)
        form = QFormLayout()

        self.timing_combo = QComboBox()
        for label, value in self.TIMING_OPTIONS:
            self.timing_combo.addItem(label, value)
        current_timing = getattr(paper, "timing_strategy", "bollinger")
        idx = self.timing_combo.findData(current_timing)
        self.timing_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("择时策略", self.timing_combo)

        self.max_position = self._ratio_box(getattr(paper, "max_position_pct", 0.2))
        self.max_total_position = self._ratio_box(getattr(paper, "max_total_position_pct", 0.8))
        form.addRow("单票最大仓位", self.max_position)
        form.addRow("总仓位上限", self.max_total_position)

        self.max_hold_days = QSpinBox()
        self.max_hold_days.setRange(0, 365)
        self.max_hold_days.setValue(int(getattr(paper, "max_hold_days", 0)))
        form.addRow("最大持仓天数", self.max_hold_days)

        self.take_profit = self._ratio_box(getattr(paper, "take_profit", 0.0))
        self.stop_loss = self._ratio_box(getattr(paper, "stop_loss", 0.0))
        form.addRow("止盈比例", self.take_profit)
        form.addRow("止损比例", self.stop_loss)
        root.addLayout(form)

        root.addWidget(QLabel("参与候选池的选股策略"))
        self.all_strategies = QCheckBox("使用全部已注册策略")
        configured = [str(v) for v in (getattr(paper, "selection_strategies", []) or [])]
        self.all_strategies.setChecked(not configured)
        root.addWidget(self.all_strategies)

        self.strategy_list = QListWidget()
        self.strategy_list.setEnabled(bool(configured))
        try:
            from strategy.strategy_registry import get_registry
            registry = get_registry("config/strategy_params.yaml")
            registry.auto_register_from_directory("strategy")
            strategies = registry.strategies
        except Exception:
            strategies = {}
        for class_name, strategy in strategies.items():
            item = QListWidgetItem(getattr(strategy, "name", class_name))
            item.setData(Qt.ItemDataRole.UserRole, class_name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if class_name in configured
                or getattr(strategy, "name", "") in configured
                else Qt.CheckState.Unchecked
            )
            self.strategy_list.addItem(item)
        self.all_strategies.toggled.connect(self.strategy_list.setDisabled)
        root.addWidget(self.strategy_list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _ratio_box(value):
        box = QDoubleSpinBox()
        box.setRange(0.0, 1.0)
        box.setSingleStep(0.01)
        box.setDecimals(4)
        box.setValue(float(value or 0.0))
        return box

    def _selected_strategies(self):
        if self.all_strategies.isChecked():
            return []
        selected = []
        for index in range(self.strategy_list.count()):
            item = self.strategy_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return selected

    def _save(self):
        try:
            self.engine.update_strategy_config(
                timing_strategy=str(self.timing_combo.currentData()),
                selection_strategies=self._selected_strategies(),
                max_position_pct=self.max_position.value(),
                max_total_position_pct=self.max_total_position.value(),
                max_hold_days=self.max_hold_days.value(),
                take_profit=self.take_profit.value(),
                stop_loss=self.stop_loss.value(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()


class ReplayWorker(QThread):
    """后台执行历史回放，避免阻塞主线程。"""
    done = Signal(dict)

    def __init__(self, engine, code: Optional[str]):
        super().__init__()
        self.engine = engine
        self.code = code

    def run(self):
        try:
            r = self.engine.replay_history(self.code)
            self.done.emit(r)
        except Exception as e:
            logger.exception("回放失败")
            self.done.emit({"error": str(e)})


class CodeScreenWorker(QThread):
    done = Signal(dict)
    progress = Signal(str)

    def __init__(self, engine, source: str, max_stocks: Optional[int], force_refresh: bool = False):
        super().__init__()
        self.engine = engine
        self.source = source
        self.max_stocks = max_stocks
        self.force_refresh = force_refresh

    def run(self):
        try:
            self.done.emit(self.engine.screen_with_user_code(
                self.source,
                self.max_stocks,
                progress_callback=self.progress.emit,
                force_refresh=self.force_refresh,
            ))
        except Exception as exc:
            self.done.emit({"error": str(exc)})


class CodeScreenDialog(QDialog):
    """Run a local user matcher and show the matching A-share rows."""

    TEMPLATE = '''def match_stock(df, code, name):
    """返回 True 表示命中；也可以返回 {"matched": True, "reason": "..."}."""
    if len(df) < 60:
        return False

    # 数据默认最新一条在第 0 行；这里改成升序便于计算均线
    work = df.sort_values("date").copy()
    work["ma5"] = work["close"].rolling(5).mean()
    work["ma20"] = work["close"].rolling(20).mean()
    latest = work.iloc[-1]
    previous = work.iloc[-2]

    crossed = latest["ma5"] > latest["ma20"] and previous["ma5"] <= previous["ma20"]
    if crossed:
        return {"matched": True, "reason": "MA5 上穿 MA20", "price": float(latest["close"])}
    return False
'''

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.worker: Optional[CodeScreenWorker] = None
        self.results = []
        self.setWindowTitle("代码筛选")
        self.resize(920, 720)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("可填写 Python 的 match_stock(df, code, name)，也可直接填写中文条件，例如：非ST股、股价<50、近1月2次涨停。这里只做筛选，不会下单。"))
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(self.TEMPLATE)
        self.editor.setMinimumHeight(300)
        root.addWidget(self.editor)

        options = QHBoxLayout()
        options.addWidget(QLabel("扫描股票数"))
        self.max_stocks = QSpinBox()
        self.max_stocks.setRange(0, 100000)
        self.max_stocks.setValue(0)
        self.max_stocks.setSpecialValueText("全部")
        options.addWidget(self.max_stocks)

        options.addWidget(QLabel("已保存策略"))
        self.saved_strategy_combo = QComboBox()
        self.saved_strategy_combo.setMinimumWidth(180)
        self.saved_strategy_combo.addItem("选择策略", None)
        self.saved_strategy_combo.currentIndexChanged.connect(
            self._on_saved_strategy_changed
        )
        options.addWidget(self.saved_strategy_combo)
        self.btn_load_strategy = QPushButton("加载策略")
        self.btn_load_strategy.setEnabled(False)
        self.btn_load_strategy.clicked.connect(self._load_saved_strategy)
        options.addWidget(self.btn_load_strategy)
        self.btn_save_strategy = QPushButton("保存为策略")
        self.btn_save_strategy.clicked.connect(self._save_strategy)
        options.addWidget(self.btn_save_strategy)
        self.btn_delete_strategy = QPushButton("删除策略")
        self.btn_delete_strategy.setEnabled(False)
        self.btn_delete_strategy.clicked.connect(self._delete_strategy)
        options.addWidget(self.btn_delete_strategy)

        self.btn_run = QPushButton("运行筛选")
        self.btn_run.clicked.connect(self._run_screen)
        options.addWidget(self.btn_run)
        self.btn_refresh_run = QPushButton("刷新行情并筛选")
        self.btn_refresh_run.clicked.connect(lambda: self._run_screen(True))
        options.addWidget(self.btn_refresh_run)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export)
        options.addWidget(self.btn_export)
        self.btn_add_watchlist = QPushButton("加入自选")
        self.btn_add_watchlist.setEnabled(False)
        self.btn_add_watchlist.clicked.connect(self._add_to_watchlist)
        options.addWidget(self.btn_add_watchlist)
        self.status = QLabel("")
        options.addWidget(self.status, 1)
        root.addLayout(options)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["选择", "代码", "名称", "价格", "命中原因"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 58)
        self.table.setColumnWidth(1, 92)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 92)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        root.addWidget(self.table, 1)

        self._refresh_saved_strategies()

    def _refresh_saved_strategies(self, selected_name: Optional[str] = None):
        current_name = selected_name
        if current_name is None:
            current_name = self.saved_strategy_combo.currentData()
        self.saved_strategy_combo.blockSignals(True)
        self.saved_strategy_combo.clear()
        self.saved_strategy_combo.addItem("选择策略", None)
        for item in self.engine.list_user_screen_strategies():
            name = str(item.get("name", "")).strip()
            if name:
                self.saved_strategy_combo.addItem(name, name)
        if current_name:
            index = self.saved_strategy_combo.findData(current_name)
            self.saved_strategy_combo.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.saved_strategy_combo.setCurrentIndex(0)
        self.saved_strategy_combo.blockSignals(False)
        self._update_saved_strategy_buttons()

    def _update_saved_strategy_buttons(self):
        has_selection = bool(self.saved_strategy_combo.currentData())
        self.btn_load_strategy.setEnabled(has_selection)
        self.btn_delete_strategy.setEnabled(has_selection)

    def _on_saved_strategy_changed(self):
        """Keep the editor synchronized with the strategy selected in the combo."""
        self._update_saved_strategy_buttons()
        name = self.saved_strategy_combo.currentData()
        if not name:
            return
        try:
            record = self.engine.load_user_screen_strategy(str(name))
            self.editor.setPlainText(str(record.get("source", "")))
            self.max_stocks.setValue(int(record.get("max_stocks", 0) or 0))
            self.status.setText(f"已自动加载策略：{name}")
        except Exception as exc:
            self.status.setText(f"策略加载失败：{exc}")

    def _save_strategy(self):
        name, ok = QInputDialog.getText(self, "保存策略", "策略名称")
        name = str(name or "").strip()
        if not ok or not name:
            return
        try:
            self.engine.save_user_screen_strategy(
                name,
                self.editor.toPlainText(),
                self.max_stocks.value() or 0,
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存策略失败", str(exc))
            return
        self._refresh_saved_strategies(name)
        self.status.setText(f"策略已保存：{name}")

    def _load_saved_strategy(self):
        name = self.saved_strategy_combo.currentData()
        if not name:
            return
        try:
            record = self.engine.load_user_screen_strategy(str(name))
            self.editor.setPlainText(str(record.get("source", "")))
            self.max_stocks.setValue(int(record.get("max_stocks", 0) or 0))
        except Exception as exc:
            QMessageBox.warning(self, "加载策略失败", str(exc))
            return
        self.status.setText(f"已加载策略：{name}")

    def _delete_strategy(self):
        name = self.saved_strategy_combo.currentData()
        if not name:
            return
        answer = QMessageBox.question(
            self,
            "删除策略",
            f"确定删除策略“{name}”吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.engine.delete_user_screen_strategy(str(name))
        except Exception as exc:
            QMessageBox.warning(self, "删除策略失败", str(exc))
            return
        self._refresh_saved_strategies()
        self.status.setText(f"策略已删除：{name}")

    def _run_screen(self, force_refresh: bool = False):
        if self.worker and self.worker.isRunning():
            return
        self.btn_run.setEnabled(False)
        self.btn_refresh_run.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.status.setText("正在扫描行情数据...")
        self.worker = CodeScreenWorker(
            self.engine,
            self.editor.toPlainText(),
            self.max_stocks.value() or None,
            force_refresh,
        )
        self.worker.progress.connect(self.status.setText)
        self.worker.done.connect(self._screen_done)
        self.worker.start()

    def _screen_done(self, payload):
        self.btn_run.setEnabled(True)
        self.btn_refresh_run.setEnabled(True)
        if payload.get("error"):
            self.status.setText("执行失败")
            QMessageBox.warning(self, "代码筛选失败", payload["error"])
            return
        self.results = payload.get("results", [])
        self.table.setRowCount(0)
        for row in self.results:
            index = self.table.rowCount()
            self.table.insertRow(index)
            checked = QTableWidgetItem()
            checked.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            checked.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(index, 0, checked)
            values = [row.get("code", ""), row.get("name", ""),
                      row.get("price", ""), row.get("reason", "")]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value))
                if column == 4:
                    # 保留完整原因，悬停时也可查看不换行的原文。
                    item.setToolTip(str(value))
                self.table.setItem(index, column, item)
        self.btn_add_watchlist.setEnabled(bool(self.results))
        self.btn_export.setEnabled(bool(self.results))
        self.status.setText(
            f"处理 {payload.get('processed', 0)} 只，命中 {len(self.results)} 只，"
            f"错误 {len(payload.get('errors', []))} 条"
        )

    def _add_to_watchlist(self):
        selected = []
        for row_index, row in enumerate(self.results):
            checkbox = self.table.item(row_index, 0)
            if checkbox and checkbox.checkState() == Qt.CheckState.Checked:
                selected.append(row)
        if not selected:
            QMessageBox.information(self, "加入自选", "请至少选择一只股票。")
            return

        group_names = [str(item.get("name", "")).strip()
                       for item in self.engine.list_watchlist_groups()]
        choices = group_names + ["新建分组..."]
        group, ok = QInputDialog.getItem(
            self, "加入自选", "选择自选分组", choices, 0, False
        )
        if not ok:
            return
        if group == "新建分组...":
            group, ok = QInputDialog.getText(self, "新建自选分组", "分组名称")
            group = str(group or "").strip()
            if not ok or not group:
                return
        try:
            self.engine.add_to_watchlist(group, selected)
        except Exception as exc:
            QMessageBox.warning(self, "加入自选失败", str(exc))
            return
        self.status.setText(f"已将 {len(selected)} 只股票加入自选分组：{group}")

    def _export(self):
        try:
            path = self.engine.save_custom_screen_results(self.results)
            self.status.setText(f"已导出：{path}")
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))


class WatchlistDialog(QDialog):
    """View persisted watchlist groups and their screened stocks."""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("自选股分组")
        self.resize(820, 520)

        root = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("分组"))
        self.group_combo = QComboBox()
        self.group_combo.currentIndexChanged.connect(self._refresh_items)
        toolbar.addWidget(self.group_combo, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_groups)
        toolbar.addWidget(refresh)
        root.addLayout(toolbar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["代码", "名称", "价格", "命中原因"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 100)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 100)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        root.addWidget(self.table, 1)
        self._refresh_groups()

    def _refresh_groups(self):
        current = self.group_combo.currentData()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        for group in self.engine.list_watchlist_groups():
            name = str(group.get("name", "")).strip()
            if name:
                self.group_combo.addItem(
                    f"{name}（{group.get('item_count', 0)}只）", name
                )
        if current:
            index = self.group_combo.findData(current)
            self.group_combo.setCurrentIndex(index if index >= 0 else 0)
        self.group_combo.blockSignals(False)
        self._refresh_items()

    def _refresh_items(self):
        group = self.group_combo.currentData()
        self.table.setRowCount(0)
        if not group:
            return
        for row in self.engine.get_watchlist_items(str(group)):
            index = self.table.rowCount()
            self.table.insertRow(index)
            values = [row.get("code", ""), row.get("name", ""),
                      row.get("price", ""), row.get("reason", "")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 3:
                    item.setToolTip(str(value))
                self.table.setItem(index, column, item)

class NavChartWidget(QWidget):
    """净值曲线（matplotlib 嵌入 QtAgg）。"""
    def __init__(self):
        super().__init__()
        import matplotlib
        matplotlib.use("QtAgg")
        matplotlib.rcParams["font.sans-serif"] = [
            "Microsoft YaHei", "SimHei", "PingFang SC", "Heiti SC", "STHeiti",
            "Arial Unicode MS", "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure
        self.fig = Figure(figsize=(7, 3), tight_layout=True)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvas(self.fig)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        self._draw_empty()

    def _draw_empty(self):
        self.ax.clear()
        self.ax.set_title("净值曲线（无数据，请先回放或等待实时模式）")
        self.ax.grid(True, alpha=0.3)
        self.canvas.draw()

    def plot(self, nav_series):
        self.ax.clear()
        if not nav_series:
            self._draw_empty()
            return
        dates = [r["trade_date"] for r in nav_series]
        navs = [r["nav"] for r in nav_series]
        self.ax.plot(range(len(navs)), navs, color=RED, linewidth=1.4)
        self.ax.axhline(1.0, color=GRAY, linestyle="--", linewidth=0.8)
        self.ax.set_title(f"净值曲线（{dates[0]} ~ {dates[-1]}，共 {len(navs)} 日）")
        self.ax.set_ylabel("单位净值")
        self.ax.grid(True, alpha=0.3)
        step = max(1, len(dates) // 8)
        self.ax.set_xticks(range(0, len(dates), step))
        self.ax.set_xticklabels([dates[i][5:] for i in range(0, len(dates), step)],
                                rotation=30, ha="right")
        self.canvas.draw()


class PaperTradingWindow(QMainWindow):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.setWindowTitle("KHunter 盘中实时模拟盘")
        self.resize(1180, 760)
        self._worker: Optional[ReplayWorker] = None
        self._build_ui()
        self._refresh_stock_combo()
        self.refresh_all()

        # 周期刷新（实时模式生效；回放后也能持续刷新）
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(5000)

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 工具栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("回放股票:"))
        self.stock_combo = QComboBox()
        self.stock_combo.setMinimumWidth(260)
        bar.addWidget(self.stock_combo)
        self.btn_replay = QPushButton("▶ 历史回放演示")
        self.btn_replay.clicked.connect(self.on_replay)
        bar.addWidget(self.btn_replay)
        self.btn_refresh = QPushButton("⟳ 刷新")
        self.btn_refresh.clicked.connect(self.refresh_all)
        bar.addWidget(self.btn_refresh)
        self.btn_strategy = QPushButton("策略配置")
        self.btn_strategy.clicked.connect(self.on_strategy_config)
        bar.addWidget(self.btn_strategy)
        self.btn_code_screen = QPushButton("代码筛选")
        self.btn_code_screen.clicked.connect(self.on_code_screen)
        bar.addWidget(self.btn_code_screen)
        self.btn_watchlist = QPushButton("自选分组")
        self.btn_watchlist.clicked.connect(self.on_watchlist)
        bar.addWidget(self.btn_watchlist)
        self.btn_realtime = QPushButton("🔴 启动盘中实时")
        self.btn_realtime.clicked.connect(self.on_realtime)
        bar.addWidget(self.btn_realtime)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color:{GRAY};")
        bar.addWidget(self.lbl_status, 1)
        root.addLayout(bar)

        # 账户概览卡片
        card = QGridLayout()
        card.setContentsMargins(8, 8, 8, 8)
        self.acc_labels = {}
        for (key, title) in [
            ("total_value", "总净值"), ("cash", "可用现金"),
            ("positions_value", "持仓市值"), ("pnl", "浮动盈亏"),
            ("pnl_pct", "盈亏比例"), ("drawdown_pct", "最大回撤"),
            ("position_count", "持仓只数"), ("timing_strategy", "择时策略"),
        ]:
            self.acc_labels[key] = (QLabel(title), QLabel("-"))
        col = 0
        for key, (title_lbl, val_lbl) in self.acc_labels.items():
            title_lbl.setStyleSheet(f"color:{GRAY}; font-size:11px;")
            val_lbl.setStyleSheet("font-size:17px; font-weight:bold;")
            wrap = QVBoxLayout()
            wrap.setSpacing(0)
            wrap.addWidget(title_lbl)
            wrap.addWidget(val_lbl)
            cell = QWidget()
            cell.setLayout(wrap)
            card.addWidget(cell, 0, col)
            col += 1
        card_box = QWidget()
        card_box.setStyleSheet("background:#fafafa; border:1px solid #e0e0e0; border-radius:6px;")
        card_box.setLayout(card)
        root.addWidget(card_box)

        # 标签页
        tabs = QTabWidget()
        self.tbl_positions = self._make_table(
            ["代码", "名称", "持仓", "可卖", "成本", "现价", "市值", "浮盈%"])
        self.tbl_trades = self._make_table(
            ["日期", "时间", "代码", "名称", "方向", "数量", "价格", "金额",
             "佣金", "印花", "状态", "原因"])
        self.tbl_signals = self._make_table(
            ["时间", "代码", "方向", "数量", "强度", "来源", "成交", "说明"])
        self.nav_chart = NavChartWidget()
        tabs.addTab(self.tbl_positions, "当前持仓")
        tabs.addTab(self.tbl_trades, "交易记录")
        tabs.addTab(self.nav_chart, "净值曲线")
        tabs.addTab(self.tbl_signals, "信号日志")
        root.addWidget(tabs, 1)

    def _make_table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        return t

    def _refresh_stock_combo(self):
        self.stock_combo.clear()
        try:
            codes = self.engine.db_manager.list_all_stocks()
        except Exception:
            codes = []
        names = (self.engine.db_manager.get_all_stock_names()
                 if hasattr(self.engine.db_manager, "get_all_stock_names") else {})
        for code in codes[:200]:
            self.stock_combo.addItem(f"{code} {names.get(code, '')}", code)

    # ---------------- 刷新 ----------------
    def refresh_all(self):
        if self._worker and self._worker.isRunning():
            return
        try:
            self._refresh_account()
            self._refresh_positions()
            self._refresh_trades()
            self._refresh_signals()
            self._refresh_nav()
        except Exception:
            logger.exception("刷新失败")

    def _refresh_account(self):
        acc = self.engine.account_overview()
        for key, (_, val_lbl) in self.acc_labels.items():
            v = acc.get(key, "-")
            if isinstance(v, float):
                text = f"{v:,.2f}" if key in ("total_value", "cash", "positions_value", "pnl") else f"{v}"
            else:
                text = str(v)
            val_lbl.setText(text)
        # 盈亏上色
        pnl = acc.get("pnl", 0)
        color = RED if pnl > 0 else (GREEN if pnl < 0 else GRAY)
        for key in ("pnl", "pnl_pct"):
            self.acc_labels[key][1].setStyleSheet(
                f"font-size:17px; font-weight:bold; color:{color};")

    def _fill_table(self, table: QTableWidget, rows, keys):
        table.setRowCount(0)
        for r in rows:
            rc = table.rowCount()
            table.insertRow(rc)
            for ci, k in enumerate(keys):
                val = r.get(k, "")
                item = QTableWidgetItem(str(val) if val is not None else "")
                if k == "side":
                    item.setForeground(QColor(RED if val in ("buy", "add") else GREEN))
                if k == "status" and val == "rejected":
                    item.setForeground(QColor(GRAY))
                if k in ("float_pnl_pct",):
                    try:
                        item.setForeground(QColor(RED if float(val) > 0 else GREEN))
                    except (ValueError, TypeError):
                        pass
                table.setItem(rc, ci, item)

    def _refresh_positions(self):
        keys = ["code", "name", "quantity", "available", "avg_cost",
                "latest_price", "market_value", "float_pnl_pct"]
        self._fill_table(self.tbl_positions, self.engine.get_positions(), keys)

    def _refresh_trades(self):
        keys = ["trade_date", "trade_time", "code", "name", "side", "quantity",
                "price", "amount", "commission", "stamp_tax",
                "status", "reject_reason"]
        self._fill_table(self.tbl_trades, self.engine.get_trades(), keys)

    def _refresh_signals(self):
        rows = self.engine.get_signals()
        out = []
        for r in rows:
            out.append({
                "trade_time": r.get("generated_at", "")[-8:],
                "code": r.get("code"), "side": r.get("side"),
                "quantity": r.get("quantity"), "strength": r.get("strength"),
                "source": r.get("source"), "acted": "是" if r.get("acted") else "否",
                "message": r.get("message"),
            })
        keys = ["trade_time", "code", "side", "quantity", "strength",
                "source", "acted", "message"]
        self._fill_table(self.tbl_signals, out, keys)

    def _refresh_nav(self):
        self.nav_chart.plot(self.engine.get_nav_series())

    # ---------------- 事件 ----------------
    def on_replay(self):
        if self._worker and self._worker.isRunning():
            return
        code = self.stock_combo.currentData()
        self.btn_replay.setEnabled(False)
        self.lbl_status.setText(f"回放中 {code or '默认'} ...")
        self._worker = ReplayWorker(self.engine, code)
        self._worker.done.connect(self._on_replay_done)
        self._worker.start()

    def _on_replay_done(self, result):
        self.btn_replay.setEnabled(True)
        if "error" in result:
            self.lbl_status.setText("回放失败")
            QMessageBox.warning(self, "回放失败", result["error"])
            return
        s = result.get("summary", {})
        self.lbl_status.setText(
            f"回放完成 {result.get('code')} {result.get('name')} | "
            f"买{s.get('buy',0)} 加仓{s.get('add',0)} 卖{s.get('sell',0)+s.get('reduce',0)} | "
            f"最终净值 {result.get('final_value'):,.2f}")
        self.refresh_all()

    def on_strategy_config(self):
        dialog = StrategyConfigDialog(self.engine, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_all()
            self.lbl_status.setText("策略配置已保存并立即生效")

    def on_code_screen(self):
        dialog = CodeScreenDialog(self.engine, self)
        dialog.exec()

    def on_watchlist(self):
        WatchlistDialog(self.engine, self).exec()

    def on_realtime(self):
        try:
            msg = self.engine.start_realtime()
            self.lbl_status.setText(msg)
            self.btn_realtime.setEnabled(False)
            self.btn_realtime.setText("🔴 实时运行中")
        except Exception as e:
            QMessageBox.warning(self, "启动失败", str(e))


class QuantPilotDesktopWindow(QMainWindow):
    """桌面壳窗口：加载 QuantPilot 原型并复用现有 Flask API。"""

    BACKEND_URL = "http://127.0.0.1:5001/"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("QuantPilot · 量化交易工作台")
        self.resize(1440, 920)
        self.setMinimumSize(1100, 720)
        self.browser = QWebEngineView(self)
        self.browser.loadFinished.connect(self._on_load_finished)
        self.setCentralWidget(self.browser)
        self._backend_starting = False
        QTimer.singleShot(0, self._load_client)

    @staticmethod
    def _backend_ready():
        try:
            with urllib.request.urlopen(QuantPilotDesktopWindow.BACKEND_URL, timeout=0.6) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _start_backend(self):
        if self._backend_starting:
            return
        self._backend_starting = True

        def serve():
            try:
                from web_server import run_web_server
                run_web_server(host="127.0.0.1", port=5001, debug=False)
            except Exception:
                logger.exception("QuantPilot 桌面端后端启动失败")

        threading.Thread(target=serve, name="quantpilot-web", daemon=True).start()

    def _load_client(self):
        if not self._backend_ready():
            self._start_backend()
            QTimer.singleShot(500, self._load_client)
            return
        self.browser.setUrl(QUrl(self.BACKEND_URL))

    def _on_load_finished(self, ok):
        if not ok:
            QTimer.singleShot(800, self._load_client)


def run_gui(engine):
    """启动 QuantPilot 原型桌面应用（由 main.py 的 paper 命令调用）。"""
    app = QApplication.instance() or QApplication([])
    win = QuantPilotDesktopWindow()
    win.show()
    return app.exec()
