#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""OpenPokerDB main window: a Holdem-Manager-2 style report over the fpdb database.

Layout follows ``history/examples/hm2.jpg``: a Cash/Tournament mode switch, a row of report tabs
(By Stakes, By Game Type, Sessions by Day, Players, By Position, Graph), a filter bar whose
"Apply" drives whichever tab is showing, a results grid with a column picker, and under the grid a
hand-history list with miniature cards that opens the table replayer on double-click. Every number
in the grid comes from StatsEngine, which in turn reads the stat columns the importer already wrote
and replays HandsActions for the few the schema does not store -- nothing here is recomputed from a
premise about poker.

    python HudMain.py [--db <sqlite file>]

Cash mode aggregates `Hands`/`HandsPlayers`/`HandsActions`. Tournament mode replaces the grid with
one row per event that a finishing place was recorded for, reading `Tourneys`/`TourneyTypes`/
`TourneysPlayers`; its money is the real buy-in and prize in cents, because a tournament seat's
`totalProfit` is in chips, so the two modes share the grid widget but nothing else. The all-in-EV
line is still opt-in: `allInEV` is 0 until the "Compute all-in EV" button runs `AllInEv`, which is
why that button sits on the graph rather than being run at startup.
"""
import os
import sys
import csv
import glob
import math
import shutil
import sqlite3
import optparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6 import QtCore, QtGui, QtWidgets, QtCharts

import Configuration
import Database
import Card
import HudTable
import StatsEngine
from StatsEngine import Filters, Engine, STATS, report, position_label
from StatsEngine import (TOURNEY_COLUMNS, TOURNEY_BY_KEY, DEFAULT_TOURNEY_COLUMNS,
                         tournament_report)

STAT_BY_KEY = {k: (label, kind) for (k, label, kind, _n, _d) in STATS}
DEFAULT_COLUMNS = ["hands", "net", "bb100", "vpip", "pfr", "b3", "wtsd", "w$sd", "aggf", "aggq"]
# report tab -> (group_by, human title). 'position' aggregates every player's seat, HM2's "By Position".
REPORTS = [
    ("By Stakes", ("gametype",)),
    ("By Game Type", ("game",)),
    ("Sessions by Day", ("day",)),
    ("Players", ("player",)),
    ("By Position", ("position",)),
]
# Tournament mode has one grid: `tournament_report` already returns one row per event, so the
# cash grouping tabs (stake, position) have no meaning here and the tab box is disabled instead of
# being filled with a second, different list.
TOURNEY_TABS = ["Tournament Results"]
LINE_CODE = {"folds": "F", "checks": "X", "calls": "C", "bets": "B", "raises": "R",
             "completes": "C"}


def money(cents):
    v = int(round(cents or 0))
    return ("-$%.2f" if v < 0 else "$%.2f") % (abs(v) / 100.0)


def chips(cents):
    """A tournament stack delta: the column holds chips scaled by 100, so show the chip count."""
    v = int(round(cents or 0))
    return "{:,}".format(int(round(v / 100.0)))


def _nice_step(span, divisions):
    """A round axis tick spacing: the smallest 1/2/5x10^n step that keeps ~`divisions` labels.

    QValueAxis otherwise divides the range evenly and prints whatever falls out, which is how a
    hand-count axis ended up labelled '86.3' and a bb axis '-476.0'.
    """
    raw = max(span, 1) / float(max(divisions, 1))
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= magnitude * multiple:
            return magnitude * multiple
    return magnitude * 10


class Gametypes(object):
    """Cached Gametypes rows so a group key can be turned back into '$0.01/$0.02 NL Hold'em'."""

    def __init__(self, conn):
        self.rows = {}
        for gid, kind, cat, lim, sb, bb, seats in conn.execute(
                "select id, type, category, limitType, smallBlind, bigBlind, maxSeats from Gametypes"):
            self.rows[gid] = (cat, lim, sb, bb, seats, kind)

    def label(self, gid):
        cat, lim, sb, bb, seats, _kind = self.rows.get(gid, ("?", "?", 0, 0, 0, "ring"))
        game = {"holdem": "NL Hold'em" if lim == "nl" else "PL Hold'em",
                "omahahi": "PLO" if lim == "pl" else "Omaha"}.get(cat, cat)
        return "%s %s/%s" % (game, money(sb), money(bb))

    def combo_items(self, ring_only=True):
        """Stakes for the filter box. Tournament gametypes are left out by default: their blind
        columns hold the level's chip structure, so '$300.00/$600.00' would read as a high-stakes
        cash game that never existed."""
        return sorted(((gid, self.label(gid)) for gid, row in self.rows.items()
                       if not ring_only or row[5] != "tour"), key=lambda x: x[1])


class StatsModel(QtCore.QAbstractTableModel):
    """Read-only grid model over report() rows; column 0 is the group label, the rest are stats.

    `fields` maps a column key to its (label, kind) so the same model renders a cash report (STATS)
    or a tournament report (TOURNEY_COLUMNS); it defaults to the cash table.
    """

    def __init__(self, parent=None):
        QtCore.QAbstractTableModel.__init__(self, parent)
        self.rows = []
        self.columns = list(DEFAULT_COLUMNS)
        self.heading = "Report"
        self.fields = STAT_BY_KEY

    def configure(self, rows, columns, heading, fields=None):
        self.beginResetModel()
        self.rows = rows
        self.columns = columns
        self.heading = heading
        self.fields = fields or STAT_BY_KEY
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 1 + len(self.columns)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role != QtCore.Qt.DisplayRole:
            return None
        if orientation == QtCore.Qt.Horizontal:
            if section == 0:
                return self.heading
            label, _kind = self.fields[self.columns[section - 1]]
            return label
        return str(section + 1)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if index.column() == 0:
            if role in (QtCore.Qt.DisplayRole, QtCore.Qt.EditRole):
                return row["_label"]
            return None
        key = self.columns[index.column() - 1]
        display, num, den = row[key]
        if role == QtCore.Qt.DisplayRole:
            return display
        if role == QtCore.Qt.TextAlignmentRole:
            return QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
        if role == QtCore.Qt.ToolTipRole and den:
            return "%d / %d" % (num, den)
        if role == QtCore.Qt.ForegroundRole:
            kind = self.fields[key][1]
            if kind in ("money", "bb") and num:
                return QtGui.QBrush(QtGui.QColor("#1a7f37" if num > 0 else "#c0392b"))
        if role == QtCore.Qt.BackgroundRole and index.row() == 0:
            return QtGui.QBrush(QtGui.QColor("#dfe9f5"))
        return None


class ColumnPicker(QtWidgets.QDialog):
    """Checkable list of every stat, so the grid can be widened or narrowed on the fly."""

    def __init__(self, columns, parent=None, fields=None, order=None):
        QtWidgets.QDialog.__init__(self, parent)
        self.setWindowTitle("Choose report columns")
        fields = fields or STAT_BY_KEY
        order = order or [k for (k, _l, _kind, _n, _d) in STATS]
        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for key in order:
            item = QtWidgets.QListWidgetItem(fields[key][0])
            item.setData(QtCore.Qt.UserRole, key)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked if key in columns else QtCore.Qt.Unchecked)
            self.list.addItem(item)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("Tick the columns to show. The order follows the reference list.")
        box.addWidget(hint)
        box.addWidget(self.list)
        box.addWidget(buttons)

    def chosen(self):
        keys = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == QtCore.Qt.Checked:
                keys.append(item.data(QtCore.Qt.UserRole))
        return keys


class HistoryList(QtWidgets.QWidget):
    """The hand list under the grid: time, mini cards, line, board. Double-click replays the hand."""

    hand_activated = QtCore.Signal(int, str)          # hand_id, player name

    def __init__(self, db_path, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.db_path = db_path
        self.title = QtWidgets.QLabel("Hand history")
        self.title.setStyleSheet("font-weight:bold;")
        self.page = QtWidgets.QComboBox()
        for text, n in (("Last 250", 250), ("Last 1,000", 1000), ("All", 0)):
            self.page.addItem(text, n)
        self.page.currentIndexChanged.connect(self.reload)
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(1, 100000)
        self.count.setValue(250)
        self.count.setSuffix(" hands")
        self.count.valueChanged.connect(self.reload)
        self.view = QtWidgets.QTableWidget()
        self.view.setColumnCount(5)
        self.view.setHorizontalHeaderLabels(["Time", "Cards", "Line", "Board", "Result ($)"])
        self.view.verticalHeader().setVisible(False)
        self.view.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.view.setRowHeight(0, 30)
        self.view.doubleClicked.connect(self._open)
        self.filters = Filters()
        self.player = "Hero"
        self.gametype_id = None
        self.tourney_id = None
        self.chips = False
        self._hand_ids = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(QtWidgets.QLabel("Show"))
        top.addWidget(self.page)
        top.addWidget(self.count)
        layout.addLayout(top)
        layout.addWidget(self.view)

    def show_for(self, filters, player, gametype_id, heading, tourney_id=None, chips=False):
        """List the hands behind a grid row.

        `tourney_id` restricts to one event and `chips` relabels the money column: a tournament
        seat's `HandsPlayers.totalProfit` is in chips, not cents (measured: -1,000,000 chips in a $2
        Daily Classic), so labelling that column '$' would print a $2 loss as -$10,000.00.
        """
        self.filters = filters
        self.player = player or "Hero"
        self.gametype_id = gametype_id
        self.tourney_id = tourney_id
        self.chips = chips
        self.view.horizontalHeaderItem(4).setText("Result (chips)" if chips else "Result ($)")
        self.title.setText("Hand history -- %s%s" % (heading, "" if not player else " (%s)" % player))
        self.reload()

    def _limit(self):
        combo_n = self.page.currentData()
        return self.count.value() if self.page.currentIndex() == 2 else combo_n

    def reload(self):
        if not os.path.exists(self.db_path):
            return
        con = sqlite3.connect(self.db_path)
        self.view.setRowCount(0)
        self._hand_ids = []
        try:
            where, params = self._where()
            limit = self._limit()
            sql = ("select h.id, h.startTime, hp.card1, hp.card2, hp.playerId, h.boardcard1, "
                   "h.boardcard2, h.boardcard3, h.boardcard4, h.boardcard5, hp.totalProfit "
                   "from Hands h "
                   "join Gametypes g on g.id = h.gametypeId "
                   "left join Sites s on s.id = g.siteId "
                   "join HandsPlayers hp on hp.handId = h.id "
                   "join Players p on p.id = hp.playerId" + where +
                   " order by h.startTime desc, h.id desc" + (" limit ?" if limit else ""))
            args = list(params) + ([limit] if limit else [])
            rows = con.execute(sql, args).fetchall()
            lines = self._lines_for(con, [(r[0], r[4]) for r in rows])
            for row in rows:
                r = self.view.rowCount()
                self.view.insertRow(r)
                self.view.setRowHeight(r, 34)
                self.view.setItem(r, 0, QtWidgets.QTableWidgetItem(str(row[1])))
                self._set_cards(r, 1, [c for c in (row[2], row[3]) if c])
                self.view.setItem(r, 2, QtWidgets.QTableWidgetItem(lines.get(row[0], "")))
                self._set_cards(r, 3, [c for c in row[5:10] if c])
                shown = chips(row[10]) if self.chips else money(row[10])
                result = QtWidgets.QTableWidgetItem(shown)
                if row[10]:
                    result.setForeground(QtGui.QBrush(QtGui.QColor(
                        "#1a7f37" if row[10] > 0 else "#c0392b")))
                self.view.setItem(r, 4, result)
                self._hand_ids.append(row[0])
        except sqlite3.Error as exc:
            self.title.setText("Hand history -- cannot read: %s" % exc)
        finally:
            con.close()
        self.view.resizeColumnsToContents()

    def _where(self):
        clauses, params = [], []
        base = Filters(self.filters.date_from, self.filters.date_to, self.filters.site,
                       self.gametype_id or self.filters.gametype_id, self.filters.limit_type,
                       self.filters.category, tourney_id=self.tourney_id)
        if base.sql:
            clauses.append(base.sql[len(" where "):])
            params.extend(base.params)
        if self.player:
            clauses.append("p.name = ?")
            params.append(self.player)
        return (" where " + " and ".join(clauses)) if clauses else "", params

    def _lines_for(self, con, pairs):
        """{hand_id: action string} for one player across many hands, in a single query.

        A Players.name maps to one playerId for the whole corpus, so every pair shares it; the
        action string is that seat's verb on each street, e.g. 'PF:C F:X T:B'.
        """
        hand_ids = [h for h, _pid in pairs if h is not None]
        if not hand_ids:
            return {}
        pid = pairs[0][1]
        marks = ",".join("?" * len(hand_ids))
        rows = con.execute(
            "select ha.handId, ha.street, a.name from HandsActions ha "
            "join Actions a on a.id = ha.actionId "
            "where ha.playerId = ? and ha.handId in (%s) order by ha.handId, ha.actionNo" % marks,
            [pid] + hand_ids).fetchall()
        by_hand = {}
        for hand_id, street, verb in rows:
            if street < 0:
                continue
            by_hand.setdefault(hand_id, {}).setdefault(street, []).append(LINE_CODE.get(verb, ""))
        return dict((hand_id, "  ".join("%s:%s" % (StatsEngine.STREET_NAME[s], "".join(streets[s]))
                                        for s in sorted(streets)))
                    for hand_id, streets in by_hand.items())

    def _set_cards(self, row, column, card_ids):
        cell = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(cell)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(1)
        for cid in card_ids[:5]:
            label = QtWidgets.QLabel()
            label.setPixmap(HudTable.card_pixmap(cid, 26))
            lay.addWidget(label)
        lay.addStretch(1)
        self.view.setCellWidget(row, column, cell)

    def _open(self, _index):
        row = self.view.currentRow()
        if 0 <= row < len(self._hand_ids):
            self.hand_activated.emit(self._hand_ids[row], self.player)


class GraphView(QtWidgets.QWidget):
    """Results graph: cumulative Net Won / All-In EV / Showdown / Non-Showdown in bb, with a summary.

    Each line has a tick box in the toolbar above it, because 'plot these four lines' is a question the
    answer to which changes while looking at the plot; and the All-In EV line comes out of
    `HandsPlayers.allInEV`, which is empty until `AllInEv.fill_column` has run, so the panel that draws
    it also carries the button that fills it.
    """

    recompute_ev = QtCore.Signal()                       # MainWindow owns the database, not this widget

    LINES = [("net", "Net Won", "#1f77b4", False),
             ("ev", "All-In EV", "#7b3294", True),
             ("nsd", "Non-Showdown", "#d62728", False),
             ("sd", "Showdown", "#2ca02c", False)]

    def __init__(self, db_path, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.db_path = db_path
        self.series = []
        self.chart = QtCharts.QChart()
        self.chart.setTitle("Results Graph")
        self.chart.setMargins(QtCore.QMargins(6, 6, 6, 6))
        self.axis_x = QtCharts.QValueAxis()
        self.axis_x.setTitleText("Hands")
        self.axis_y = QtCharts.QValueAxis()
        self.axis_y.setTitleText("bb")
        # A value axis defaults to a fixed *number* of ticks and ignores setTickInterval; the mode that
        # honours an interval is TicksDynamic. PySide6 6.12 exposes that enum only through an instance,
        # not as QAbstractAxis.TickType, so it is read off the axis rather than named.
        self.ticks_dynamic = self.axis_x.tickType().__class__.TicksDynamic
        # Attach the axes once, here. Removing and re-adding the same axis on every redraw leaks its
        # title -- after a few tick-box toggles the chart grew a second 'bb' and a second 'Hands'.
        self.chart.addAxis(self.axis_x, QtCore.Qt.AlignBottom)
        self.chart.addAxis(self.axis_y, QtCore.Qt.AlignLeft)
        self.scene = QtCharts.QChartView(self.chart)
        self.scene.setRenderHint(QtGui.QPainter.Antialiasing)
        self.legend = self.chart.legend()
        self.legend.setAlignment(QtCore.Qt.AlignBottom)
        self.summary = QtWidgets.QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumWidth(260)
        self.summary.setStyleSheet("background:#fffbe6; border:1px solid #d9b23a; padding:6px;")
        self.player = QtWidgets.QLineEdit("Hero")
        self.player.setPlaceholderText("player name for the graph")
        self.player.returnPressed.connect(self.reload)
        self.toggles = {}
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Graph player:"))
        top.addWidget(self.player)
        for key, label, color, _dash in self.LINES:
            box = QtWidgets.QCheckBox(label)
            box.setChecked(True)
            box.setStyleSheet("color:%s; padding-left:4px;" % color)
            box.toggled.connect(self._draw_current)
            self.toggles[key] = box
            top.addWidget(box)
        self.ev_button = QtWidgets.QPushButton("Compute all-in EV")
        self.ev_button.setToolTip("Fill HandsPlayers.allInEV from the hand histories, then redraw. "
                                  "Needed once per database, and again after an import.")
        self.ev_button.clicked.connect(self.recompute_ev)
        top.addWidget(self.ev_button)
        top.addStretch(1)
        box = QtWidgets.QHBoxLayout(self)
        left = QtWidgets.QVBoxLayout()
        left.addLayout(top)
        left.addWidget(self.scene)
        box.addLayout(left, 1)
        box.addWidget(self.summary)

    def reload(self, filters=None, fallback_player="Hero"):
        name = self.player.text().strip() or fallback_player
        con = sqlite3.connect(self.db_path)
        try:
            self.series = Engine(con).series(filters or Filters(), player=name)
        finally:
            con.close()
        self._draw(name)

    def _draw_current(self):
        """Redraw with whatever the tick boxes say, without re-querying."""
        self._draw(self.player.text().strip() or "Hero")

    def _draw(self, name):
        """Redraw the visible lines. The axes stay attached; only the series are rebuilt."""
        self.chart.removeAllSeries()
        pts = self.series
        keys = [(key, label, color, dash) for key, label, color, dash in self.LINES
                if self.toggles[key].isChecked()]
        if not pts or not keys:
            self.summary.setPlainText("No hands match the current filters." if not pts
                                      else "Tick at least one line.")
            self.axis_x.setRange(0, 1)
            self.axis_y.setRange(-1, 1)
            return
        # Set the ranges before anything attaches: QValueAxis recomputes its tick interval when a
        # series is attached, so an interval set afterwards is silently thrown away and the axis
        # divides the range into its default 5 pieces -- which is how the hands axis got labelled
        # '86.3' and the bb axis '-125.0'.
        values = [p[key] for p in pts for key, _l, _c, _d in keys]
        lo, hi = min(values), max(values)
        y_step = _nice_step(max(hi - lo, abs(hi), abs(lo), 1), 5)
        self.axis_x.setRange(0, pts[-1]["n"])
        self.axis_x.setTickType(self.ticks_dynamic)
        self.axis_x.setTickInterval(_nice_step(pts[-1]["n"], 5))
        self.axis_y.setRange(math.floor(lo / y_step) * y_step, math.ceil(hi / y_step) * y_step)
        self.axis_y.setTickType(self.ticks_dynamic)
        self.axis_y.setTickInterval(y_step)
        self.axis_x.setLabelFormat("%d")
        self.axis_y.setLabelFormat("%.0f" if y_step >= 1 else "%g")
        for key, label, color, dash in keys:
            series = QtCharts.QLineSeries()
            series.setName(label)
            for p in pts:
                series.append(p["n"], p[key])
            pen = QtGui.QPen(QtGui.QColor(color))
            pen.setWidth(2)
            if dash:
                pen.setStyle(QtCore.Qt.DashLine)
            series.setPen(pen)
            self.chart.addSeries(series)
            series.attachAxis(self.axis_x)
            series.attachAxis(self.axis_y)
        last = pts[-1]
        ev_hands = last["ev_hands"]
        text = ("Player: %s\nHands: %d\n\nNet Won: %.1f bb\nShowdown: %.1f bb\n"
                "Non-Showdown: %.1f bb\nAll-In EV: %.1f bb\n(all-in hands: %d)\n"
                % (name, last["n"], last["net"], last["sd"], last["nsd"], last["ev"], ev_hands))
        if not ev_hands:
            text += ("\nNo seat in this range has an all-in EV yet, so that line is the net line. "
                     "Press 'Compute all-in EV' to fill it.")
        else:
            text += ("\nVariance removed by the EV line: %+.1f bb\n(%d all-in hands of %d)"
                     % (last["ev"] - last["net"], ev_hands, last["n"]))
        self.summary.setPlainText(text)


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, db_path):
        QtWidgets.QMainWindow.__init__(self)
        self.db_path = db_path
        # Two databases in this build are both called fpdb.db3 (the live one and the tournament
        # sandbox), so the title has to carry the whole path to be tellable-apart.
        self.setWindowTitle("OpenPokerDB -- %s" % db_path)
        self.resize(1360, 860)
        self.gametypes = None
        self.current_group = ("gametype",)
        self.current_columns = list(DEFAULT_COLUMNS)
        self.last_rows = []
        # 'cash' reads Hands/HandsPlayers through StatsEngine.report; 'tour' reads Tourneys/
        # TourneysPlayers through tournament_report. The two grids share the model but not the
        # columns, filters or meaning of a selected row, so everything below branches on this.
        self.mode = "cash"
        self.tourney_columns = list(DEFAULT_TOURNEY_COLUMNS)
        self._selected_tourney = None

        self._build_menu()
        self._build_toolbar()
        self._build_filters()
        self._build_body()
        self.statusBar().showMessage(self._db_summary())

    # -- construction ---------------------------------------------------------------

    def _build_menu(self):
        db_menu = self.menuBar().addMenu("&Database")
        for text, slot in (
                ("&Import hand histories...", self.import_histories),
                ("&Backup database...", self.backup_database),
                ("&Load / restore database...", self.load_database),
                ("&Purge hands...", self.purge_hands),
                ("&Recompute all-in EV", self.recompute_ev),
                ("&VACUUM (compact)", self.vacuum_database),
                ("&Integrity check", self.integrity_check),
                ("E&xport grid to CSV...", self.export_csv)):
            action = QtGui.QAction(text, self)
            action.triggered.connect(slot)
            db_menu.addAction(action)
        help_menu = self.menuBar().addMenu("&Help")
        about = QtGui.QAction("About", self)
        about.triggered.connect(lambda: QtWidgets.QMessageBox.about(
            self, "OpenPokerDB",
            "Cash reports over the fpdb database. Numbers come from StatsEngine."))
        help_menu.addAction(about)

    def _build_toolbar(self):
        bar = QtWidgets.QToolBar("Mode")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.cash_btn = QtWidgets.QPushButton("Cash")
        self.tourney_btn = QtWidgets.QPushButton("Tournament")
        for button in (self.cash_btn, self.tourney_btn):
            button.setCheckable(True)
            button.setStyleSheet("QPushButton:checked {background:#2d6cdf; color:white;"
                                 "font-weight:bold;}")
            bar.addWidget(button)
        self.cash_btn.setChecked(True)
        self.cash_btn.clicked.connect(lambda: self.set_mode("cash"))
        self.tourney_btn.clicked.connect(lambda: self.set_mode("tour"))
        self.tourney_btn.setToolTip("One row per tournament that a summary gave a finishing place "
                                    "to; money is the real buy-in/prize, not chips.")
        bar.addSeparator()
        self.tabs_bar = QtWidgets.QComboBox()
        for title, _group in REPORTS:
            self.tabs_bar.addItem(title)
        self.tabs_bar.currentIndexChanged.connect(self._tab_changed)
        bar.addWidget(self.tabs_bar)
        bar.addSeparator()
        columns_btn = QtWidgets.QPushButton("Columns...")
        columns_btn.setToolTip("Add or remove stats from this report")
        columns_btn.clicked.connect(self.choose_columns)
        bar.addWidget(columns_btn)
        self.graph_btn = QtWidgets.QPushButton("Graph")
        self.graph_btn.setToolTip("The bb results graph is a cash measure; a tournament's blinds "
                                  "change every level.")
        self.graph_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.graph))
        bar.addWidget(self.graph_btn)
        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.clicked.connect(self.apply_filters)
        bar.addWidget(refresh_btn)

    def set_mode(self, mode):
        """Swap the grid, tabs, filters and history pane between cash and tournament reading.

        The stakes filter is dropped in tournament mode on purpose: a tournament's Gametypes row
        stores the level's blinds at the hand's start, so filtering tournament hands by 'stake'
        would silently filter by blind level rather than by the price paid.
        """
        self.mode = mode
        tour = mode == "tour"
        self.cash_btn.setChecked(not tour)
        self.tourney_btn.setChecked(tour)
        self.tabs_bar.blockSignals(True)
        self.tabs_bar.clear()
        if tour:
            self.tabs_bar.addItem(TOURNEY_TABS[0])
            self.tabs_bar.setEnabled(False)
        else:
            for title, _group in REPORTS:
                self.tabs_bar.addItem(title)
            self.tabs_bar.setEnabled(True)
        self.tabs_bar.blockSignals(False)
        self.stakes.setEnabled(not tour)
        self.stakes.setToolTip("" if not tour else "Stakes are a cash filter; tournaments use "
                                                   "the buy-in shown in each row.")
        self.min_hands.setEnabled(not tour)
        self.min_hands.setToolTip("" if not tour else "Tournament rows are events, not hands.")
        self.graph_btn.setEnabled(not tour)
        self._selected_tourney = None
        self.apply_filters()

    def _current_columns(self):
        return self.tourney_columns if self.mode == "tour" else self.current_columns

    def _current_fields(self):
        return TOURNEY_BY_KEY if self.mode == "tour" else STAT_BY_KEY

    def _build_filters(self):
        panel = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(panel)
        row.setContentsMargins(8, 4, 8, 4)
        today = datetime.date.today()
        self.date_from = QtWidgets.QDateEdit(QtCore.QDate(today.year, 1, 1))
        self.date_from.setCalendarPopup(True)
        self.date_to = QtWidgets.QDateEdit(QtCore.QDate(today))
        self.date_to.setCalendarPopup(True)
        self.use_dates = QtWidgets.QCheckBox("Date range")
        row.addWidget(self.use_dates)
        row.addWidget(self.date_from)
        row.addWidget(QtWidgets.QLabel("to"))
        row.addWidget(self.date_to)
        self.game_type = QtWidgets.QComboBox()
        self.game_type.addItem("Any game type", None)
        for key, label in (("holdem-nl", "NL Hold'em"), ("omahahi-pl", "Pot-Limit Omaha")):
            self.game_type.addItem(label, key)
        row.addWidget(self.game_type)
        self.stakes = QtWidgets.QComboBox()
        self.stakes.addItem("Any stakes", None)
        row.addWidget(self.stakes)
        self.player_filter = QtWidgets.QLineEdit()
        self.player_filter.setPlaceholderText("filter player name")
        row.addWidget(self.player_filter)
        self.min_hands = QtWidgets.QSpinBox()
        self.min_hands.setRange(0, 1000000)
        self.min_hands.setValue(1)
        self.min_hands.setSuffix(" min hands")
        row.addWidget(self.min_hands)
        apply_btn = QtWidgets.QPushButton("Apply to all reports")
        apply_btn.setStyleSheet("QPushButton {background:#e8f0fe; font-weight:bold;}")
        apply_btn.clicked.connect(self.apply_filters)
        row.addWidget(apply_btn)
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_filters)
        row.addWidget(clear_btn)
        row.addStretch(1)
        self.filter_panel = panel

    def _build_body(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.filter_panel)

        self.split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        outer.addWidget(self.split)

        self.stack = QtWidgets.QStackedWidget()
        self.model = StatsModel()
        self.grid = QtWidgets.QTableView()
        self.grid.setModel(self.model)
        self.grid.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.grid.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.grid.verticalHeader().setVisible(False)
        self.grid.selectionModel().selectionChanged.connect(self._row_selected)
        grid_page = QtWidgets.QWidget()
        grid_layout = QtWidgets.QVBoxLayout(grid_page)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.addWidget(self.grid)
        self.stack.addWidget(grid_page)
        self.graph = GraphView(self.db_path)
        self.graph.recompute_ev.connect(self.recompute_ev)
        self.stack.addWidget(self.graph)
        self.split.addWidget(self.stack)

        self.history = HistoryList(self.db_path)
        self.history.hand_activated.connect(self.open_replayer)
        self.split.addWidget(self.history)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([560, 260])

    # -- data -----------------------------------------------------------------------

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _db_summary(self):
        if not os.path.exists(self.db_path):
            return "no database at %s -- use Database > Import hand histories" % self.db_path
        con = self._connect()
        try:
            hands = con.execute("select count(*) from Hands").fetchone()[0]
            tourneys = con.execute(
                "select count(*) from Tourneys t join TourneysPlayers tp on tp.tourneyId = t.id "
                "where tp.rank is not null").fetchone()[0]
            self.gametypes = Gametypes(con)
        except sqlite3.Error:
            return "database %s has no Hands table yet" % os.path.basename(self.db_path)
        finally:
            con.close()
        return "%d cash hands, %d finished tournaments -- %s" % (
            hands, tourneys, self.db_path)

    def _refresh_stakes_combo(self):
        con = self._connect()
        try:
            self.gametypes = Gametypes(con)
        finally:
            con.close()
        keep = self.stakes.currentData()
        self.stakes.blockSignals(True)
        self.stakes.clear()
        self.stakes.addItem("Any stakes", None)
        for gid, label in self.gametypes.combo_items():
            self.stakes.addItem(label, gid)
        idx = self.stakes.findData(keep)
        self.stakes.setCurrentIndex(idx if idx >= 0 else 0)
        self.stakes.blockSignals(False)

    def current_filters(self):
        game_key = self.game_type.currentData()
        category = limit = None
        if game_key:
            category, limit = game_key.split("-")
        date_from = date_to = None
        if self.use_dates.isChecked():
            date_from = self.date_from.date().toString("yyyy-MM-dd")
            date_to = self.date_to.date().toString("yyyy-MM-dd")
        # In tournament mode the stakes combo is disabled, so any stake left selected from cash mode
        # must not leak into the hand-history WHERE: a tourney's Gametypes row is a blind level.
        gametype_id = None if self.mode == "tour" else self.stakes.currentData()
        return Filters(date_from=date_from, date_to=date_to, gametype_id=gametype_id,
                       limit_type=limit, category=category,
                       player=self.player_filter.text().strip() or None)

    def clear_filters(self):
        self.use_dates.setChecked(False)
        self.game_type.setCurrentIndex(0)
        self.stakes.setCurrentIndex(0)
        self.player_filter.clear()
        self.min_hands.setValue(1)
        self.apply_filters()

    def apply_filters(self):
        if not os.path.exists(self.db_path):
            self.statusBar().showMessage(self._db_summary())
            return
        if self.mode == "tour":
            self._apply_tourney_filters()
            return
        self._refresh_stakes_combo()
        filters = self.current_filters()
        title, group = REPORTS[self.tabs_bar.currentIndex()]
        self.current_group = group
        con = self._connect()
        try:
            rows = report(con, filters, group_by=group, columns=self.current_columns,
                          min_hands=self.min_hands.value())
        finally:
            con.close()
        for row in rows:
            row["_label"] = self._label_for(group, row["group"])
        self.last_rows = rows
        self.model.configure(rows, self.current_columns, title.replace("By ", ""))
        self.grid.resizeColumnsToContents()
        self.grid.resizeColumnToContents(0)
        self.statusBar().showMessage("%s -- %d rows, %d hands" % (
            title, len(rows), sum(r["_counters"].get("hands", 0) for r in rows)))
        self.history.show_for(filters, self._focus_player(rows, group), None, title)
        if self.stack.currentWidget() is self.graph:
            self.graph.reload(filters, self._focus_player(rows, group) or "Hero")

    def _apply_tourney_filters(self):
        """Tournament grid: one row per finished event, plus the hand list under the selected event."""
        filters = self.current_filters()
        self.current_group = ("tourney",)
        con = self._connect()
        try:
            rows = tournament_report(con, filters)
        finally:
            con.close()
        self.last_rows = rows
        self.model.configure(rows, self.tourney_columns, "Tournament", TOURNEY_BY_KEY)
        self.grid.resizeColumnsToContents()
        self.grid.resizeColumnToContents(0)
        events = [r for r in rows if r["group"][0] != "totals"]
        net = sum(r["_counters"].get("net", 0) for r in events)
        self.statusBar().showMessage("Tournament -- %d events, %d hands, net %s" % (
            len(events), sum(r["_counters"].get("hands", 0) for r in events), money(net)))
        # Select the first real event (row 0 is the TOTAL banner) so the hand list is populated the
        # way HM2 fills the pane under a clicked row rather than leaving it empty on entry.
        if len(rows) > 1:
            self.grid.selectRow(1)
        else:
            self.history.show_for(filters, "Hero", None, "no finished events", chips=True)

    def _label_for(self, group, key):
        if group == ("gametype",):
            return self.gametypes.label(key[0]) if self.gametypes else str(key[0])
        if group == ("game",):
            cat, lim = (key[0].split("-") + [""])[:2]
            return {"holdem": "Hold'em", "omahahi": "Omaha"}.get(cat, cat) + " " + lim.upper()
        return str(key[0])

    def _focus_player(self, rows, group):
        if group == ("player",) and rows:
            return rows[0]["group"][0]
        return "Hero"

    def _tab_changed(self, _index):
        self.stack.setCurrentIndex(0)
        self.apply_filters()

    def _row_selected(self, *_args):
        index = self.grid.selectionModel().currentIndex()
        if not index.isValid():
            return
        row = self.model.rows[index.row()]
        filters = self.current_filters()
        if self.mode == "tour":
            # Row group is (tourneyId, playerName); the TOTAL banner has no event to list.
            if row["group"][0] == "totals":
                return
            self._selected_tourney = row["group"][0]
            self.history.show_for(filters, row["group"][1], None, row["_label"],
                                  tourney_id=row["group"][0], chips=True)
            return
        gametype_id = row["group"][0] if self.current_group == ("gametype",) else None
        player = row["group"][0] if self.current_group == ("player",) else "Hero"
        label = row["_label"]
        self.history.show_for(filters, player, gametype_id, label)

    def choose_columns(self):
        if self.mode == "tour":
            picker = ColumnPicker(self.tourney_columns, self, fields=TOURNEY_BY_KEY,
                                  order=[k for (k, _l, _kind) in TOURNEY_COLUMNS])
            if picker.exec() == QtWidgets.QDialog.Accepted:
                chosen = picker.chosen()
                if chosen:
                    self.tourney_columns = chosen
                    self.apply_filters()
            return
        picker = ColumnPicker(self.current_columns, self)
        if picker.exec() == QtWidgets.QDialog.Accepted:
            chosen = picker.chosen()
            if chosen:
                self.current_columns = chosen
                self.apply_filters()

    # -- database operations --------------------------------------------------------

    def import_histories(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Import hand histories", os.path.expanduser("~"))
        if not folder:
            return
        count = len(glob.glob(os.path.join(folder, "**", "*.txt"), recursive=True))
        if QtWidgets.QMessageBox.question(
                self, "Import histories",
                "Import every .txt under:\n%s\n\n(%d files found)\n\nThis runs the real importer "
                "and adds hands to %s." % (folder, count, self.db_path)) \
                != QtWidgets.QMessageBox.Yes:
            return
        importer = ImportDialog(self.db_path, folder, self)
        importer.run()

    def backup_database(self):
        if not os.path.exists(self.db_path):
            QtWidgets.QMessageBox.information(self, "Backup", "No database to back up yet.")
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = QtWidgets.QFileDialog.getSaveFileName(
            self, "Backup database", os.path.join(os.path.dirname(self.db_path),
                                                  "fpdb-backup-%s.db3" % stamp))[0]
        if not target:
            return
        src = sqlite3.connect(self.db_path)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            QtWidgets.QMessageBox.information(self, "Backup", "Wrote a consistent copy to\n%s" % target)
        except sqlite3.Error as exc:
            QtWidgets.QMessageBox.critical(self, "Backup failed", str(exc))
        finally:
            src.close()
            dst.close()

    def load_database(self):
        target = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load a database file", os.path.dirname(self.db_path),
            "SQLite (*.db3 *.sqlite *.sqlite3);;All files (*)")[0]
        if not target:
            return
        con = sqlite3.connect(target)
        try:
            hands = con.execute("select count(*) from Hands").fetchone()[0]
        except sqlite3.Error as exc:
            QtWidgets.QMessageBox.critical(self, "Load", "%s is not an OpenPokerDB database: %s"
                                           % (target, exc))
            return
        finally:
            con.close()
        self.db_path = target
        self.history.db_path = target
        self.graph.db_path = target
        self.statusBar().showMessage(self._db_summary())
        self.apply_filters()
        QtWidgets.QMessageBox.information(self, "Load", "Now reading %s (%d hands)." % (target, hands))

    def purge_hands(self):
        if not os.path.exists(self.db_path):
            return
        dialog = PurgeDialog(self.db_path, self)
        dialog.exec()
        self.apply_filters()

    def vacuum_database(self):
        if not os.path.exists(self.db_path):
            return
        if QtWidgets.QMessageBox.question(
                self, "VACUUM", "Rebuild the database file to reclaim space. Continue?") \
                != QtWidgets.QMessageBox.Yes:
            return
        con = self._connect()
        try:
            con.isolation_level = None
            con.execute("VACUUM")
            QtWidgets.QMessageBox.information(self, "VACUUM", "Done.")
        except sqlite3.Error as exc:
            QtWidgets.QMessageBox.critical(self, "VACUUM failed", str(exc))
        finally:
            con.close()

    def integrity_check(self):
        if not os.path.exists(self.db_path):
            return
        con = self._connect()
        try:
            result = [r[0] for r in con.execute("PRAGMA integrity_check")]
        finally:
            con.close()
        QtWidgets.QMessageBox.information(
            self, "Integrity check", "\n".join(result) or "no result")

    def recompute_ev(self):
        """Fill `HandsPlayers.allInEV` for every all-in seat, then redraw the graph.

        This is the column fpdb filled through `pokereval`, which cannot be installed here, so AllInEv
        computes it from the hand histories on demand instead. Only the rows of hands that contain an
        all-in wager are written -- 33 of 345 in this corpus -- and a preflop shove against a caller who
        never showed is sampled, so the pass takes a minute rather than a second. Re-run it after an
        import: newly imported hands arrive with the column empty and their EV line is their net line.
        """
        if not os.path.exists(self.db_path):
            return
        import AllInEv
        con = self._connect()
        dialog = QtWidgets.QProgressDialog("Computing all-in EV...", None, 0, 100, self)
        dialog.setWindowModality(QtCore.Qt.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)

        def progress(done, total):
            dialog.setValue(int(100.0 * done / max(total, 1)))
            QtWidgets.QApplication.processEvents()

        try:
            written, hands, seconds = AllInEv.fill_column(con, progress)
        except sqlite3.Error as exc:
            QtWidgets.QMessageBox.critical(self, "All-in EV failed", str(exc))
            return
        finally:
            dialog.reset()
            dialog.close()
            con.close()
        # Show the result, not just the message: apply_filters only redraws the graph when the graph is
        # the page on screen, and the line this button exists for is the one he cannot otherwise see.
        self.stack.setCurrentWidget(self.graph)
        self.apply_filters()
        QtWidgets.QMessageBox.information(
            self, "All-in EV",
            "%d seats across %d all-in hands in %.1fs.\nThe purple line is now on the graph."
            % (written, hands, seconds))

    def export_csv(self):
        if not self.last_rows:
            QtWidgets.QMessageBox.information(self, "Export", "Nothing to export yet.")
            return
        target = QtWidgets.QFileDialog.getSaveFileName(self, "Export grid to CSV",
                                                       "report.csv", "CSV (*.csv)")[0]
        if not target:
            return
        columns = self._current_columns()
        fields = self._current_fields()
        with open(target, "w", newline="", encoding="utf8") as handle:
            writer = csv.writer(handle)
            header = ["Report"] + [fields[c][0] for c in columns]
            writer.writerow(header)
            for row in self.last_rows:
                writer.writerow([row["_label"]] + [row[c][0] for c in columns])
        QtWidgets.QMessageBox.information(self, "Export", "Wrote %s" % target)

    def open_replayer(self, hand_id, player):
        from HudReplayer import ReplayWindow
        win = ReplayWindow(self.db_path, hand_id, player)
        win.show()
        self._replay_windows = getattr(self, "_replay_windows", []) + [win]


class ImportDialog(object):
    """Run the real importer on a folder and report the counts, without blocking the GUI thread."""

    def __init__(self, db_path, folder, parent):
        self.db_path = db_path
        self.folder = folder
        self.parent = parent

    def run(self):
        try:
            import Importer
            config = Configuration.Config()
            settings = {}
            settings.update(config.get_db_parameters())
            settings.update(config.get_import_parameters())
            settings.update(config.get_default_paths())
            importer = Importer.Importer(False, settings, config, None)
            importer.setDropIndexes("don't drop")
            importer.setThreads(-1)
            importer.setCallHud(False)
            importer.addBulkImportImportFileOrDir(self.folder)
            stored, dups, partial, errors, seconds = importer.runImport()
            QtWidgets.QMessageBox.information(
                self.parent, "Import complete",
                "%d stored, %d duplicate, %d partial, %d errors in %.1fs"
                % (stored, dups, partial, errors, seconds))
        except Exception as exc:                              # noqa: BLE001 - shown on screen
            QtWidgets.QMessageBox.critical(self.parent, "Import failed",
                                           "%s: %s" % (exc.__class__.__name__, exc))


class PurgeDialog(QtWidgets.QDialog):
    """Delete hands by date range and/or stake, with a dry-run count before it commits.

    Tournament hands are protected by a tick box rather than swept up by the date range: a finished
    event's result lives in `Tourneys`/`TourneysPlayers`, which this dialog does not touch, so
    deleting only its hands would leave a grid row with no history behind it.
    """

    CHILD_TABLES = ("HandsActions", "HandsPlayers", "Boards", "HandsStove", "RawHands")

    def __init__(self, db_path, parent=None):
        QtWidgets.QDialog.__init__(self, parent)
        self.db_path = db_path
        self.setWindowTitle("Purge hands")
        self.use_dates = QtWidgets.QCheckBox("By date range")
        self.date_from = QtWidgets.QDateEdit(QtCore.QDate(datetime.date.today().year, 1, 1))
        self.date_from.setCalendarPopup(True)
        self.date_to = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.gametype = QtWidgets.QComboBox()
        self.gametype.addItem("All stakes", None)
        con = sqlite3.connect(db_path)
        try:
            gt = Gametypes(con)
            for gid, label in gt.combo_items():
                self.gametype.addItem(label, gid)
        finally:
            con.close()
        self.tourney = QtWidgets.QCheckBox("Include tournament hands")
        self.tourney.setToolTip("Off by default: a date-range purge is a cash-game cleanup, and "
                                "deleting an event's hands would leave its result row with no "
                                "history behind it.")
        self.preview = QtWidgets.QLabel("")
        preview_btn = QtWidgets.QPushButton("Preview count")
        preview_btn.clicked.connect(self.preview_count)
        purge_btn = QtWidgets.QPushButton("Purge...")
        purge_btn.clicked.connect(self.purge)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(preview_btn)
        buttons.addWidget(purge_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        grid = QtWidgets.QGridLayout()
        grid.addWidget(self.use_dates, 0, 0)
        grid.addWidget(self.date_from, 0, 1)
        grid.addWidget(self.date_to, 0, 2)
        grid.addWidget(QtWidgets.QLabel("Stake:"), 1, 0)
        grid.addWidget(self.gametype, 1, 1)
        grid.addWidget(self.tourney, 2, 0, 1, 3)
        box = QtWidgets.QVBoxLayout(self)
        box.addLayout(grid)
        box.addWidget(self.preview)
        box.addLayout(buttons)

    def _where(self):
        clauses, params = ([] if self.tourney.isChecked()
                           else ["tourneyId is null"], [])
        if self.use_dates.isChecked():
            clauses.append("date(startTime) between ? and ?")
            params += [self.date_from.date().toString("yyyy-MM-dd"),
                       self.date_to.date().toString("yyyy-MM-dd")]
        if self.gametype.currentData() is not None:
            clauses.append("gametypeId = ?")
            params.append(self.gametype.currentData())
        return (" where " + " and ".join(clauses)) if clauses else "", params

    def _hand_ids(self, con):
        where, params = self._where()
        return [r[0] for r in con.execute("select id from Hands" + where, params)]

    def preview_count(self):
        con = sqlite3.connect(self.db_path)
        try:
            ids = self._hand_ids(con)
            skipped = 0 if self.tourney.isChecked() else con.execute(
                "select count(*) from Hands where tourneyId is not null").fetchone()[0]
        finally:
            con.close()
        self.preview.setText("%d hands match. Purge will also remove their seats, actions, boards "
                             "and raw text.%s" % (
                                 len(ids),
                                 "" if not skipped else "  (%d tournament hands are protected by "
                                 "the tick box.)" % skipped))

    def purge(self):
        con = sqlite3.connect(self.db_path)
        try:
            ids = self._hand_ids(con)
        finally:
            con.close()
        if not ids:
            QtWidgets.QMessageBox.information(self, "Purge", "No hands match the filter.")
            return
        if QtWidgets.QMessageBox.warning(
                self, "Confirm purge",
                "Delete %d hands and all their rows? This cannot be undone from here -- take a "
                "Backup first if unsure." % len(ids),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        con = sqlite3.connect(self.db_path)
        try:
            con.isolation_level = None
            con.execute("begin")
            marks = ",".join("?" * len(ids))
            for table in self.CHILD_TABLES:
                try:
                    con.execute("delete from %s where handId in (%s)" % (table, marks), ids)
                except sqlite3.Error:
                    pass
            con.execute("delete from Hands where id in (%s)" % marks, ids)
            con.execute("commit")
            con.execute("vacuum")
            QtWidgets.QMessageBox.information(self, "Purge", "Deleted %d hands." % len(ids))
        except sqlite3.Error as exc:
            con.execute("rollback")
            QtWidgets.QMessageBox.critical(self, "Purge failed", str(exc))
        finally:
            con.close()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8", errors="replace")
    argv = argv if argv is not None else sys.argv[1:]
    parser = optparse.OptionParser(usage="%prog [options]")
    parser.add_option("--db", default=None, help="SQLite file to read (default: HUD_config.xml)")
    options, _args = parser.parse_args(argv)

    config = Configuration.Config()
    params = config.get_db_parameters()
    if params.get("db-backend") != Database.Database.SQLITE:
        print("This build reads SQLite only (DESIGN.md 5.1); config says %r"
              % params.get("db-server"))
        return 1
    db_path = options.db or os.path.join(config.dir_database, params["db-databaseName"])

    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont("Segoe UI", 10))
    window = MainWindow(db_path)
    window.show()
    window.apply_filters()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
