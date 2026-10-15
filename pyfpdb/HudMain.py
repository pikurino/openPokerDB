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
(By Stakes, By Game Type, Sessions by Day, Players, By Position, Population), a filter bar whose
"Apply" drives whichever tab is showing, a results grid with a column picker, and under the grid a
hand-history list with miniature cards that opens the table replayer on double-click. Every number in
a hand report comes from StatsEngine, which in turn reads the stat columns the importer already wrote
and replays HandsActions for the few the schema does not store -- nothing here is recomputed from a
premise about poker.

Two things are driven from the grid rather than from a control somewhere else, because that is where
the question arises: right-clicking a row plots that stake/game/day/player on the graph or filters
everything to it, and ``Settings > Settings...`` (or ``Settings > Theme``) chooses the look -- black,
HM2-like, PT4-like, HM3-like -- which repaints the window, the grid and the chart at once.

The graph is its **own window**, not a page of this one: it is read next to the grid, and swapping the
grid out to show it left no way back. Every cash grouping except Players counts the hero's seats only
(``Settings > Settings... > Graph & hero`` names that seat), which is what makes a right-clicked row
and the plot it opens agree -- unscoped, a By-Stakes row summed every seat at every hand, so it read
1068 hands and -$2.10 (the whole field's profit, i.e. the rake) for the 178 hands Hero actually played
there for -$10.70.

The sixth tab, **Population**, is the one the rest of the window is not: its rows are slices of
*decisions* made by everyone but the hero, cut by spot, street, bet size and flop/turn/river board
texture (``Metagame``), and the tab is a chooser over report *specs* rather than a fixed grid. What a
population report contains is designed in ``Design report...``, which draws both lists from Metagame's
registries, so a new spot or texture is a registry entry, never a GUI change. It shares the grid, the
model, the theme, the tooltips and the CSV export with the hand reports, and nothing else: a
population row is not a set of hands, so it neither plots nor drives the hand list.

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
import json
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
import Metagame
import HudTable
import HudThemes
import StatsEngine
from StatsEngine import Filters, Engine, STATS, report, position_label
from StatsEngine import (TOURNEY_COLUMNS, TOURNEY_BY_KEY, DEFAULT_TOURNEY_COLUMNS,
                         tournament_report)

STAT_BY_KEY = {k: (label, kind) for (k, label, kind, _n, _d) in STATS}
DEFAULT_COLUMNS = ["hands", "net", "bb100", "vpip", "pfr", "b3", "wtsd", "w$sd", "aggf", "aggq"]
# report tab -> (group_by, human title). 'position' aggregates every player's seat, HM2's "By Position".
# 'population' is not a StatsEngine grouping at all: it is the Metagame decision engine, whose rows
# are slices of *decisions* rather than slices of hands, so it carries its own report spec instead of
# a group key. Everything downstream branches on that token.
REPORTS = [
    ("By Stakes", ("gametype",)),
    ("By Game Type", ("game",)),
    ("Sessions by Day", ("day",)),
    ("Players", ("player",)),
    ("By Position", ("position",)),
    ("Population", ("population",)),
]
# Tournament mode has one grid: `tournament_report` already returns one row per event, so the
# cash grouping tabs (stake, position) have no meaning here and the tab box is disabled instead of
# being filled with a second, different list.
TOURNEY_TABS = ["Tournament Results"]
LINE_CODE = {"folds": "F", "checks": "X", "calls": "C", "bets": "B", "raises": "R",
             "completes": "C"}
# What a population report is measured over when the filter bar restricts nothing. It is not 'all
# hands': `Filters` leaves tournament hands out and `Metagame.Replay` leaves games without a flop out.
FALLBACK_SCOPE = "all cash hold'em hands"
POPULATION_SPLIT = {"field": "the field, not Hero", "hero": "Hero",
                    "both": "Hero and the field, in separate rows"}


def money(cents):
    v = int(round(cents or 0))
    return ("-$%.2f" if v < 0 else "$%.2f") % (abs(v) / 100.0)


def chips(cents):
    """A tournament stack delta: the column holds chips scaled by 100, so show the chip count."""
    v = int(round(cents or 0))
    return "{:,}".format(int(round(v / 100.0)))


def money_brush(value):
    """Green for a profit, red for a loss, and nothing for a zero.

    A $0.00 result is not a loss, and tinting it red made half the hand list read as a bleed. None
    means 'paint me with the theme's text colour', which is what the caller has to arrange.
    """
    if not value:
        return None
    pos, neg = HudThemes.money_colors()
    return QtGui.QBrush(pos if value > 0 else neg)


def _esc(text):
    """Escape a database-sourced string before putting it in a rich-text label.

    The graph summary is HTML so the bb figures can be coloured per line, and its scope and player
    fields come straight from `Sites`/`Hands` rows -- a hand-history author can name a seat
    '<img src=x>' and have it render in the panel.
    """
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
            if kind in ("money", "bb"):
                return money_brush(num)
        if role == QtCore.Qt.BackgroundRole and index.row() == 0:
            # Only the TOTAL banner gets the highlight. In a cash grid row 0 is simply the biggest
            # stake, and banding it read as if it were a total of the rows under it.
            if self.rows and self.rows[0]["group"][0] == "totals":
                return QtGui.QBrush(HudThemes.row_banner())
        return None

    def retheme(self):
        """Ask the views to re-read data(): the money colours and the TOTAL banner come from the
        theme and are handed out per role, so a theme change is only visible after a reset."""
        self.beginResetModel()
        self.endResetModel()


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


def fill_checkable(list_widget, entries, checked):
    """Rebuild a checkable list from (key, label, tooltip) triples, ticking the ones in `checked`.

    Signals are blocked because a rebuild fires ``itemChanged`` for every row: the tick states live
    on the dialog (a filtered list cannot hold the whole truth about a report's columns), so a
    rebuild must not read back as a user edit.
    """
    list_widget.blockSignals(True)
    list_widget.clear()
    for key, label, tooltip in entries:
        item = QtWidgets.QListWidgetItem(label)
        item.setData(QtCore.Qt.UserRole, key)
        item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
        item.setCheckState(QtCore.Qt.Checked if key in checked else QtCore.Qt.Unchecked)
        if tooltip:
            item.setToolTip(tooltip)
        list_widget.addItem(item)
    list_widget.blockSignals(False)


def select_key(list_widget, key):
    for i in range(list_widget.count()):
        if list_widget.item(i).data(QtCore.Qt.UserRole) == key:
            list_widget.setCurrentRow(i)
            return


class ReportBuilder(QtWidgets.QDialog):
    """The population report designer: pick the slices, pick the measures, watch the rows update.

    Nothing about the resulting report is fixed by this file. Both lists are drawn from Metagame's
    registries, so a new spot, board texture or measure appears here as a registry entry and costs no
    GUI code, and the preview runs the same ``Metagame.run`` that the tab runs -- a report cannot look
    right here and mean something else there.

    Two things the preview says out loud, because both silently produced a wrong-looking report
    before this dialog existed:

    * how many decisions it *left out*. Grouping by a river texture drops every decision that never
      saw a river, and an earlier draft filed those under one label, which made a 1,791-decision row
      swallow a flop-texture report whole.
    * when a report grouped *by* spot also ticks spot-specific columns. Each of those is non-zero
      only on its own row and reads zero everywhere else, which is how a wall of zeros gets believed.

    The filter bar is not duplicated here: the design inherits it, the scope line says what it
    inherited, and ``where`` is stripped before saving so a saved report is a shape rather than
    yesterday's date range.
    """

    MAX_PREVIEW_ROWS = 15
    FAMILIES = [("any spot", "Any spot -- reads the same way on every row"),
                ("decisions", "How many decisions each named spot got"),
                ("reaction", "How a named spot is answered"),
                ("outcome", "What a named spot won"),
                ("special", "Totals and check-raises")]

    def __init__(self, spec, db_path, hero, parent=None):
        QtWidgets.QDialog.__init__(self, parent)
        spec = dict(spec or {})
        self.db_path = db_path
        self.hero = hero
        self.scope_text = ""
        self.saved_path = None
        self.setWindowTitle("Design a population report")
        self.resize(1100, 730)
        measures = []
        for key in spec.get("measures") or []:
            if key in Metagame.MEASURE_KIND and key not in measures:
                measures.append(key)
        # Ticked keys first, in the report's own order, so reopening a design shows its columns left
        # to right rather than re-sorted into the registry.
        self.measure_checked = list(measures)
        self.measure_order = measures + [k for (k, _l, _f, _kind) in Metagame.MEASURES
                                         if k not in measures]
        dims = [g for g in spec.get("group_by") or [] if g in Metagame.DIMENSION_LABEL]
        self.dim_checked = list(dims)
        self.dim_order = dims + [k for (k, _l) in Metagame.DIMENSIONS if k not in dims]
        self._build(spec)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.run_preview)
        # Filling the two lists is also the first preview -- `_refresh_lists` ends by running it. Open
        # the dialog on a report and it must already show that report's rows, not an empty table under
        # a ticked set of boxes.
        self._refresh_lists()

    # -- construction ---------------------------------------------------------------

    def _build(self, spec):
        outer = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Report name"))
        self.name = QtWidgets.QLineEdit(str(spec.get("name") or "Untitled report"))
        self.name.setToolTip("The name the tab's report chooser lists this report under")
        top.addWidget(self.name, 1)
        self.scope = QtWidgets.QLabel("Filter scope: %s" % (self.scope_text or FALLBACK_SCOPE))
        self.scope.setWordWrap(False)
        self.scope.setToolTip("The report is designed over whatever the filter bar currently holds; "
                              "that is not saved into the report.")
        top.addWidget(self.scope, 1)
        outer.addLayout(top)

        pair = QtWidgets.QHBoxLayout()
        outer.addLayout(pair)

        left = QtWidgets.QGroupBox("Group each row by (top to bottom)")
        left_box = QtWidgets.QVBoxLayout(left)
        self.dims = QtWidgets.QListWidget()
        self.dims.itemChanged.connect(self._dims_toggled)
        left_box.addWidget(self.dims)
        left_box.addLayout(self._mover(self.dims, -1, "Move up"))
        pair.addWidget(left, 1)

        right = QtWidgets.QGroupBox("Measure columns to show")
        right_box = QtWidgets.QVBoxLayout(right)
        pick = QtWidgets.QHBoxLayout()
        self.family = QtWidgets.QComboBox()
        self.family.addItem("Every measure family", None)
        for key, label in self.FAMILIES:
            self.family.addItem(label, key)
        self.family.currentIndexChanged.connect(self._refresh_lists)
        pick.addWidget(self.family, 1)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("filter by word")
        self.search.textChanged.connect(self._refresh_lists)
        pick.addWidget(self.search, 1)
        right_box.addLayout(pick)
        self.measures = QtWidgets.QListWidget()
        self.measures.itemChanged.connect(self._measures_toggled)
        right_box.addWidget(self.measures)
        move = self._mover(self.measures, -1, "Move up")
        untick = QtWidgets.QPushButton("Untick all")
        untick.setToolTip("Clear every column so a fresh set can be ticked from the filtered list")
        untick.clicked.connect(self._untick_measures)
        move.addWidget(untick)
        right_box.addLayout(move)
        pair.addWidget(right, 2)

        controls = QtWidgets.QHBoxLayout()
        outer.addLayout(controls)
        controls.addWidget(QtWidgets.QLabel("Minimum decisions for a rate"))
        self.min_n = QtWidgets.QSpinBox()
        self.min_n.setRange(1, 1000000)
        self.min_n.setValue(int(spec.get("min_n") or 15))
        self.min_n.setToolTip("Below this many decisions in a row, a rate cell reads 'not yet data' "
                              "instead of a percentage that cannot mean anything.")
        self.min_n.valueChanged.connect(self._changed)
        controls.addWidget(self.min_n)
        controls.addWidget(QtWidgets.QLabel("Count"))
        self.split = QtWidgets.QComboBox()
        for data, label in (("field", "the field (everyone but Hero)"),
                            ("hero", "Hero only"),
                            ("both", "Hero and the field, as separate rows")):
            self.split.addItem(label, data)
        self.split.setCurrentIndex(max(0, self.split.findData(spec.get("split") or "field")))
        self.split.currentIndexChanged.connect(self._changed)
        controls.addWidget(self.split)
        controls.addStretch(1)
        self.saved_note = QtWidgets.QLabel("")
        self.saved_note.setToolTip("Where the last save wrote this report")
        controls.addWidget(self.saved_note)
        outer.addLayout(controls)

        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        self.hint.setVisible(False)
        outer.addWidget(self.hint)

        preview = QtWidgets.QGroupBox("Preview")
        preview_box = QtWidgets.QVBoxLayout(preview)
        self.summary = QtWidgets.QLabel("Nothing to preview yet.")
        self.summary.setWordWrap(True)
        preview_box.addWidget(self.summary)
        self.pane = QtWidgets.QStackedWidget()
        self.table = QtWidgets.QTableWidget()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.pane.addWidget(self.table)
        self.json_view = QtWidgets.QPlainTextEdit()
        self.json_view.setReadOnly(True)
        self.pane.addWidget(self.json_view)
        preview_box.addWidget(self.pane, 1)
        row = QtWidgets.QHBoxLayout()
        self.json_btn = QtWidgets.QPushButton("Show the report spec as JSON")
        self.json_btn.setCheckable(True)
        self.json_btn.toggled.connect(lambda on: self.pane.setCurrentIndex(1 if on else 0))
        row.addWidget(self.json_btn)
        row.addStretch(1)
        preview_box.addLayout(row)
        outer.addWidget(preview, 1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Apply to the tab")
        save = QtWidgets.QPushButton("Save as report...")
        save.setToolTip("Write this design to %s so the tab lists it after a restart"
                        % Metagame.REPORT_DIR)
        save.clicked.connect(self.save_report)
        buttons.addButton(save, QtWidgets.QDialogButtonBox.ActionRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.name.textChanged.connect(self._changed)

    def _mover(self, list_widget, first_delta, first_label):
        """Up/down buttons for one list: column and grouping order both mean something."""
        row = QtWidgets.QHBoxLayout()
        for delta, label in ((first_delta, first_label), (+1, "Move down")):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda _c=False, w=list_widget, d=delta: self._move(w, d))
            row.addWidget(button)
        row.addStretch(1)
        return row

    # -- the two lists --------------------------------------------------------------

    def _refresh_lists(self):
        family = self.family.currentData()
        needle = self.search.text().strip().lower()
        entries = []
        for key in self.measure_order:
            label = Metagame.MEASURE_LABEL[key]
            if family and Metagame.MEASURE_FAMILY[key] != family:
                continue
            if needle and needle not in label.lower() and needle not in key.lower():
                continue
            # The raw key in the tooltip: two labels can read alike ("Pf-faced-open: fold" and
            # "Pf-faced-3bet: fold") and a report is saved by key, not by label.
            entries.append((key, label, key))
        fill_checkable(self.measures, entries, self.measure_checked)
        fill_checkable(self.dims,
                       [(k, Metagame.DIMENSION_LABEL[k], k) for k in self.dim_order],
                       self.dim_checked)
        self.run_preview()

    def _dims_toggled(self, item):
        self._toggle(self.dim_checked, item)

    def _measures_toggled(self, item):
        self._toggle(self.measure_checked, item)

    @staticmethod
    def _toggle(target, item):
        key = item.data(QtCore.Qt.UserRole)
        if item.checkState() == QtCore.Qt.Checked:
            if key not in target:
                target.append(key)
        elif key in target:
            target.remove(key)

    def _untick_measures(self):
        self.measure_checked = []
        self._refresh_lists()

    def _move(self, list_widget, delta):
        item = list_widget.currentItem()
        if item is None:
            return
        key = item.data(QtCore.Qt.UserRole)
        order = self.dim_order if list_widget is self.dims else self.measure_order
        if key not in order:
            return
        i = order.index(key)
        j = i + delta
        if not 0 <= j < len(order):
            return
        order[i], order[j] = order[j], order[i]
        self._refresh_lists()
        select_key(list_widget, key)

    def _changed(self, *_args):
        """Any control moved: re-preview, but only once he has stopped clicking."""
        self.timer.start()

    # -- the spec and the preview ---------------------------------------------------

    def set_scope(self, text):
        """Name the hands the design is measured over. The filter bar owns that, not the spec."""
        self.scope_text = text or FALLBACK_SCOPE
        self.scope.setText("Filter scope: %s" % self.scope_text)

    def current_spec(self):
        """The design, as a shape. Which hands it will be measured over is the filter bar's job, so
        no `where` comes out of here -- a saved report must not carry yesterday's date range.
        """
        group_by = [g for g in self.dim_order if g in set(self.dim_checked)]
        measures = [m for m in self.measure_order if m in set(self.measure_checked)]
        return {"name": self.name.text().strip() or "Untitled report",
                "group_by": group_by,
                "measures": measures,
                "min_n": int(self.min_n.value()),
                "split": self.split.currentData()}

    def save_report(self):
        spec = self.current_spec()
        if not spec["measures"]:
            QtWidgets.QMessageBox.warning(self, "Nothing to save",
                                          "Tick at least one measure column first.")
            return
        try:
            self.saved_path = Metagame.save_spec(spec)
        except (IOError, OSError) as exc:
            QtWidgets.QMessageBox.critical(self, "Could not save", str(exc))
            return
        self.saved_note.setText("Saved: %s" % self.saved_note_text())
        self.run_preview()

    def saved_note_text(self):
        return os.path.basename(self.saved_path or "")

    def run_preview(self):
        spec = self.current_spec()
        self.json_view.setPlainText(json.dumps(spec, indent=1, sort_keys=True))
        if not spec["measures"]:
            self._clear_preview("Tick at least one measure column to get a report.")
            return
        if not os.path.exists(self.db_path):
            self._clear_preview("No database at %s to preview against." % self.db_path)
            return
        con = sqlite3.connect(self.db_path)
        try:
            columns, rows, meta = Metagame.run(con, spec, self.hero)
        except sqlite3.Error as exc:
            self._clear_preview("Preview failed: %s" % exc)
            return
        finally:
            con.close()
        if "all__n" in spec["measures"]:
            # The runner always carries the row's own decision count, and `all__n` is that same
            # number ticked as a column. Two columns of one number under two labels is how a report
            # starts to look like it measured two things.
            columns = [column for column in columns if column[1] != "n"]
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels([label for label, _key in columns])
        shown = rows[:self.MAX_PREVIEW_ROWS]
        self.table.setRowCount(len(shown))
        for r, row in enumerate(shown):
            for c, (_label, key) in enumerate(columns):
                if key in Metagame.MEASURE_KIND:
                    text = Metagame.cell_text(Metagame.MEASURE_KIND[key], row[key])
                    align = QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                elif key == "n":
                    text, align = "%d" % row["n"], QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                else:
                    # A grouping column. Its value is the row's label, not a measurement, and the
                    # runner returns both kinds in one `columns` list.
                    text, align = str(row[key]), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
                item = QtWidgets.QTableWidgetItem(text)
                item.setTextAlignment(align)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        words = "%d decisions matched the filter, %d grouped into %d rows" % (
            meta["decisions"], meta["grouped"], len(rows))
        if meta["out_of_scope"]:
            words += ", %d left out because they never reached the street this report groups by" % (
                meta["out_of_scope"])
        if len(rows) > len(shown):
            words += "  (showing the %d biggest; the tab lists all %d)" % (len(shown), len(rows))
        self.summary.setText(words)
        self._spot_warning(spec)

    def _clear_preview(self, message):
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.summary.setText(message)
        self.hint.setVisible(False)

    def _spot_warning(self, spec):
        """Say so when a report grouped by spot is measured with spot-specific columns.

        That combination is not an error -- it is how a spot census is read -- but on every other row
        those cells are legitimately zero, and a grid of zeros next to a heading that says 'Spot' is
        how a report gets believed that says nothing.
        """
        named = [m for m in spec["measures"] if m.rsplit("__", 1)[0] in Metagame.SPOT_LABELS]
        if "spot" in spec["group_by"] and named:
            head = ("One ticked column belongs" if len(named) == 1
                    else "%d ticked columns belong" % len(named))
            self.hint.setText(
                "%s to one named spot, and this report groups rows BY spot, so each is non-zero only "
                "on its own row and reads 0 on every other one. The 'Any spot' family asks the same "
                "question of whatever a row happens to be." % head)
            self.hint.setVisible(True)
        else:
            self.hint.setVisible(False)


class HistoryList(QtWidgets.QWidget):
    """The hand list under the grid: time, mini cards, line, board. Double-click replays the hand."""

    hand_activated = QtCore.Signal(int, str)          # hand_id, player name

    def __init__(self, db_path, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.db_path = db_path
        self.title = QtWidgets.QLabel("Hand history")
        # Object name, not an inline stylesheet: the app stylesheet is what a theme change rewrites,
        # and an inline call would win over it and freeze the old colours in place.
        self.title.setObjectName("historyTitle")
        self.page = QtWidgets.QComboBox()
        for text, n in (("250", 250), ("1,000", 1000), ("10,000", 10000), ("All", 100000)):
            self.page.addItem(text, n)
        self.page.setToolTip("Presets for the hands-count box next to it")
        self.page.currentIndexChanged.connect(self._preset)
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(1, 100000)
        self.count.setValue(250)
        self.count.setSuffix(" hands")
        self.count.setToolTip("How many hands to list below the grid -- HM2's page-size box")
        self.count.valueChanged.connect(self.reload)
        # Declared after `self.count` because the first sync reads it; without this the two widgets
        # disagree the moment a number arrives that is not one of the presets (a screenshot caught
        # `Show [10,000 hands]` next to a preset box still reading `250`).
        self.count.valueChanged.connect(self._sync_preset)
        self._sync_preset(self.count.value())
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
        self._profits = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(QtWidgets.QLabel("Show"))
        top.addWidget(self.count)
        top.addWidget(QtWidgets.QLabel("preset:"))
        top.addWidget(self.page)
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

    def _preset(self, _index):
        """The preset combo drives the spin box, so there is one number that decides the list."""
        self.count.setValue(self.page.currentData())

    def _sync_preset(self, value):
        """Highlight the preset the spin is on, and clear the box when it is on none of them.

        The spin is the number that actually limits the query, so the combo has to follow it rather
        than sit on a stale default; an unmatched index means "some number you typed".
        """
        self.page.blockSignals(True)
        self.page.setCurrentIndex(self.page.findData(int(value)))
        self.page.blockSignals(False)

    def set_hands_count(self, value):
        self.count.setValue(max(1, min(int(value), self.count.maximum())))

    def _limit(self):
        return self.count.value()

    def reload(self):
        if not os.path.exists(self.db_path):
            return
        con = sqlite3.connect(self.db_path)
        self.view.setRowCount(0)
        self._hand_ids = []
        self._profits = []
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
                brush = money_brush(row[10])
                if brush is not None:
                    result.setForeground(brush)
                self.view.setItem(r, 4, result)
                self._hand_ids.append(row[0])
                self._profits.append(row[10] or 0)
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

    def retheme(self):
        """Re-tint the result column. The rows have not changed, so do not re-query them."""
        plain = QtGui.QBrush(self.view.palette().color(QtGui.QPalette.Text))
        for row, profit in enumerate(self._profits):
            item = self.view.item(row, 4)
            if item is not None:
                item.setForeground(money_brush(profit) or plain)

    def _open(self, _index):
        row = self.view.currentRow()
        if 0 <= row < len(self._hand_ids):
            self.hand_activated.emit(self._hand_ids[row], self.player)


class GraphView(QtWidgets.QWidget):
    """Results graph: every cumulative money line a Holdem-Manager-style report plots, in bb.

    The lines are Net Won, All-In EV, Showdown, Non-Showdown, the won and lost halves of both, and
    applied rake -- the same counters the grid's money columns read, accumulated hand by hand, so
    the last point of a line is the number in the TOTAL row above it. Every line has a tick box
    with a colour swatch, because 'which of these do I want' changes while looking at the answer;
    the three preset buttons pick the combinations that actually answer a question and 'Everything'
    is how you find out which line you were not reading.

    The x axis toggles between hand number and days elapsed: by hand a downswing is visible, by day
    you can see whether the month is going anywhere. Day mode plots one point per day -- that day's
    last hand -- because the intermediate points of a cumulative walk are the hand axis' business.

    Two more things live here because they only make sense next to the plot: the All-In EV line
    comes out of `HandsPlayers.allInEV`, which is empty until `AllInEv.fill_column` has run, so the
    widget that draws it carries the button that fills it; and `scope` is set by MainWindow when a
    grid row is right-clicked into the graph, because two graphs of two different stakes look
    identical unless the window says which one you are looking at.
    """

    recompute_ev = QtCore.Signal()           # MainWindow owns the database, not this widget
    options_changed = QtCore.Signal()        # ticks and x-axis: MainWindow persists them

    LINES = [("net", "Net Won", False),
             ("ev", "All-In EV", True),
             ("sd", "Showdown", False),
             ("nsd", "Non-Showdown", False),
             ("sd_won", "Won at SD", False),
             ("sd_lost", "Lost at SD", False),
             ("nsd_won", "Won non-SD", False),
             ("nsd_lost", "Lost non-SD", False),
             ("rake", "Rake", True)]
    PRESETS = [("Core", ("net", "ev", "sd", "nsd")),
               ("Showdown split", ("net", "sd_won", "sd_lost", "nsd_won", "nsd_lost")),
               ("Everything", tuple(key for key, _label, _dash in LINES))]
    LINE_TIPS = {
        "net": "Running total of HandsPlayers.totalProfit in big blinds.",
        "ev": "Same walk with an all-in seat's result replaced by HandsPlayers.allInEV.",
        "sd": "Only hands where this seat saw the showdown.",
        "nsd": "Only hands where this seat never saw the showdown, i.e. everyone folded.",
        "sd_won": "The showdown hands, positives only.",
        "sd_lost": "The showdown hands, negatives only.",
        "nsd_won": "The never-showdown hands, positives only.",
        "nsd_lost": "The never-showdown hands, negatives only.",
        "rake": "Running total of HandsPlayers.rake. Measured on the live corpus: 115 of 2070 "
                "seats carry a nonzero rake and 114 of those won the hand, so the site puts a "
                "hand's rake on its winner -- with a player name in the box this is the rake on "
                "that player's winning seats, not the fee the table took from him.",
    }

    def __init__(self, db_path, hero="Hero", parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.db_path = db_path
        self.hero = hero
        self.series = []
        self.scope = ""
        self._loading = False
        self.chart = QtCharts.QChart()
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
        # Themed through the app stylesheet by object name, never by an inline setStyleSheet: an
        # inline call beats the app sheet, and a theme change would then do nothing to this panel.
        self.summary.setObjectName("graphSummary")
        self.summary.setReadOnly(True)
        # Both bounds, not just the maximum: with only a maximum the layout squeezes the panel to a
        # third of its width and every line of the summary wraps into a column of broken phrases.
        self.summary.setMinimumWidth(300)
        self.summary.setMaximumWidth(320)
        self.player = QtWidgets.QLineEdit(hero)
        self.player.setPlaceholderText("player name for the graph")
        self.player.setToolTip("Whose seats the lines add up. Left as the hero name, the plot is the "
                               "same set of hands the By-Stakes, By-Day and By-Position rows count.")
        self.player.returnPressed.connect(self.reload)
        self.xaxis_box = QtWidgets.QComboBox()
        for text, data in (("hands", "hands"), ("days", "days")):
            self.xaxis_box.addItem("x axis: " + text, data)
        self.xaxis_box.setToolTip("Cumulative bb plotted against hand number, or against days "
                                  "elapsed since the first hand (one point per day).")
        self.xaxis_box.currentIndexChanged.connect(self._xaxis_changed)
        self.toggles = {}
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Graph player:"))
        top.addWidget(self.player)
        top.addWidget(self.xaxis_box)
        for label, keys in self.PRESETS:
            button = QtWidgets.QPushButton(label)
            button.setToolTip("Tick: " + ", ".join(self.label_of(k) for k in keys))
            button.clicked.connect(lambda _checked=False, ks=keys: self.set_lines(ks))
            top.addWidget(button)
        self.ev_button = QtWidgets.QPushButton("Compute all-in EV")
        self.ev_button.setToolTip("Fill HandsPlayers.allInEV from the hand histories, then redraw. "
                                  "Needed once per database, and again after an import.")
        self.ev_button.clicked.connect(self.recompute_ev)
        top.addWidget(self.ev_button)
        top.addStretch(1)
        # The nine tick boxes get their own row rather than being squeezed next to the player box:
        # a swatch and a label have to be readable to be tickable, and this is the control he asked
        # for ('everything holdem manager plots'), so it is the one thing not allowed to truncate.
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QtWidgets.QLabel("Lines:"))
        for key, label, _dash in self.LINES:
            box = QtWidgets.QCheckBox(label)
            box.setObjectName("linePicker")
            box.setIcon(self._swatch(HudThemes.SERIES_COLORS[key]))
            box.setToolTip(self.LINE_TIPS[key])
            box.toggled.connect(self._line_toggled)
            self.toggles[key] = box
            row.addWidget(box)
        row.addStretch(1)
        box = QtWidgets.QHBoxLayout(self)
        left = QtWidgets.QVBoxLayout()
        left.addLayout(top)
        left.addLayout(row)
        left.addWidget(self.scene)
        box.addLayout(left, 1)
        box.addWidget(self.summary)
        self.apply_theme()
        self.set_lines(HudThemes.DEFAULTS["graph_lines"])

    # -- configuration --------------------------------------------------------------

    @staticmethod
    def _swatch(color):
        """A filled square for a tick box. A swatch survives a theme change; coloured checkbox text
        does not, because the app stylesheet's `color` beats any palette entry."""
        pixmap = QtGui.QPixmap(12, 12)
        pixmap.fill(QtGui.QColor(color))
        return QtGui.QIcon(pixmap)

    def label_of(self, key):
        return dict((k, label) for k, label, _dash in self.LINES)[key]

    def checked_lines(self):
        return [key for key, _label, _dash in self.LINES if self.toggles[key].isChecked()]

    def set_lines(self, keys):
        wanted = set(keys)
        self._loading = True
        for key, box in self.toggles.items():
            box.setChecked(key in wanted)
        self._loading = False
        self._draw_current()
        self.options_changed.emit()

    def set_xaxis(self, mode):
        index = self.xaxis_box.findData(mode)
        self.xaxis_box.setCurrentIndex(index if index >= 0 else 0)

    def options(self):
        """The graph settings the tick boxes own. The player box is deliberately absent: it is a
        per-plot choice ('show me this villain's line'), while `hero` is an identity the whole report
        grid is scoped by, and letting a name typed into one quietly re-scope the other is how a
        'By Stakes' row stops meaning 'my stakes'."""
        return {"graph_lines": self.checked_lines(), "graph_x": self.xaxis_box.currentData()}

    def apply_options(self, settings):
        self._loading = True
        self.set_xaxis(settings.get("graph_x", "hands"))
        self._loading = False
        self.set_hero(settings.get("hero", self.hero))
        self.set_lines(settings.get("graph_lines") or HudThemes.DEFAULTS["graph_lines"])

    def set_hero(self, name):
        """Move the hero identity over, taking the box with it only if he had not typed a name there."""
        name = name or "Hero"
        if self.player.text().strip() in ("", self.hero):
            self.player.setText(name)
        self.hero = name

    def _line_toggled(self, _checked):
        self._draw_current()
        if not self._loading:
            self.options_changed.emit()

    def _xaxis_changed(self, _index):
        self._draw_current()
        if not self._loading:
            self.options_changed.emit()

    def apply_theme(self):
        """Repaint the chart's own colours from the current theme. Series colours deliberately do
        not come from the theme: the same stake keeps its line colour across themes, so a screenshot
        taken before a theme change still means the same thing."""
        c = HudThemes.chart_colors()
        background = QtGui.QBrush(QtGui.QColor(c["background"]))
        text = QtGui.QBrush(QtGui.QColor(c["text"]))
        self.chart.setBackgroundBrush(background)
        self.chart.setBackgroundVisible(True)
        self.chart.setPlotAreaBackgroundBrush(background)
        self.chart.setPlotAreaBackgroundVisible(True)
        self.chart.setTitleBrush(text)
        self.legend.setLabelColor(QtGui.QColor(c["text"]))
        for axis in (self.axis_x, self.axis_y):
            axis.setLabelsColor(QtGui.QColor(c["text"]))
            axis.setTitleBrush(text)
            axis.setGridLineColor(QtGui.QColor(c["grid"]))
            # QValueAxis has no base-line setter in PySide6 6.12; the axis line itself is the pen.
            axis.setLinePenColor(QtGui.QColor(c["text"]))

    # -- drawing --------------------------------------------------------------------

    def reload(self, filters=None, fallback_player=None, scope=None):
        if scope is not None:
            self.scope = scope
        name = self.player.text().strip() or fallback_player or self.hero
        con = sqlite3.connect(self.db_path)
        try:
            self.series = Engine(con).series(filters or Filters(), player=name)
        finally:
            con.close()
        self._draw(name)

    def _draw_current(self):
        """Redraw with whatever the tick boxes say, without re-querying."""
        self._draw(self.player.text().strip() or self.hero)

    def _draw(self, name):
        """Redraw the visible lines. The axes stay attached; only the series are rebuilt."""
        self.chart.removeAllSeries()
        pts = self.series
        keys = [entry for entry in self.LINES if self.toggles[entry[0]].isChecked()]
        self.apply_theme()
        if not pts or not keys:
            self.chart.setTitle("Results graph")
            self.summary.setPlainText("No hands match the current filters." if not pts
                                      else "Tick at least one line.")
            self.axis_x.setRange(0, 1)
            self.axis_y.setRange(-1, 1)
            return
        # One point per day in day mode: `pts` is in time order, so remembering the last hand seen
        # for each startTime prefix gives that day's closing total. The date text is the same
        # 10-character prefix `Sessions by Day` groups on, so the two views cannot disagree.
        if self.xaxis_box.currentData() == "days":
            closing = {}
            for p in pts:
                closing[str(p["t"])[:10]] = p
            days = sorted(closing)
            plotted = [closing[d] for d in days]
            xs = list(range(len(days)))
            span, title = max(len(days) - 1, 1), "Days"
        else:
            plotted = pts
            xs = [p["n"] for p in pts]
            span, title = pts[-1]["n"], "Hands"
        self.axis_x.setTitleText(title)
        # Set the ranges before anything attaches: QValueAxis recomputes its tick interval when a
        # series is attached, so an interval set afterwards is silently thrown away and the axis
        # divides the range into its default 5 pieces -- which is how the hands axis got labelled
        # '86.3' and the bb axis '-125.0'.
        values = [p[key] for p in plotted for key, _l, _d in keys]
        lo, hi = min(values), max(values)
        y_step = _nice_step(max(hi - lo, abs(hi), abs(lo), 1), 5)
        self.axis_x.setRange(0, span)
        self.axis_x.setTickType(self.ticks_dynamic)
        self.axis_x.setTickInterval(_nice_step(span, 5))
        self.axis_y.setRange(math.floor(lo / y_step) * y_step, math.ceil(hi / y_step) * y_step)
        self.axis_y.setTickType(self.ticks_dynamic)
        self.axis_y.setTickInterval(y_step)
        self.axis_x.setLabelFormat("%d")
        self.axis_y.setLabelFormat("%.0f" if y_step >= 1 else "%g")
        for key, label, dash in keys:
            series = QtCharts.QLineSeries()
            series.setName(label)
            for x, p in zip(xs, plotted):
                series.append(x, p[key])
            pen = QtGui.QPen(QtGui.QColor(HudThemes.SERIES_COLORS[key]))
            pen.setWidth(2)
            if dash:
                pen.setStyle(QtCore.Qt.DashLine)
            series.setPen(pen)
            self.chart.addSeries(series)
            series.attachAxis(self.axis_x)
            series.attachAxis(self.axis_y)
        last = pts[-1]
        scope = self.scope or "everything the filters allow"
        self.chart.setTitle("Results -- %s (%s)" % (self.scope or "filtered hands", name))
        # Rich text, because a proportional font cannot line a plain-text column up, and the whole
        # point of this panel is reading six numbers down the side of a plot at a glance. Player
        # names and stake labels come out of the database, so they are escaped before they go in.
        pos, neg = HudThemes.money_colors()
        plain = QtGui.QColor(HudThemes.theme()["text"])
        rows = []
        for key, label, _dash in self.LINES:
            if not self.toggles[key].isChecked():
                continue
            v = last[key]
            # Rake accumulates money the site took, so a rising green line would read as a profit.
            color = plain if key == "rake" or not v else pos if v > 0 else neg
            rows.append("<tr><td>%s</td><td align='right' style='color:%s'>%+.1f bb</td></tr>"
                        % (_esc(label), color.name(), v))
        html = ("<b>%s</b><br>%s<br><b>Hands:</b> %d &nbsp; <b>Days:</b> %d<br><table width='100%%'>%s"
                "</table><br>" % (_esc("Results -- %s" % scope), _esc(name), last["n"],
                                  len({str(p["t"])[:10] for p in pts}), "".join(rows)))
        ev_hands = last["ev_hands"]
        if not ev_hands:
            html += ("No seat in this range has an all-in EV yet, so that line is the net line. "
                     "Press 'Compute all-in EV' to fill it.")
        else:
            html += ("Variance removed by the EV line: <b>%+.1f bb</b> (%d all-in hands of %d)"
                     % (last["ev"] - last["net"], ev_hands, last["n"]))
        if self.xaxis_box.currentData() == "days":
            html += "<br><br>Day mode: one point per calendar day, at that day's closing total."
        self.summary.setHtml(html)


class GraphWindow(QtWidgets.QMainWindow):
    """The results graph in its own window, opened beside the report rather than instead of it.

    It used to be the second page of the main window's stack, so plotting a stake took the grid away
    and left no visible way back to it -- the report and the plot are two things he reads together,
    not two states of one window. Closing this one costs nothing: the main window keeps the object and
    re-shows it, so the tick boxes and the typed player name survive an open-close cycle.
    """

    def __init__(self, db_path, hero, settings, parent=None):
        QtWidgets.QMainWindow.__init__(self, parent)
        self.setWindowTitle("Results graph -- OpenPokerDB")
        self.resize(1180, 720)
        self.graph = GraphView(db_path, hero)
        self.graph.apply_options(settings)
        self.setCentralWidget(self.graph)


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
        self._cash_tab = 0
        self.tourney_columns = list(DEFAULT_TOURNEY_COLUMNS)
        self._selected_tourney = None
        # The population tab's own state: which report is in force, and what the grid is currently
        # showing. `pop_columns` is the measure list plus the row's decision count, which the tab
        # always puts first so a rate can never be read without its denominator in view.
        self.pop_spec = None
        self.pop_specs = []
        self.pop_columns = []
        self.pop_fields = {}
        # One JSON file holds the look and the graph/list defaults, and it is read before the menu
        # is built because the theme submenu ticks the entry that is in force.
        self.settings = HudThemes.load_settings()
        # The seat that is him. Every cash grouping except By Players counts this name's hands, so a
        # By-Stakes row and the plot it opens describe the same set of hands (see Engine.aggregate).
        self.hero = self.settings.get("hero") or "Hero"
        # Created on first use: the graph lives in its own window, and opening the main window must
        # not drag a chart, a second palette and a second set of axis objects into existence.
        self.graph_window = None

        self._build_menu()
        self._build_toolbar()
        self._build_filters()
        self._build_body()
        self.history.set_hands_count(self.settings["history_hands"])
        self.history.count.valueChanged.connect(
            lambda value: self._remember("history_hands", int(value)))
        self.statusBar().showMessage("%s  |  theme: %s, settings: %s" % (
            self._db_summary(), HudThemes.theme()["label"], HudThemes.SETTINGS_FILE))

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
        # Settings is its own top-level menu, not an item under Database: it is where the look and
        # the graph defaults live, and hiding a theme switch behind a database menu is how nobody
        # finds it. The theme list is duplicated in the menu bar as a submenu so a switch is two
        # clicks from anywhere, and the dialog is where the rest of the settings are.
        settings_menu = self.menuBar().addMenu("&Settings")
        open_settings = QtGui.QAction("&Settings... (theme, graph lines, list size)", self)
        open_settings.triggered.connect(self.open_settings)
        settings_menu.addAction(open_settings)
        settings_menu.addSeparator()
        theme_menu = settings_menu.addMenu("&Theme")
        self.theme_actions = []
        group = QtGui.QActionGroup(self)
        for key, label in HudThemes.names():
            action = QtGui.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(key == self.settings["theme"])
            action.triggered.connect(lambda _c=False, k=key: self.set_theme(k))
            group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions.append((key, action))
        help_menu = self.menuBar().addMenu("&Help")
        about = QtGui.QAction("About", self)
        about.triggered.connect(lambda: QtWidgets.QMessageBox.about(
            self, "OpenPokerDB",
            "Cash and tournament reports over the fpdb database. Numbers come from StatsEngine. "
            "Right-click a report row to plot or filter that stake, game, day or player."))
        help_menu.addAction(about)

    def _build_toolbar(self):
        bar = QtWidgets.QToolBar("Mode")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.cash_btn = QtWidgets.QPushButton("Cash")
        self.tourney_btn = QtWidgets.QPushButton("Tournament")
        for button in (self.cash_btn, self.tourney_btn):
            button.setCheckable(True)
            # Named, not styled inline: the checked look comes from the app stylesheet, so it follows
            # the theme instead of freezing the old blue in place.
            button.setObjectName("modeButton")
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
        self.graph_btn.setToolTip("Open the bb results graph in its own window, plotting the hands the "
                                  "grid is counting. The cash graph is a cash measure; a tournament's "
                                  "blinds change every level.")
        self.graph_btn.clicked.connect(self.open_graph)
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
        # The tab on screen belongs to the mode being left. Tournament mode has one tab, so a cash
        # index is meaningless there and has to be kept for the way back -- a round trip through
        # Tournament that dumped a Population (or Players) report back on By Stakes is the "no way to
        # get my view back" complaint this answers.
        if self.mode != "tour":
            self._cash_tab = max(self.tabs_bar.currentIndex(), 0)
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
            self.tabs_bar.setCurrentIndex(min(self._cash_tab, len(REPORTS) - 1))
            self.tabs_bar.setEnabled(True)
        self.tabs_bar.blockSignals(False)
        self.stakes.setEnabled(not tour)
        self.stakes.setToolTip("" if not tour else "Stakes are a cash filter; tournaments use "
                                                   "the buy-in shown in each row.")
        self.min_hands.setEnabled(not tour)
        self.min_hands.setToolTip("" if not tour else "Tournament rows are events, not hands.")
        self.graph_btn.setEnabled(not tour)
        self._selected_tourney = None
        self._update_population_bar()
        self.apply_filters()

    def _current_columns(self):
        if self.current_group == ("population",):
            return self.pop_columns
        return self.tourney_columns if self.mode == "tour" else self.current_columns

    def _current_fields(self):
        if self.current_group == ("population",):
            return self.pop_fields
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
        apply_btn.setObjectName("applyButton")
        apply_btn.clicked.connect(self.apply_filters)
        row.addWidget(apply_btn)
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_filters)
        row.addWidget(clear_btn)
        row.addStretch(1)
        self.filter_panel = panel

    def _build_population_bar(self):
        """The Population tab's control strip: which report, and how to change it.

        It sits directly above the grid rather than in a dialog or the menu bar because every control
        here edits the table on the screen: picking a report, tuning the minimum sample, switching
        between the field and Hero, and the one-line answer to 'how many decisions is this actually
        built from'. The strip hides on the other tabs, where none of it means anything.
        """
        panel = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(panel)
        row.setContentsMargins(8, 2, 8, 2)
        row.addWidget(QtWidgets.QLabel("Report"))
        self.pop_report = QtWidgets.QComboBox()
        self.pop_report.setToolTip("The built-in reports plus anything saved under %s"
                                   % Metagame.REPORT_DIR)
        self.pop_report.currentIndexChanged.connect(self._population_report_changed)
        row.addWidget(self.pop_report, 1)
        design = QtWidgets.QPushButton("Design report...")
        design.clicked.connect(self.design_population_report)
        design.setToolTip("Choose the slices (spot, street, bet size, flop/turn/river texture) and "
                          "the measures, with a live preview. Nothing about a report is fixed.")
        row.addWidget(design)
        save = QtWidgets.QPushButton("Save")
        save.clicked.connect(self.save_population_report)
        save.setToolTip("Keep this report -- and the sample size and split currently set -- under "
                        "its name. The filter bar is not part of a saved report.")
        row.addWidget(save)
        delete = QtWidgets.QPushButton("Delete")
        delete.clicked.connect(self.delete_population_report)
        delete.setToolTip("Remove the saved file for this report. The built-in ones cannot be "
                          "deleted, they reset when this button is used on a saved copy.")
        row.addWidget(delete)
        self.pop_min_n = QtWidgets.QSpinBox()
        self.pop_min_n.setRange(1, 1000000)
        self.pop_min_n.setValue(15)
        self.pop_min_n.setSuffix(" min")
        self.pop_min_n.setToolTip("A row with fewer decisions than this shows 'not yet data' instead "
                                  "of a rate. Small samples are the whole danger of a metagame "
                                  "report.")
        self.pop_min_n.valueChanged.connect(self._population_tweaked)
        row.addWidget(self.pop_min_n)
        self.pop_whose = QtWidgets.QComboBox()
        for data, label in (("field", "the field"), ("hero", "Hero"), ("both", "Hero + field")):
            self.pop_whose.addItem(label, data)
        self.pop_whose.setToolTip("Population means everyone but you, which is the default here.")
        self.pop_whose.currentIndexChanged.connect(self._population_tweaked)
        row.addWidget(self.pop_whose)
        row.addStretch(1)
        self.pop_summary = QtWidgets.QLabel("no report run yet")
        self.pop_summary.setWordWrap(False)
        self.pop_summary.setToolTip("How many decisions the report was built from, and how many it "
                                    "left out because they never reached the street it groups by.")
        row.addWidget(self.pop_summary)
        panel.setVisible(False)
        self.pop_bar = panel
        return panel

    def _build_body(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.filter_panel)
        outer.addWidget(self._build_population_bar())

        self.split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        outer.addWidget(self.split)

        self.model = StatsModel()
        self.grid = QtWidgets.QTableView()
        # Named so the app stylesheet can give the grid its alternate rows and selection colour,
        # and named for the same reason the mode buttons are: an inline sheet would outrank a theme.
        self.grid.setObjectName("reportGrid")
        self.grid.setModel(self.model)
        self.grid.setAlternatingRowColors(True)
        self.grid.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.grid.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.grid.verticalHeader().setVisible(False)
        self.grid.selectionModel().selectionChanged.connect(self._row_selected)
        # Right-clicking a row is how a stake becomes a graph. The row already *is* the group, so
        # naming the slice you want needs no second control that can drift out of sync with the grid.
        self.grid.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_context)
        self.split.addWidget(self.grid)

        self.history = HistoryList(self.db_path)
        self.history.hand_activated.connect(self.open_replayer)
        self.split.addWidget(self.history)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([560, 260])
        self._init_population()

    # -- population reports ---------------------------------------------------------

    def _init_population(self):
        """Load the report list and put the last one used back on screen."""
        self._fill_report_chooser(self.settings.get("population_report"))
        self.pop_spec = dict(self.pop_specs[self.pop_report.currentIndex()])
        self._sync_population_controls()

    def _fill_report_chooser(self, select=None):
        """Rebuild the chooser from the presets plus the saved files, keeping `select` on screen.

        An applied-but-unsaved design is appended if no file carries its name, otherwise 'Apply'
        would show a report that then vanished from the very combo used to choose it.
        """
        specs = Metagame.load_specs()
        name = (self.pop_spec or {}).get("name")
        if name and not any(spec.get("name") == name for spec in specs):
            specs.append(dict((k, v) for k, v in self.pop_spec.items() if k != "where"))
        self.pop_specs = specs
        want = select if select is not None else name
        index = next((i for i, spec in enumerate(specs) if spec.get("name") == want), 0)
        self.pop_report.blockSignals(True)
        self.pop_report.clear()
        for i, spec in enumerate(specs):
            self.pop_report.addItem(str(spec.get("name", "report")), i)
        self.pop_report.setCurrentIndex(index)
        self.pop_report.blockSignals(False)

    def _sync_population_controls(self):
        """Put the strip's sample size and split onto the report now in force, without re-running it.

        Signals are blocked: setting a spin box is a *reflection* of the spec here, and letting it
        fire the changed-handler would apply the report twice on every chooser click.
        """
        spec = self.pop_spec or {}
        self.pop_min_n.blockSignals(True)
        self.pop_min_n.setValue(int(spec.get("min_n") or 1))
        self.pop_min_n.blockSignals(False)
        self.pop_whose.blockSignals(True)
        self.pop_whose.setCurrentIndex(
            max(0, self.pop_whose.findData(spec.get("split") or "field")))
        self.pop_whose.blockSignals(False)

    def _population_tab(self):
        return (self.mode == "cash"
                and REPORTS[self.tabs_bar.currentIndex()][1] == ("population",))

    def _update_population_bar(self):
        """Show the strip only on its own tab, and retire the hand-only control while it is there.

        The filter bar's min-hands box counts *hands*; a population row counts decisions and has its
        own minimum on the strip. Leaving both live and labelled alike is how a greyed-out box ends up
        being believed to be the thing that changed the numbers.
        """
        pop = self._population_tab()
        self.pop_bar.setVisible(pop)
        if self.mode == "tour":
            return
        self.min_hands.setEnabled(not pop)
        self.min_hands.setToolTip("" if not pop else
                                  "A population row counts decisions, not hands. Its minimum is the "
                                  "'min' box on the strip above the grid.")

    def _filters_dict(self):
        """The filter bar as the plain dict a spec carries in `where`.

        `Metagame.run` rebuilds a `Filters` from it, so the population grid answers its question over
        exactly the hands the other five tabs are counting -- one scope, six tabs, no second set of
        filters to keep in step.
        """
        filters = self.current_filters()
        return dict((key, getattr(filters, key)) for key in
                    ("date_from", "date_to", "site", "gametype_id", "limit_type", "category",
                     "player", "min_seats", "max_seats"))

    def filters_description(self):
        """One line naming what the filter bar currently restricts, for the designer's scope label."""
        bits = []
        if self.use_dates.isChecked():
            bits.append("%s to %s" % (self.date_from.date().toString("yyyy-MM-dd"),
                                      self.date_to.date().toString("yyyy-MM-dd")))
        if self.game_type.currentData():
            bits.append(self.game_type.currentText())
        if self.stakes.currentData():
            bits.append(self.stakes.currentText())
        if self.player_filter.text().strip():
            bits.append("player ~%s" % self.player_filter.text().strip())
        return ", ".join(bits) if bits else FALLBACK_SCOPE

    def current_population_spec(self):
        """The in-force report with the strip's controls and the filter bar merged into it.

        The strip wins over the stored spec: a sample size typed five seconds ago is the one the
        grid is showing, and it is what the designer has to open on.
        """
        spec = dict(self.pop_spec or {})
        spec["group_by"] = list(spec.get("group_by") or [])
        spec["measures"] = list(spec.get("measures") or [])
        spec["name"] = str(spec.get("name") or "Untitled report")
        spec["min_n"] = int(self.pop_min_n.value())
        spec["split"] = self.pop_whose.currentData()
        spec["where"] = self._filters_dict()
        return spec

    def _apply_population(self):
        """Run the report through Metagame and into the grid every other tab already uses.

        One grid for the whole window is the point: population rows get the theme, the column widths,
        the money colours, the num/den tooltips and the CSV export for free, and cannot drift into a
        second widget that renders the same number a different way.
        """
        spec = self.current_population_spec()
        con = self._connect()
        try:
            rows, measures, fields, meta = Metagame.grid_report(con, spec, self.hero)
        except sqlite3.Error as exc:
            self.current_group = ("population",)
            self.statusBar().showMessage("Population report failed: %s" % exc)
            return
        finally:
            con.close()
        columns = list(measures)
        if "all__n" not in measures:
            # Every rate on the row divides that row's decision count, so the count is never a column
            # he chose -- but a percentage whose denominator is off-screen is the number that gets
            # misread, so the tab puts it first whether or not the report asked for it.
            fields["n"] = ("Decisions", "int")
            columns.insert(0, "n")
        for row in rows:
            row["n"] = ("%d" % row["n"], row["n"], 0)
        self.current_group = ("population",)
        self.pop_columns, self.pop_fields = columns, fields
        self.last_rows = rows
        heading = " / ".join(Metagame.DIMENSION_LABEL[g] for g in spec["group_by"]) or "All decisions"
        self.model.configure(rows, columns, heading, fields)
        self.grid.resizeColumnsToContents()
        self.grid.resizeColumnToContents(0)
        left = "" if not meta["out_of_scope"] else (
            "  |  %d decisions left out: they never reached the street this report groups by"
            % meta["out_of_scope"])
        self.pop_summary.setText("%d rows over %d decisions (%s)%s" % (
            len(rows), meta["grouped"], POPULATION_SPLIT.get(spec["split"], spec["split"]),
            "" if not left else "  -- see the status bar"))
        self.statusBar().showMessage("Population: %s -- %d rows, %d decisions (%s)%s" % (
            spec["name"], len(rows), meta["grouped"],
            POPULATION_SPLIT.get(spec["split"], spec["split"]), left))
        self.history.show_for(self.current_filters(), self.hero, None,
                              "every filtered hand (a population row is not a hand filter)")

    def _select_population_report(self, index):
        self.pop_spec = dict(self.pop_specs[index])
        self._sync_population_controls()
        self._remember("population_report", self.pop_spec.get("name"))
        self.apply_filters()

    def _population_report_changed(self, index):
        if index >= 0:
            self._select_population_report(index)

    def _population_tweaked(self, *_args):
        """The strip's own controls write into the live spec, then redraw the grid.

        Only while the tab is showing: nudging a spin box should not re-query the five hand reports.
        """
        if self.pop_spec is None or not self.pop_bar.isVisible():
            return
        self.pop_spec["min_n"] = int(self.pop_min_n.value())
        self.pop_spec["split"] = self.pop_whose.currentData()
        self.apply_filters()

    def design_population_report(self):
        """Open the designer on the report on screen, and put whatever it returns onto the grid."""
        builder = ReportBuilder(self.current_population_spec(), self.db_path, self.hero, self)
        builder.set_scope(self.filters_description())
        if builder.exec() != QtWidgets.QDialog.Accepted:
            return
        self.pop_spec = builder.current_spec()
        self._sync_population_controls()
        self._fill_report_chooser(self.pop_spec["name"])
        self._remember("population_report", self.pop_spec["name"])
        self.apply_filters()
        self.statusBar().showMessage("Population: %s -- %s" % (
            self.pop_spec["name"],
            "saved to %s" % builder.saved_path if builder.saved_path else
            "not saved; press Save on the strip to keep it"))

    def save_population_report(self):
        spec = self.current_population_spec()
        spec.pop("where", None)
        if not spec["measures"]:
            QtWidgets.QMessageBox.warning(self, "Nothing to save",
                                          "This report has no measure columns.")
            return
        try:
            path = Metagame.save_spec(spec)
        except (IOError, OSError) as exc:
            QtWidgets.QMessageBox.critical(self, "Could not save", str(exc))
            return
        keep = spec["name"]
        self._fill_report_chooser(keep)
        self.statusBar().showMessage("Saved report '%s' to %s" % (keep, path))

    def delete_population_report(self):
        """Remove a saved report's file. Built-in reports are code, not files, so they stay."""
        name = str((self.pop_spec or {}).get("name") or "")
        safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|')
        path = os.path.join(Metagame.REPORT_DIR, "%s.report.json" % safe)
        if not os.path.isfile(path):
            QtWidgets.QMessageBox.information(
                self, "Nothing to delete",
                "'%s' is a built-in report: it is not a file, so there is nothing to delete. Save a "
                "design of your own and this button will remove that one." % name)
            return
        if QtWidgets.QMessageBox.question(
                self, "Delete saved report",
                "Delete the saved report file?\n%s\n\nThe design is gone from %s; the report on "
                "screen stays until you pick another." % (path, Metagame.REPORT_DIR)) \
                != QtWidgets.QMessageBox.Yes:
            return
        try:
            os.remove(path)
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, "Could not delete", str(exc))
            return
        self.pop_spec = None
        self._fill_report_chooser("")
        self._select_population_report(self.pop_report.currentIndex())
        self.statusBar().showMessage("Deleted %s" % path)

    # -- data -----------------------------------------------------------------------

    def graph_view(self):
        """The GraphView inside the graph window, building the window the first time it is needed.

        Everything that has to reach the chart -- theme, options, a new database path -- goes through
        here or checks `self.graph_window` first, because the window may not exist yet, and a plot
        page that was quietly constructed at startup is also a plot page that never got the settings
        the dialog had already saved.
        """
        if self.graph_window is None:
            self.graph_window = GraphWindow(self.db_path, self.hero, self.settings, self)
            self.graph_window.graph.recompute_ev.connect(self.recompute_ev)
            self.graph_window.graph.options_changed.connect(self._remember_graph_options)
            self.graph_window.graph.apply_theme()
        return self.graph_window.graph

    def show_graph(self, filters=None, scope=None, player=None):
        """Open (or re-focus) the graph window and plot `filters`; the grid never leaves the screen."""
        graph = self.graph_view()
        if player:
            graph.player.setText(player)
        graph.reload(filters, scope=scope)
        window = self.graph_window
        window.show()
        window.raise_()
        window.activateWindow()
        return graph

    def open_graph(self):
        """The Graph button: plot the hands the grid is currently counting, in the graph window."""
        title = REPORTS[self.tabs_bar.currentIndex()][0]
        self.show_graph(self.current_filters(), scope="%s, current filters" % title)

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
        title, group = REPORTS[self.tabs_bar.currentIndex()]
        self._update_population_bar()
        if group == ("population",):
            self._refresh_stakes_combo()
            self._apply_population()
            return
        self._refresh_stakes_combo()
        filters = self.current_filters()
        self.current_group = group
        con = self._connect()
        # Every cash grouping but By Players is his own report: scoped to the hero's seats, so the
        # Hands column counts hands (not seats) and a right-clicked row's plot is the same set.
        hero = None if group == ("player",) else self.hero
        try:
            rows = report(con, filters, group_by=group, columns=self.current_columns,
                          min_hands=self.min_hands.value(), hero=hero)
        finally:
            con.close()
        for row in rows:
            row["_label"] = self._label_for(group, row["group"])
        self.last_rows = rows
        self.model.configure(rows, self.current_columns, title.replace("By ", ""))
        self.grid.resizeColumnsToContents()
        self.grid.resizeColumnToContents(0)
        # Says whose hands these are: the same "1212 hands" means Hero's 1212 seats here and the whole
        # field's on the Players tab, and a number whose owner is unstated is not checkable.
        total = sum(r["_counters"].get("hands", 0) for r in rows)
        self.statusBar().showMessage("%s -- %d rows, %s hands" % (
            title, len(rows),
            "%s played %d" % (hero, total) if hero else "%d dealt" % total))
        self.history.show_for(filters, self._focus_player(rows, group), None, title)
        if self.graph_window is not None and self.graph_window.isVisible():
            self.graph_window.graph.reload(filters, self._focus_player(rows, group))

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
            self.history.show_for(filters, self.hero, None, "no finished events", chips=True)

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
        return self.hero

    def _tab_changed(self, _index):
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
        if self.current_group == ("population",):
            # A population row is a slice of *decisions*, and `Filters` cannot express a spot, a board
            # texture or a bet size, so the list below stays on the whole filtered corpus and says so.
            # Quietly listing every hand instead would make the pane look like it agreed with the row.
            self.history.show_for(filters, self.hero, None,
                                  "%s: the list is every filtered hand, not this row's"
                                  % row["_label"])
            return
        player = row["group"][0] if self.current_group == ("player",) else self.hero
        label = row["_label"]
        self.history.show_for(filters, player, gametype_id, label)

    # -- right-clicking a grid row: plot it, or narrow everything to it -----------------

    GROUP_NOUN = {"gametype": "stake", "game": "game type", "day": "day", "player": "player"}

    def _grid_context(self, pos):
        """Offer the row under the cursor as a graph and as a filter.

        Both actions read the row's own group key, so the plot is computed from the same set of hands
        the row's numbers were -- there is no second control that can disagree with the grid.
        """
        index = self.grid.indexAt(pos)
        if not index.isValid() or index.row() >= len(self.model.rows):
            return
        row = self.model.rows[index.row()]
        self.grid.selectRow(index.row())
        menu = QtWidgets.QMenu(self)
        if self.current_group == ("population",):
            # The hand reports' two actions both need a set of hands, and a population row is a set of
            # decisions, so the menu says what it cannot do and offers the one thing it can.
            note = menu.addAction("A population row is a slice of decisions, not of hands: "
                                  "nothing to plot")
            note.setEnabled(False)
            action = menu.addAction("Design this report...")
            action.triggered.connect(lambda _c=False: self.design_population_report())
        elif self.mode == "tour":
            # A tournament's Gametypes row holds the blind level, so a bb graph of tour hands would
            # divide by a different number in every hand. The filter is what a tour row can do.
            day = str(row["date"][0])[:10]
            action = menu.addAction("Filter to this event's day: %s" % day)
            action.triggered.connect(lambda _c=False, d=day: self.filter_to_date(d))
        else:
            noun = self.GROUP_NOUN.get(self.current_group[0])
            if noun:
                action = menu.addAction("Plot this %s: %s" % (noun, row["_label"]))
                action.triggered.connect(lambda _c=False, r=row: self.plot_group(r))
                action = menu.addAction("Filter everything to this %s" % noun)
                action.triggered.connect(lambda _c=False, r=row: self.filter_group(r))
            else:
                # By Position groups seats, and `Filters` cannot express a seat, so the menu says so
                # rather than plotting something the row does not mean.
                note = menu.addAction("A position is a seat, not a hand filter: nothing to plot")
                note.setEnabled(False)
        menu.addSeparator()
        action = menu.addAction("Clear all filters")
        action.triggered.connect(self.clear_filters)
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    @staticmethod
    def _narrow(base, **overrides):
        """A Filters that keeps `base` and replaces the named keys.

        `Filters` builds its WHERE from its constructor arguments and is never mutated, so narrowing
        to a right-clicked row without losing the date range or player name he already typed means
        rebuilding it with the overrides spliced in.
        """
        args = dict((key, getattr(base, key)) for key in
                    ("date_from", "date_to", "site", "gametype_id", "limit_type", "category",
                     "player", "min_seats", "max_seats"))
        args.update(overrides)
        return Filters(**args)

    def _group_slice(self, row):
        """(Filters, scope label) for one cash grid row, or (None, None) if it is not filterable."""
        base = self.current_filters()
        token = self.current_group[0]
        key = row["group"]
        if token == "gametype":
            return self._narrow(base, gametype_id=key[0]), row["_label"]
        if token == "game":
            category, limit = (str(key[0]).split("-") + [""])[:2]
            return self._narrow(base, category=category, limit_type=limit), row["_label"]
        if token == "day":
            return self._narrow(base, date_from=key[0], date_to=key[0]), "%s only" % key[0]
        if token == "player":
            return self._narrow(base, player=key[0]), "player %s" % key[0]
        return None, None

    def plot_group(self, row):
        """Open the graph window on exactly this row's hands, with the row named in the title.

        The player box is set as well as the filters, because a stake row's numbers now count the
        hero's seats: plotting that row with a villain's name still in the box would draw a line that
        is not the row.
        """
        filters, scope = self._group_slice(row)
        if filters is None:
            return
        player = row["group"][0] if self.current_group[0] == "player" else self.hero
        graph = self.show_graph(filters, scope=scope, player=player)
        self.statusBar().showMessage("Graph: %s (%d hands plotted, %s)" % (
            scope, len(graph.series), player))

    def filter_group(self, row):
        """Put the row's group into the filter bar and re-run every report, so grid and list agree."""
        token = self.current_group[0]
        key = row["group"]
        if token == "gametype":
            index = self.stakes.findData(key[0])
            if index >= 0:
                self.stakes.setCurrentIndex(index)
        elif token == "game":
            index = self.game_type.findData(str(key[0]))
            if index >= 0:
                self.game_type.setCurrentIndex(index)
        elif token == "day":
            self.filter_to_date(key[0])
            return
        elif token == "player":
            self.player_filter.setText(key[0])
        self.apply_filters()
        self.statusBar().showMessage("Filtered to %s: %s" % (
            self.GROUP_NOUN.get(token, "that row"), row["_label"]))

    def filter_to_date(self, day):
        """One calendar day in the date-range filter. `Filters` adds the end-of-day seconds itself
        when the value is 10 characters, so the day is inclusive."""
        self.use_dates.setChecked(True)
        date = QtCore.QDate.fromString(day, "yyyy-MM-dd")
        self.date_from.setDate(date)
        self.date_to.setDate(date)
        self.apply_filters()

    # -- settings -----------------------------------------------------------------------

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            self.store_settings(dialog.chosen())
        else:
            # The dialog previews the theme as it is picked, so Cancel has to put the old one back.
            self.preview_theme(self.settings["theme"])

    def store_settings(self, settings):
        self.settings = dict(settings)
        self.hero = self.settings.get("hero") or "Hero"
        self.preview_theme(self.settings["theme"])
        if self.graph_window is not None:
            self.graph_window.graph.apply_options(self.settings)
        self.history.set_hands_count(self.settings["history_hands"])
        self.apply_filters()
        path = HudThemes.save_settings(self.settings)
        self.statusBar().showMessage("Settings saved to %s"
                                     % (path or "the fpdb config folder (not writable)"))

    def preview_theme(self, key):
        """Apply a theme to the whole application now, then repaint what a stylesheet cannot reach:
        the grid's money colours and banner come from model roles, and the chart keeps its own
        brushes, so both have to be told."""
        HudThemes.apply(QtWidgets.QApplication.instance(), key)
        for action_key, action in self.theme_actions:
            action.setChecked(action_key == key)
        self.model.retheme()
        self.history.retheme()
        if self.graph_window is not None:
            self.graph_window.graph.apply_theme()
            if self.graph_window.isVisible():
                self.graph_window.graph._draw_current()

    def set_theme(self, key):
        """The Settings > Theme menu entry: applies and remembers, in one click, no dialog."""
        self.settings["theme"] = key
        self.preview_theme(key)
        HudThemes.save_settings(self.settings)
        self.statusBar().showMessage("Theme: %s (saved)" % HudThemes.theme()["label"])

    def _remember_graph_options(self):
        self.settings.update(self.graph_window.graph.options())
        HudThemes.save_settings(self.settings)

    def _remember(self, key, value):
        self.settings[key] = value
        HudThemes.save_settings(self.settings)

    def choose_columns(self):
        if self._population_tab():
            # A population report's columns are chosen together with the slices that make the rows,
            # so the column picker would be half a designer pointing at a stat list it does not own.
            self.design_population_report()
            return
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
        if self.graph_window is not None:
            self.graph_window.graph.db_path = target
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
        # Show the result, not just the message: the EV line this button exists for is invisible while
        # the graph window is still closed, and a dialog box is not a redraw.
        self.apply_filters()
        self.open_graph()
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
            # The model's own heading, not a literal "Report": on a population grid that column holds
            # the slices the rows were cut by ("River card did"), and a CSV whose first column is
            # labelled `Report` says nothing about what the rows are.
            writer.writerow([self.model.heading] + [fields[c][0] for c in columns])
            for row in self.last_rows:
                writer.writerow([row["_label"]] + [row[c][0] for c in columns])
        QtWidgets.QMessageBox.information(self, "Export", "Wrote %s" % target)

    def open_replayer(self, hand_id, player):
        from HudReplayer import ReplayWindow
        win = ReplayWindow(self.db_path, hand_id, player)
        win.show()
        self._replay_windows = getattr(self, "_replay_windows", []) + [win]


class SettingsDialog(QtWidgets.QDialog):
    """Program settings: the look, which lines the graph plots by default, and the hand-list length.

    The theme preview is live, because a colour choice cannot be judged from its name -- pick
    HM2-like and the window becomes HM-like or the setting is worthless. That is why Cancel has to
    undo the preview, and why `Restore defaults` re-previews too. The other controls are applied
    when the dialog is accepted; they change what the window shows next, not what it shows now.
    """

    def __init__(self, settings, window, parent=None):
        QtWidgets.QDialog.__init__(self, parent if parent is not None else window)
        self.source = window
        self.original = dict(settings)
        self.setWindowTitle("Settings")
        self.resize(620, 430)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._appearance(settings), "Appearance")
        tabs.addTab(self._graph_tab(settings), "Graph & hero")
        tabs.addTab(self._list_tab(settings), "Hand list")
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        reset = QtWidgets.QPushButton("Restore defaults")
        reset.setToolTip("Put every setting back to the built-in default and preview it now.")
        reset.clicked.connect(self.restore_defaults)
        buttons.addButton(reset, QtWidgets.QDialogButtonBox.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box = QtWidgets.QVBoxLayout(self)
        box.addWidget(tabs)
        box.addWidget(buttons)

    def _appearance(self, settings):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        self.theme_box = QtWidgets.QComboBox()
        for key, label in HudThemes.names():
            self.theme_box.addItem(label, key)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(settings.get("theme"))))
        self.theme_box.currentIndexChanged.connect(self._preview_theme)
        self.theme_box.setToolTip("Applies as soon as you pick it. Cancel puts the old one back.")
        form.addRow("Theme", self.theme_box)
        hint = QtWidgets.QLabel(
            "The theme repaints the window, the grid, the hand list and the graph. Line colours "
            "stay the same in every theme, so a plot you recognised before a switch is the same "
            "plot after it.")
        hint.setWordWrap(True)
        form.addRow(hint)
        where = QtWidgets.QLabel("Saved in: %s" % HudThemes.SETTINGS_FILE)
        where.setWordWrap(True)
        form.addRow(where)
        return page

    def _graph_tab(self, settings):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        group = QtWidgets.QGroupBox("Lines the graph plots (the same tick boxes are on the graph)")
        grid = QtWidgets.QGridLayout(group)
        self.line_boxes = {}
        for i, (key, label, _dash) in enumerate(GraphView.LINES):
            box = QtWidgets.QCheckBox(label)
            box.setIcon(GraphView._swatch(HudThemes.SERIES_COLORS[key]))
            box.setToolTip(GraphView.LINE_TIPS[key])
            box.setChecked(key in (settings.get("graph_lines") or []))
            self.line_boxes[key] = box
            grid.addWidget(box, i // 2, i % 2)
        outer.addWidget(group)
        form = QtWidgets.QFormLayout()
        self.xaxis_box = QtWidgets.QComboBox()
        for text, data in (("hand number", "hands"), ("days elapsed", "days")):
            self.xaxis_box.addItem(text, data)
        self.xaxis_box.setCurrentIndex(max(0, self.xaxis_box.findData(settings.get("graph_x"))))
        self.hero = QtWidgets.QLineEdit(settings.get("hero") or "Hero")
        self.hero.setToolTip("Matched exactly, the way the importer writes his own seat name.")
        hero_hint = QtWidgets.QLabel(
            "The seat that is you in the histories. By-Stakes, By-Day, By-Game and By-Position rows "
            "and the graph all count this name's hands, so a row and the plot it opens describe the "
            "same set of hands; the Players tab always lists everyone.")
        hero_hint.setWordWrap(True)
        form.addRow("Hero name", self.hero)
        form.addRow(hero_hint)
        form.addRow("x axis", self.xaxis_box)
        outer.addLayout(form)
        outer.addStretch(1)
        return page

    def _list_tab(self, settings):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        self.hands_box = QtWidgets.QSpinBox()
        self.hands_box.setRange(1, 100000)
        self.hands_box.setValue(int(settings.get("history_hands") or 250))
        self.hands_box.setSuffix(" hands")
        form.addRow("Hands listed", self.hands_box)
        hint = QtWidgets.QLabel(
            "How many hands the list under the grid shows when a report opens. The box above that "
            "list changes it for the moment; right-clicking a grid row narrows it to that row.")
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _preview_theme(self, _index):
        self.source.preview_theme(self.theme_box.currentData())

    def restore_defaults(self):
        settings = dict(HudThemes.DEFAULTS)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(settings["theme"])))
        for key, box in self.line_boxes.items():
            box.setChecked(key in settings["graph_lines"])
        self.xaxis_box.setCurrentIndex(max(0, self.xaxis_box.findData(settings["graph_x"])))
        self.hero.setText(settings["hero"])
        self.hands_box.setValue(settings["history_hands"])
        self._preview_theme(0)

    def chosen(self):
        """The settings this dialog is asking for, read back off the controls."""
        return {"theme": self.theme_box.currentData(),
                "graph_lines": [key for key, box in self.line_boxes.items() if box.isChecked()],
                "graph_x": self.xaxis_box.currentData(),
                "hero": self.hero.text().strip() or "Hero",
                "history_hands": int(self.hands_box.value())}

    def reject(self):
        self.source.preview_theme(self.original["theme"])
        QtWidgets.QDialog.reject(self)


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
    # The theme goes on before the window exists: a widget polished under the old palette keeps its
    # colours, and the first thing he would see is the light theme flash before the black one.
    HudThemes.apply(app, HudThemes.load_settings()["theme"])
    window = MainWindow(db_path)
    window.show()
    window.apply_filters()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
