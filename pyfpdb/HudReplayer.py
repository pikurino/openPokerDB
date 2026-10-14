#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""OpenPokerDB: import GGPoker hand histories and replay them one action at a time on a drawn table.

Implements DESIGN.md section 7, screens 1 (Import) and 3 (Hand viewer), on the database the GGPoker
parser writes (section 3). Everything on screen comes from the database or from the history files it
was built from -- nothing is recomputed from a premise about poker. The table itself is drawn by
HudTable, which receives plain numbers.

    python HudReplayer.py [--db <sqlite file>] [--history <folder>] [--hands <N>]

Database: the config (`HUD_config.xml`, created from `HUD_config.xml.example` on first run) selects
sqlite `fpdb.db3`. Those names still say fpdb, the upstream project this fork revives, on purpose:
they are what existing databases and the regression harness point at. The `fpdb` database section 6's
harness recreates is deliberately *not* the one this window writes to, so importing here can never be
wiped by `TestHandsPlayers.py`.
"""
import os
import re
import sys
import glob
import sqlite3
import optparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6 import QtCore, QtGui, QtWidgets

import Configuration
import Database
import HudTable
import Importer
import SQL
import Card

# A hand's streets, as fpdb numbers them: -1 is the post round (blinds), 4 is a display-only
# showdown with no actions of its own.
STREET_TITLES = {-1: "Posts", 0: "Preflop", 1: "Flop", 2: "Turn", 3: "River", 4: "Showdown"}
BOARD_CARDS = {-1: 0, 0: 0, 1: 3, 2: 4, 3: 5, 4: 5}
# DerivedStats.setPositions stores 'S'/'B' for the blind seats and counts the others back from the
# button, so '0' is the button and '3' is UTG at a 6-max table.
POSITION_LABELS = {"S": "SB", "B": "BB", "0": "BTN", "1": "CO", "2": "HJ", "3": "UTG",
                   "4": "UTG+1", "5": "UTG+2", "6": "MP1", "7": "MP2"}
# Gametypes.limitType values actually present in the imported corpus: 'nl' and 'pl'.
LIMIT_LABELS = {"nl": "no-limit", "pl": "pot-limit"}
# Money that changes the wager total, by the action name in the Actions lookup table.
POST_ACTIONS = frozenset(["ante", "small blind", "big blind", "secondsb", "bringin", "completes"])
SUIT_GLYPHS = {"h": "\u2665", "d": "\u2666", "c": "\u2663", "s": "\u2660"}
RE_HAND_CHUNK = re.compile(r"^Poker Hand #RC(\d+):", re.MULTILINE)
# Frames advance every 700 ms at 1x; the combo scales that.
PLAY_INTERVALS = {0.5: 1400, 1: 700, 2: 350, 4: 175}


def money(cents):
    return ("-$%.2f" if cents < 0 else "$%.2f") % (abs(cents) / 100.0)


def bb_text(cents, bb_cents):
    """Money in big blinds, keeping only the decimals that say something.

    A 2-cent BB makes a $0.54 bet 27 BB and a $5.83 stack 291.5 BB; the trailing zeros of %.2f would
    turn those into '27.00 BB' and '291.50 BB', which is noise on a plate a few centimetres wide.
    """
    value = round(cents / float(bb_cents), 2)
    text = ("%.2f" % value).rstrip("0").rstrip(".")
    return "%s BB" % ("0" if text in ("", "-0") else text)


# The database records only the history file's basename (Importer.py:154), so the folder it came
# from is remembered here to keep the raw-history pane working across restarts.
HISTORY_FOLDER_FILE = os.path.join(Configuration.CONFIG_PATH, "HudReplayer.history_folder")


def remember_history_folder(folder):
    try:
        with open(HISTORY_FOLDER_FILE, "w", encoding="utf8") as handle:
            handle.write(folder)
    except OSError:
        pass


def recalled_history_folder():
    try:
        with open(HISTORY_FOLDER_FILE, encoding="utf8") as handle:
            folder = handle.read().strip()
    except OSError:
        return ""
    return folder if os.path.isdir(folder) else ""


def card_face(card_id):
    """Return (text, is_red) for a stored card id, or None when the card is unknown."""
    if not card_id:
        return None
    name = Card.valueSuitFromCard(card_id)
    if not name:
        return None
    return (name[:-1] + SUIT_GLYPHS.get(name[-1], name[-1]), name[-1] in ("h", "d"))


def cards_text(card_ids):
    return " ".join((card_face(c) or ("?",))[0] for c in card_ids if c)


def action_sentence(name, player, amount, raise_to):
    """How the history itself would say it, for the action list and the table callout."""
    if name in POST_ACTIONS:
        return "%s posts %s %s" % (player, name, money(amount))
    if name in ("folds", "checks"):
        return "%s %s" % (player, name)
    if name == "raises":
        return "%s raises %s to %s" % (player, money(amount), money(raise_to))
    if name in ("calls", "bets"):
        return "%s %s %s" % (player, name, money(amount))
    return ("%s %s %s" % (player, name, money(amount))) if amount else "%s %s" % (player, name)


class ImportThread(QtCore.QThread):
    """Run one folder through the real pipeline. Emits counts; never touches the GUI thread."""

    finished = QtCore.Signal(object)

    def __init__(self, folder, parent=None):
        QtCore.QThread.__init__(self, parent)
        self.folder = folder

    def run(self):
        try:
            config = Configuration.Config()
            settings = {}
            settings.update(config.get_db_parameters())
            settings.update(config.get_import_parameters())
            settings.update(config.get_default_paths())
            # Same bootstrap as TestHandsPlayers.py, with two differences that matter: no
            # recreate_tables() (this database holds his real hands) and no fake HandCache.
            # Importer builds its own Database connection, which creates the schema the first time
            # because fpdb's sqlite connect() treats a missing Settings table as an empty database.
            importer = Importer.Importer(False, settings, config, None)
            importer.setDropIndexes("don't drop")
            importer.setThreads(-1)
            importer.setCallHud(False)
            # Auto-detect the site: the parser is chosen by IdentifySite, exactly as the
            # bulk-import screen would (DESIGN.md section 2.2).
            importer.addBulkImportImportFileOrDir(self.folder)
            (stored, dups, partial, errors, seconds) = importer.runImport()
            counts = {"folder": self.folder, "files": len(glob.glob(
                os.path.join(self.folder, "**", "*.txt"), recursive=True)),
                "stored": stored, "dups": dups, "partial": partial, "errors": errors,
                "seconds": round(seconds, 1), "error": None}
        except Exception as exc:                                  # noqa: BLE001 - shown on screen
            counts = {"folder": self.folder, "error": "%s: %s" % (exc.__class__.__name__, exc)}
        self.finished.emit(counts)


class ReplayWidget(QtWidgets.QWidget):
    """The hand as a table: one frame per action, with the controls to walk them."""

    def __init__(self, db_path, history_dir, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.db_path = db_path
        self.hand = None
        self.frames = []
        self.index = 0
        self.run = 0
        self.in_bb = False
        self.raw_cache = {}
        self.folder_hint = ""
        self.history_files = []

        self.title = QtWidgets.QLabel("no hand selected")
        self.title.setStyleSheet("font:bold 15px 'Segoe UI'; color:#eee; background:#20262c;"
                                 " padding:8px 10px; border-radius:6px;")
        self.table = HudTable.PokerTable()

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.title)
        box.addWidget(self.table, 1)
        box.addLayout(self._build_controls())
        box.addWidget(self._build_slider())
        self.log = QtWidgets.QListWidget()
        # The table is the product; the log is a ruler to click on. Uncapped, this list takes half
        # the window and squeezes the ring until its own chips overlap the cards.
        self.log.setMinimumHeight(90)
        self.log.setMaximumHeight(210)
        self.log.setStyleSheet("QListWidget {background:#12181d; color:#dbe4ec; border:1px solid "
                               "#2b343d; font:11px 'Consolas';} "
                               "QListWidget::item:selected {background:#1d3a5c; color:#ffffff;}")
        self.log.itemClicked.connect(self._row_clicked)
        box.addWidget(self.log, 0)
        self.raw_btn = QtWidgets.QPushButton("Show raw history")
        self.raw_btn.clicked.connect(self.toggle_raw)
        box.addWidget(self.raw_btn)
        self.raw = QtWidgets.QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setMinimumHeight(140)
        self.raw.setStyleSheet("background:#12181d; color:#c8d4de; font:11px 'Consolas';")
        self.raw.hide()
        box.addWidget(self.raw)
        self.set_history_folder(history_dir)

    def _build_controls(self):
        row = QtWidgets.QHBoxLayout()
        self.first_btn = QtWidgets.QPushButton("|<")
        self.prev_btn = QtWidgets.QPushButton("<")
        self.play_btn = QtWidgets.QPushButton("Play")
        self.next_btn = QtWidgets.QPushButton(">")
        self.last_btn = QtWidgets.QPushButton(">|")
        for button, tip, slot in (
                (self.first_btn, "Back to the start of the hand (Home)", lambda: self.seek(0)),
                (self.prev_btn, "One action back (Left arrow)", lambda: self.step(-1)),
                (self.play_btn, "Replay the rest of the hand automatically (Space)",
                 self.toggle_play),
                (self.next_btn, "One action forward (Right arrow)", lambda: self.step(1)),
                (self.last_btn, "Straight to the showdown (End)",
                 lambda: self.seek(len(self.frames) - 1))):
            button.setToolTip(tip)
            button.setFixedWidth(46 if button is not self.play_btn else 74)
            button.clicked.connect(slot)
            row.addWidget(button)
        self.counter = QtWidgets.QLabel("0 / 0")
        self.counter.setStyleSheet("color:#9fb3c8; font:11px 'Segoe UI';")
        row.addWidget(self.counter)
        self.speed = QtWidgets.QComboBox()
        for factor in sorted(PLAY_INTERVALS):
            self.speed.addItem("%.1fx" % factor, factor)
        self.speed.setCurrentIndex(1)
        self.speed.setToolTip("How fast the actions step during playback")
        self.speed.currentIndexChanged.connect(self._speed_changed)
        row.addWidget(self.speed)
        row.addSpacing(18)
        self.bb_btn = QtWidgets.QPushButton("Show BB")
        self.bb_btn.setCheckable(True)
        self.bb_btn.setStyleSheet("QPushButton:checked {background:#2d6cdf; color:white;}")
        self.bb_btn.setToolTip("Draw stacks, bets, wins and the pot in big blinds of this hand's "
                               "posted big blind instead of dollars.")
        self.bb_btn.toggled.connect(self._bb_toggled)
        row.addWidget(self.bb_btn)
        row.addSpacing(18)
        self.street_btns = {}
        for street in sorted(STREET_TITLES):
            button = QtWidgets.QPushButton(STREET_TITLES[street])
            button.setCheckable(True)
            button.setToolTip("Jump to the first action on the %s street (Up/Down arrows walk "
                              "streets)" % STREET_TITLES[street].lower())
            button.clicked.connect(lambda _checked, s=street: self.jump_street(s))
            button.hide()
            self.street_btns[street] = button
            row.addWidget(button)
        row.addStretch(1)
        self.run_btns = []
        for index in (0, 1):
            button = QtWidgets.QPushButton("run %d" % (index + 1))
            button.setCheckable(True)
            button.setStyleSheet("QPushButton:checked {background:#2d6cdf; color:white;}")
            button.clicked.connect(lambda _checked, i=index: self.set_run(i))
            button.hide()
            self.run_btns.append(button)
            row.addWidget(button)
        self.play_timer = QtCore.QTimer(self)
        self.play_timer.timeout.connect(self._tick)
        return row

    def _build_slider(self):
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.slider.setToolTip("Drag to any action in the hand")
        self.slider.valueChanged.connect(self._slider_moved)
        return self.slider

    def toggle_raw(self):
        # isHidden() is the flag this toggle owns; isVisible() would also be False for a window the
        # user has minimised.
        show = self.raw.isHidden()
        self.raw.setVisible(show)
        self.raw_btn.setText("Hide raw history" if show else "Show raw history")
        if show and self.hand and not self.raw.toPlainText():
            self.raw.setPlainText(self.load_raw(self.hand))

    def load_raw(self, hand):
        """The hand's own text, read out of the file it was imported from."""
        path = None
        stem = hand["file_stem"]
        if stem:
            path = next((c for c in self.history_files if
                         os.path.splitext(os.path.basename(c))[0] == stem), None)
        if path is None:
            return ("History file %s.txt was not found under %s, so the raw text cannot be shown. "
                    "Pick the folder again with 'Import hand histories...'"
                    % (stem or "(unknown)", self.folder_hint or "(no folder chosen yet)"))
        if path not in self.raw_cache:
            text = open(path, encoding="utf8", errors="replace").read().replace("\r\n", "\n")
            marks = list(RE_HAND_CHUNK.finditer(text))
            self.raw_cache[path] = dict(
                (int(m.group(1)), text[m.end():(marks[i + 1].start() if i + 1 < len(marks)
                                                   else len(text))].strip())
                for i, m in enumerate(marks))
        return self.raw_cache[path].get(int(hand["siteHandNo"]),
                                        "(hand not found in %s)" % path)

    def set_history_folder(self, folder):
        """Where to look for raw text: the folder the histories were imported from."""
        self.folder_hint = folder if (folder and os.path.isdir(folder)) else ""
        self.history_files = sorted(glob.glob(os.path.join(self.folder_hint, "**", "*.txt"),
                                              recursive=True)) if self.folder_hint else []
        self.raw_cache.clear()
        self.raw_btn.setToolTip("Read from the .txt files under %s"
                                % (self.folder_hint or "no folder chosen yet"))

    def set_hand(self, hand_id):
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute(
                "select h.id, h.siteHandNo, h.tableName, h.startTime, h.seats, h.runItTwice, "
                "h.boardcard1, h.boardcard2, h.boardcard3, h.boardcard4, h.boardcard5, h.comment, "
                "g.category, g.limitType, g.smallBlind, g.bigBlind, "
                "h.street1Pot, h.street2Pot, h.street3Pot, f.file "
                "from Hands h left join Gametypes g on g.id = h.gametypeId "
                "left join Files f on f.id = h.fileId where h.id = ?",
                (hand_id,)).fetchone()
            players = con.execute(
                "select p.name, hp.position, hp.seatNo, hp.startCash, hp.winnings, hp.totalProfit, "
                "hp.showed, hp.card1, hp.card2, hp.card3, hp.card4 "
                "from HandsPlayers hp join Players p on p.id = hp.playerId where hp.handId = ? "
                "order by hp.seatNo", (hand_id,)).fetchall()
            actions = con.execute(
                "select ha.street, ha.actionNo, a.name, p.name, ha.amount, ha.raiseTo, "
                "ha.amountCalled, ha.allIn from HandsActions ha join Players p on p.id = "
                "ha.playerId join Actions a on a.id = ha.actionId where ha.handId = ? "
                "order by ha.actionNo", (hand_id,)).fetchall()
            boards = con.execute(
                "select boardId, boardcard1, boardcard2, boardcard3, boardcard4, boardcard5 "
                "from Boards where handId = ? order by boardId", (hand_id,)).fetchall()
        finally:
            con.close()
        if row is None:
            self.hand = None
            self.frames = []
            self.title.setText("hand %s is not in %s" % (hand_id, os.path.basename(self.db_path)))
            self.table.set_state(None)
            return

        hand = {"id": row[0], "siteHandNo": row[1], "tableName": row[2], "startTime": row[3],
                "seats": row[4], "runItTwice": row[5], "board": list(row[6:11]), "comment": row[11],
                "category": row[12], "limitType": row[13], "sb": row[14], "bb": row[15],
                "file_stem": row[19],
                # Hand.py:1068 names these columns 'pot size at flop / turn / river', so street 1
                # is street1Pot. street4Pot and showdownPot are hardcoded 0 for hold'em there.
                "streetPots": {1: row[16], 2: row[17], 3: row[18], 4: row[18]}}
        hand["players"] = [{"name": p[0], "position": p[1], "seatNo": p[2], "startCash": p[3],
                            "winnings": p[4], "profit": p[5], "showed": p[6], "all_in_on": None,
                            "hole_cards": 4 if hand["category"] == "omahahi" else 2,
                            "cards": [c for c in p[7:11] if c]} for p in players]
        hand["actions"] = [{"street": a[0], "actionNo": a[1], "name": a[2], "player": a[3],
                            "amount": a[4], "raiseTo": a[5], "called": a[6], "allIn": a[7]}
                           for a in actions]
        # Only run-it-twice hands get Boards rows (DerivedStats.py:177-189 writes one per run);
        # every other hand's board lives on the Hands row itself.
        hand["boards"] = [list(b[1:6]) for b in boards] or [list(row[6:11])]

        # Money this action puts into the pot, per fpdb's own semantics (Hand.py: addRaiseBy stores
        # (raise-by, raise-to, amount-called) and the pot grows by the called part plus the raise).
        committed = dict((p["name"], {}) for p in hand["players"])
        for act in hand["actions"]:
            name = act["name"]
            if name in POST_ACTIONS or name in ("calls", "bets"):
                added = act["amount"]
            elif name == "raises":
                added = act["amount"] + act["called"]
            else:
                added = 0
            act["added"] = added
            committed[act["player"]][act["street"]] = committed[act["player"]].get(
                act["street"], 0) + added
        # Money an opponent put in but got straight back because nobody called it. The database
        # gives it exactly: a player's net loss is what stayed in the pot, so
        # at-risk = winnings - totalProfit, and anything wagered above that was returned.
        # Measured over all 345 imported hands, at-risk never exceeds what was wagered.
        hand["returned"] = dict(
            (p["name"], max(0, sum(committed[p["name"]].values())
                            - (p["winnings"] - p["profit"]))) for p in hand["players"])
        self.hand = hand
        self.run = 0
        self.raw.clear()
        if not self.raw.isHidden():
            self.raw.setPlainText(self.load_raw(hand))
        self.rebuild_frames()
        self.seek(0)

    def rebuild_frames(self):
        """Turn the hand's rows into one frame per thing that happens, in order."""
        hand = self.hand
        board = hand["boards"][self.run]
        names = [p["name"] for p in hand["players"]]
        wagered = dict((n, 0) for n in names)
        street_bet = dict((n, 0) for n in names)
        returned, folded, all_in, winners = {}, [], [], {}
        revealed = ["Hero"] if "Hero" in names else []
        state = {"board_n": 0}
        frames = []

        def push(kind, street, text, bubble=None):
            frames.append({"kind": kind, "street": street, "text": text, "bubble": bubble,
                           "board_n": state["board_n"], "wagered": dict(wagered),
                           "street_bet": dict(street_bet), "returned": dict(returned),
                           "folded": list(folded), "all_in": list(all_in),
                           "revealed": list(revealed), "winners": dict(winners)})

        games = {"holdem": "Hold'em", "omahahi": "Omaha Hi"}
        push("start", -1, "Hand starts: %s %s, %d seats, blinds %s/%s" % (
            games.get(hand["category"], hand["category"]),
            LIMIT_LABELS.get(hand["limitType"], hand["limitType"]), hand["seats"],
            money(hand["sb"]), money(hand["bb"])))
        dealt, last = 0, None
        for act in hand["actions"]:
            street = act["street"]
            if last is not None and street > 0 and street != last:
                # A new betting round: the chips in front of everyone now belong to the pot.
                street_bet.update((n, 0) for n in names)
            if street > dealt and street in (1, 2, 3):
                dealt = street
                state["board_n"] = BOARD_CARDS[street]
                push("deal", street, "%s: %s" % (STREET_TITLES[street],
                                                 cards_text(board[:state["board_n"]])))
            name = act["player"]
            wagered[name] += act["added"]
            street_bet[name] += act["added"]
            if act["name"] == "folds" and name not in folded:
                folded.append(name)
            if act["allIn"] and name not in all_in:
                all_in.append(name)
            text = action_sentence(act["name"], name, act["amount"], act["raiseTo"])
            if act["allIn"]:
                text += "  (all-in)"
            last = street
            push("action", street, text, (name, text))
        # An all-in runout deals the rest of the board with no actions to walk through.
        final = len([c for c in board if c])
        for street in (1, 2, 3):
            if dealt < street and final >= BOARD_CARDS[street]:
                dealt = street
                state["board_n"] = BOARD_CARDS[street]
                street_bet.update((n, 0) for n in names)
                push("deal", street, "%s: %s  (no action, all-in)" % (
                    STREET_TITLES[street], cards_text(board[:state["board_n"]])))
        for p in hand["players"]:
            back = hand["returned"][p["name"]]
            if back:
                returned[p["name"]] = back
                push("return", max(dealt, 0), "%s gets back %s (uncalled)" % (
                    p["name"], money(back)), (p["name"], "gets back %s" % money(back)))
        for p in hand["players"]:
            if p["name"] in revealed or not p["cards"]:
                continue
            revealed.append(p["name"])
            push("reveal", 4, "%s shows %s" % (p["name"], cards_text(p["cards"])),
                 (p["name"], "shows %s" % cards_text(p["cards"])))
        for p in sorted(hand["players"], key=lambda q: -q["winnings"]):
            if not p["winnings"]:
                continue
            winners[p["name"]] = p["winnings"]
            push("payout", 4, "%s wins %s" % (p["name"], money(p["winnings"])),
                 (p["name"], "wins %s" % money(p["winnings"])))
        self.frames = frames
        self.index = 0

        self.log.blockSignals(True)
        self.log.clear()
        for frame in frames:
            self.log.addItem(frame["text"])
        self.log.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, len(frames) - 1))
        self.slider.setPageStep(max(1, len(frames) // 20))
        self.slider.blockSignals(False)
        streets = set(f["street"] for f in frames)
        for street, button in self.street_btns.items():
            button.setVisible(street in streets)
            button.setChecked(street == frames[0]["street"])
        multi = len(hand["boards"]) > 1
        for index, button in enumerate(self.run_btns):
            button.setVisible(multi and index < len(hand["boards"]))
            button.setChecked(index == self.run)

    def seek(self, index, keep_playing=False):
        """Show one frame. Anything the user does to move stops playback; the timer does not."""
        if not self.frames:
            return
        if not keep_playing:
            self.stop_play()
        self.index = max(0, min(len(self.frames) - 1, index))
        self._render()

    def step(self, delta):
        self.seek(self.index + delta)

    def jump_street(self, street):
        for index, frame in enumerate(self.frames):
            if frame["street"] == street:
                self.seek(index)
                return

    def step_street(self, delta):
        order = sorted(set(f["street"] for f in self.frames))
        position = order.index(self.frames[self.index]["street"])
        if 0 <= position + delta < len(order):
            self.jump_street(order[position + delta])

    def set_run(self, index):
        if index == self.run or index >= len(self.hand["boards"]):
            return
        # The point of a second run is to compare the same street, so stay on it rather than
        # bouncing to the start of the hand.
        street = self.frames[self.index]["street"] if self.frames else None
        self.run = index
        self.rebuild_frames()
        if street is not None:
            self.jump_street(street)

    def toggle_play(self):
        if self.play_timer.isActive():
            self.stop_play()
        elif self.frames:
            if self.index >= len(self.frames) - 1:
                # Rewind visibly: pressing Play at the showdown should show the start of the hand
                # at once, not the payout until the first tick lands.
                self.seek(0, keep_playing=True)
            self.play_timer.start(PLAY_INTERVALS[self.speed.currentData()])
            self.play_btn.setText("Pause")

    def stop_play(self):
        self.play_timer.stop()
        self.play_btn.setText("Play")

    def _tick(self):
        if self.index >= len(self.frames) - 1:
            self.stop_play()
            return
        self.play_timer.setInterval(PLAY_INTERVALS[self.speed.currentData()])
        self.seek(self.index + 1, keep_playing=True)
        if self.index >= len(self.frames) - 1:
            self.stop_play()

    def _slider_moved(self, value):
        if value != self.index:
            self.seek(value)

    def _row_clicked(self, item):
        self.seek(self.log.row(item))

    def _speed_changed(self):
        if self.play_timer.isActive():
            self.play_timer.setInterval(PLAY_INTERVALS[self.speed.currentData()])

    def _bb_toggled(self, checked):
        self.in_bb = checked
        self._render()

    def fmt(self, cents):
        """A money figure for the table: big blinds when 'Show BB' is on, dollars when it is not.

        The divisor is the hand's own big blind from Gametypes (integer cents, 2 for the imported
        $0.01/$0.02 corpus), so what reads as 100 BB is 100 BB at any stakes. Hands without a known
        blind fall back to dollars rather than dividing by zero. The action log and the title stay in
        dollars: they quote the hand's own text, where the site wrote dollars.
        """
        bb = (self.hand or {}).get("bb") or 0
        return bb_text(cents, bb) if self.in_bb and bb else money(cents)

    def _seat_rows(self, frame):
        hand = self.hand
        order = sorted(hand["players"], key=lambda p: p["seatNo"])
        anchor = next((i for i, p in enumerate(order) if p["name"] == "Hero"), None)
        if anchor is None:
            anchor = next((i for i, p in enumerate(order) if str(p["position"]) == "0"), 0)
        # Measured on the corpus: GG numbers seats BTN, SB, BB, UTG, HJ, CO -- against the way
        # action passes. The table is drawn so action runs from the hero's right, over the top and
        # down to his left, so the ring is built by walking the seat numbers *down* from the hero:
        # hero on the button puts CO at his right and SB at his left, as at a live table.
        order = [order[anchor]] + list(reversed(order[anchor + 1:] + order[:anchor]))
        actor = frame["bubble"][0] if frame["bubble"] else None
        rows = []
        for p in order:
            name = p["name"]
            won = frame["winners"].get(name, 0)
            known = bool(p["cards"]) and (name == "Hero" or name in frame["revealed"])
            folded = name in frame["folded"]
            # A folded opponent is out of the hand, and a row of card backs on their plate reads as
            # a live player; the hero's own cards stay visible after a fold, as they do in PT4.
            cards = ((p["cards"] if known else [0] * p["hole_cards"])
                     if not folded or name == "Hero" else [])
            rows.append({
                "name": "HERO" if name == "Hero" else name,
                "hero": name == "Hero",
                "position": POSITION_LABELS.get(str(p["position"]), ""),
                "stack_text": self.fmt(p["startCash"] - frame["wagered"][name]
                                       + frame["returned"].get(name, 0) + won),
                "cards": cards,
                "face_up": known,
                "folded": folded,
                "dealer": str(p["position"]) == "0",
                "bet_text": (self.fmt(frame["street_bet"][name])
                             if frame["street_bet"][name] else ""),
                "tag": "ALL-IN" if name in frame["all_in"] else "",
                "winner_text": ("WINS %s" % self.fmt(won)) if won else "",
                "acting": name == actor})
        return rows

    def _render(self):
        if not self.hand or not self.frames:
            return
        frame = self.frames[self.index]
        hand = self.hand
        board = [c for c in hand["boards"][self.run][:frame["board_n"]] if c]
        pot = sum(frame["wagered"].values()) - sum(frame["returned"].values())
        stored = 0
        for street in (3, 2, 1):
            if street <= frame["street"] and hand["streetPots"].get(street):
                stored = hand["streetPots"][street]
                break
        self.table.set_state({"seats": self._seat_rows(frame), "board": board,
                              "pot_text": "Pot %s" % self.fmt(pot)})
        self.table.setToolTip(
            "Pot is the money the players have put in and not had back, summed from HandsActions.\n"
            "Hands.street%dPot for this street says %s: that column also counts GGPoker's Cash "
            "Drop money and any uncalled bet the bettor gets back (DESIGN.md 3.3, 3.6)."
            % ({-1: 1, 0: 1, 1: 1, 2: 2, 3: 3, 4: 3}[frame["street"]], money(stored or pot)))
        for street, button in self.street_btns.items():
            button.setChecked(street == frame["street"])
        runs = len(hand["boards"])
        self.counter.setText("%d / %d%s" % (self.index + 1, len(self.frames),
                                            "   board run %d of %d" % (self.run + 1, runs)
                                            if runs > 1 else ""))
        self.slider.blockSignals(True)
        self.slider.setValue(self.index)
        self.slider.blockSignals(False)
        self.log.blockSignals(True)
        self.log.setCurrentRow(self.index)
        self.log.scrollToItem(self.log.item(self.index))
        self.log.blockSignals(False)
        games = {"holdem": "Hold'em", "omahahi": "Omaha Hi"}
        self.title.setText("%s  %s  %s %s  %s/%s   |   %s" % (
            "#RC%s" % hand["siteHandNo"], hand["startTime"],
            games.get(hand["category"], hand["category"]),
            LIMIT_LABELS.get(hand["limitType"], hand["limitType"]),
            money(hand["sb"]), money(hand["bb"]), frame["text"]))


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, db_path, history_dir, hand_limit):
        QtWidgets.QMainWindow.__init__(self)
        self.db_path = db_path
        self.history_dir = history_dir
        self.hand_limit = hand_limit
        self.thread = None
        self.last_report = "database %s" % os.path.basename(db_path)
        self.setWindowTitle("OpenPokerDB - GG import and replay")
        self.resize(1500, 900)

        bar = QtWidgets.QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.import_btn = QtWidgets.QPushButton("Import hand histories...")
        self.import_btn.setToolTip("Pick the folder GG writes its histories to; every .txt under "
                                   "it is imported into %s" % db_path)
        self.import_btn.clicked.connect(self.choose_folder)
        bar.addWidget(self.import_btn)
        self.status = QtWidgets.QLabel("")
        bar.addWidget(self.status)
        self.set_status("")

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.setCentralWidget(split)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 0, 6)
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter: type to narrow on time, game, stakes, table")
        self.filter_edit.textChanged.connect(self.load_hands)
        left_layout.addWidget(self.filter_edit)
        self.hands = QtWidgets.QTableWidget()
        self.hands.setColumnCount(6)
        self.hands.setHorizontalHeaderLabels(["Started", "Game", "Stakes", "Seats", "Hero $",
                                              "Hand"])
        self.hands.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.hands.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.hands.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.hands.horizontalHeader().setStretchLastSection(True)
        self.hands.verticalHeader().setVisible(False)
        self.hands.itemSelectionChanged.connect(self.selected_hand)
        left_layout.addWidget(self.hands)
        split.addWidget(left)

        self.replay = ReplayWidget(db_path, history_dir)
        split.addWidget(self.replay)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([430, 1050])

        # Handled as key events rather than QShortcuts: a shortcut would also fire while he is
        # typing in the filter box or moving through the hand list with the arrows.
        if not os.path.exists(db_path):
            self.last_report = "no database yet - click Import hand histories..."
        self.load_hands()

    #: widgets that own the arrow keys and space themselves
    FOCUS_OWNS_KEYS = (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit,
                       QtWidgets.QAbstractItemView, QtWidgets.QComboBox,
                       QtWidgets.QAbstractButton)

    def keyPressEvent(self, event):
        if not isinstance(QtWidgets.QApplication.focusWidget(), self.FOCUS_OWNS_KEYS):
            # Down leads the eye the same way the street buttons and the action log do: later
            # streets are further down.
            actions = {QtCore.Qt.Key_Left: lambda: self.replay.step(-1),
                       QtCore.Qt.Key_Right: lambda: self.replay.step(1),
                       QtCore.Qt.Key_Up: lambda: self.replay.step_street(-1),
                       QtCore.Qt.Key_Down: lambda: self.replay.step_street(1),
                       QtCore.Qt.Key_Home: lambda: self.replay.seek(0),
                       QtCore.Qt.Key_End: lambda: self.replay.seek(len(self.replay.frames) - 1),
                       QtCore.Qt.Key_Space: self.replay.toggle_play}
            action = actions.get(event.key())
            if action is not None:
                action()
                event.accept()
                return
        QtWidgets.QMainWindow.keyPressEvent(self, event)

    def choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Import hand histories",
                                                            self.history_dir
                                                            if os.path.isdir(self.history_dir)
                                                            else os.path.expanduser("~"))
        if not folder:
            return
        self.set_status("importing %s ..." % folder)
        self.import_btn.setEnabled(False)
        self.thread = ImportThread(folder)
        self.thread.finished.connect(self.import_done)
        self.thread.start()

    def import_done(self, counts):
        self.import_btn.setEnabled(True)
        if counts.get("error"):
            self.set_status("IMPORT FAILED -- %s" % counts["error"])
            QtWidgets.QMessageBox.critical(self, "Import failed", counts["error"])
            return
        self.replay.set_history_folder(counts["folder"])
        remember_history_folder(counts["folder"])
        # Errors are shown on screen, never only logged (DESIGN.md section 7.1).
        self.last_report = "%s | %d stored, %d duplicate, %d partial, %d errors, %.1fs" % (
            os.path.basename(counts["folder"]), counts["stored"], counts["dups"],
            counts["partial"], counts["errors"], counts["seconds"])
        self.load_hands()
        if counts["errors"]:
            QtWidgets.QMessageBox.warning(
                self, "Hands failed to import",
                "%d hands raised parse errors and are not in the database.\nLog: %s"
                % (counts["errors"], os.path.join(Configuration.CONFIG_PATH, "log",
                                                  "fpdb-log.txt")))

    def load_hands(self):
        needle = self.filter_edit.text().strip().lower() if self.filter_edit else ""
        con = sqlite3.connect(self.db_path)
        try:
            if not con.execute("select count(*) from sqlite_master where type='table' and "
                               "name='Hands'").fetchone()[0]:
                self.hands.setRowCount(0)
                return
            rows = con.execute(
                "select h.id, h.startTime, g.category, g.limitType, g.smallBlind, g.bigBlind, "
                "h.seats, h.siteHandNo, "
                "(select coalesce(sum(hp.totalProfit), 0) from HandsPlayers hp "
                " join Players p on p.id = hp.playerId "
                " where hp.handId = h.id and p.name = 'Hero') as hero "
                "from Hands h left join Gametypes g on g.id = h.gametypeId "
                "order by h.startTime desc, h.id desc limit ?", (self.hand_limit,)).fetchall()
        except sqlite3.Error as exc:
            self.set_status("cannot read %s -- %s" % (self.db_path, exc))
            return
        finally:
            con.close()
        games = {"holdem": "Hold'em", "omahahi": "Omaha Hi"}
        self.hands.setRowCount(0)
        for row in rows:
            cells = [row[1], games.get(row[2], row[2] or ""),
                     "%s/%s" % (money(row[4]), money(row[5])), str(row[6]),
                     money(row[8]), "#RC%s" % row[7]]
            if needle and needle not in " ".join(cells).lower():
                continue
            index = self.hands.rowCount()
            self.hands.insertRow(index)
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if column == 4:
                    item.setForeground(QtGui.QBrush(QtGui.QColor(
                        "#c0392b" if text.startswith("-$") else "#2e8b57")))
                item.setData(QtCore.Qt.UserRole, row[0])
                self.hands.setItem(index, column, item)
        self.hands.resizeColumnsToContents()
        self.set_status("%d hands listed" % self.hands.rowCount())
        if self.hands.rowCount():
            self.hands.selectRow(0)

    def set_status(self, text):
        self.status.setText("  %s  |  %s" % (self.last_report, text))

    def selected_hand(self):
        items = self.hands.selectedItems()
        if items:
            self.replay.set_hand(items[0].data(QtCore.Qt.UserRole))


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = optparse.OptionParser(usage="%prog [options]")
    parser.add_option("--db", default=None,
                      help="SQLite file to read (default: the one HUD_config.xml names)")
    parser.add_option("--history", default=None,
                      help="folder of .txt histories, for the raw-history pane (default: the "
                           "folder last imported, remembered in %s)" % HISTORY_FOLDER_FILE)
    parser.add_option("--hands", type="int", default=400, help="max hands to list")
    (options, _args) = parser.parse_args(argv)

    config = Configuration.Config()
    params = config.get_db_parameters()
    if params.get("db-backend") != Database.Database.SQLITE:
        print("This build reads SQLite only (DESIGN.md section 5.1); config says %r"
              % params.get("db-server"))
        return 1
    db_path = options.db or os.path.join(config.dir_database, params["db-databaseName"])
    history_dir = options.history or recalled_history_folder()

    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont("Segoe UI", 10))
    window = MainWindow(db_path, history_dir, options.hands)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
