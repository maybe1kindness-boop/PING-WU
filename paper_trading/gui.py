"""PySide6 桌面 GUI —— 盘中实时模拟盘主界面。

基于 PaperTradingEngine 的数据查询接口构建：
  - 顶部账户概览卡片（净值/现金/市值/盈亏/回撤/持仓数）
  - 标签页：当前持仓 / 交易记录 / 净值曲线 / 信号日志
  - 工具栏：选择股票 + 回放演示 + 刷新（实时模式阶段2接入后自动刷新）

回放在 QThread 中执行，避免阻塞 UI；完成后通过信号回到主线程刷新。
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget)

logger = logging.getLogger(__name__)

# A股惯例：红涨绿跌
RED = "#d8413c"
GREEN = "#21a675"
GRAY = "#888"


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


class NavChartWidget(QWidget):
    """净值曲线（matplotlib 嵌入 QtAgg）。"""
    def __init__(self):
        super().__init__()
        import matplotlib
        matplotlib.use("QtAgg")
        matplotlib.rcParams["font.sans-serif"] = [
            "PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS", "DejaVu Sans"]
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

    def on_realtime(self):
        try:
            msg = self.engine.start_realtime()
            self.lbl_status.setText(msg)
            self.btn_realtime.setEnabled(False)
            self.btn_realtime.setText("🔴 实时运行中")
        except Exception as e:
            QMessageBox.warning(self, "启动失败", str(e))


def run_gui(engine):
    """启动桌面应用（由 main.py 的 paper 命令调用）。"""
    app = QApplication.instance() or QApplication([])
    win = PaperTradingWindow(engine)
    win.show()
    return app.exec()
