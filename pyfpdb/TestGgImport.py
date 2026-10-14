#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""Verify a GGPoker import against the hand histories it came from.

Read-only: it queries the database named in the config and re-reads the corpus, then asserts the
identities that the row-comparison harness (TestHandsPlayers.py) cannot see -- the per-hand money
identity, the run-it-twice boards, and card visibility. Run TestHandsPlayers.py -s GGPoker first to
populate the test database from the fixtures.

    python TestGgImport.py [corpus_dir] [--config HUD_config.test.xml] [--verbose]
"""
import sys
import os
import re
import glob
import sqlite3
import optparse
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import Configuration
import Database
import Card

# The Windows console is cp1252 and hand-history text can break print(); keep every message here
# plain ASCII (DESIGN.md section 5.3 records the same failure inside logging).

RE_HAND = re.compile(r"^Poker Hand #RC(\d+):", re.MULTILINE)
RE_MONEY = re.compile(r"^Total pot \$([\d.]+) \| Rake \$([\d.]+) \| Jackpot \$([\d.]+)"
                      r" \| Bingo \$([\d.]+) \| Fortune \$([\d.]+) \| Tax \$([\d.]+)",
                      re.MULTILINE)
RE_DROP = re.compile(r"^Cash Drop to Pot : total \$([\d.]+)", re.MULTILINE)
# A player's cards are known if Hero (dealt in 'Dealt to Hero [...]', doc section 6) or if the hand reveals
# them: the body form is "<alias>: shows [...]", the summary form "Seat n: <alias> (button) showed".
RE_BODY_SHOWN = re.compile(r"^(\S+): (?:shows|showed) \[", re.MULTILINE)
RE_SUMMARY_SHOWN = re.compile(r"^Seat \d+: (\S+)(?: \([^)]*\))? showed \[", re.MULTILINE)
# Doc section 8.3: a run-it-twice hand labels its streets FIRST/SECOND and each line carries
# '[board so far] [new card]', so the deepest line of a run is that run's full board.
RE_RIT_STREET = re.compile(r"^\*\*\* (FIRST|SECOND) (FLOP|TURN|RIVER) \*\*\* \[([^\]]*)\]"
                           r"(?: \[([^\]]+)\])?", re.MULTILINE)
STREETS = ("FLOP", "TURN", "RIVER")
CENTS = lambda s: int(round(float(s) * 100))


def read_corpus(directory):
    """hand id -> hand text, from every *.txt under directory."""
    hands = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.txt"))):
        text = open(path, encoding="utf8").read().replace("\r\n", "\n")
        for chunk in re.split(r"(?:\s?\n){2,}", text.strip()):
            m = RE_HAND.match(chunk)
            if m:
                hands[int(m.group(1))] = chunk
    return hands


def main(argv=None):
    parser = optparse.OptionParser(usage="%prog [corpus_dir] [options]")
    parser.add_option("--config", default="HUD_config.test.xml")
    (options, args) = parser.parse_args(argv)
    corpus = args[0] if args else os.path.join(
        os.path.expanduser("~"), "Documents", "Projects", "Poker", "history", "games")

    config = Configuration.Config(file=options.config)
    params = config.get_db_parameters()
    if params.get('db-backend') != Database.Database.SQLITE:
        print("FAIL this checker is SQLite-only (DESIGN.md section 5.1); backend is %r"
              % params.get('db-backend'))
        return 1
    # Same path Database.connect() builds for SQLite: dir_database + the selected database name.
    dbfile = os.path.join(config.dir_database, params['db-databaseName'])
    if not os.path.exists(dbfile):
        print("FAIL database %s does not exist -- run TestHandsPlayers.py -s GGPoker first" % dbfile)
        return 1
    db = sqlite3.connect(dbfile)

    hands = read_corpus(corpus)
    if not hands:
        print("FAIL no GGPoker hands found under %s" % corpus)
        return 1

    stored = {int(h): i for h, i in db.execute("select siteHandNo, id from Hands")}
    failures = []

    def check(label, ok, detail=""):
        print("%-4s %s%s" % ("PASS" if ok else "FAIL", label, (" -- " + detail) if detail else ""))
        if not ok:
            failures.append(label)

    # Everything below is scoped to this corpus: the database may hold other sites' or other
    # sessions' hands, and comparing a corpus-wide expectation against a wider table is how a
    # checker starts lying.
    present = set(hands) & set(stored)

    check("corpus hands are all in the database",
          set(hands) <= set(stored),
          "%d raw, %d missing" % (len(hands), len(set(hands) - set(stored))))

    # Money: fpdb stores cents as integers, so every comparison here is exact.
    profit = collections.defaultdict(int)
    winnings = collections.defaultdict(int)
    for hid, p, w in db.execute("select h.siteHandNo, hp.totalProfit, hp.winnings "
                                "from Hands h join HandsPlayers hp on hp.handId = h.id"):
        if int(hid) in present:
            profit[int(hid)] += p
            winnings[int(hid)] += w

    bad_profit, bad_winnings = [], []
    for hid, text in sorted(hands.items()):
        m = RE_MONEY.search(text)
        if not m or hid not in profit:
            bad_profit.append(hid)
            continue
        pot = CENTS(m.group(1))
        deductions = sum(CENTS(m.group(i)) for i in range(2, 7))
        cash_drop = sum(CENTS(x) for x in RE_DROP.findall(text))
        if winnings[hid] != pot - deductions:
            bad_winnings.append(hid)
        if profit[hid] != cash_drop - deductions:
            bad_profit.append(hid)
    check("per hand: sum(winnings) == Total pot - (rake+jackpot+bingo+fortune+tax)",
          not bad_winnings, "%d violations %s" % (len(bad_winnings), bad_winnings[:5]))
    check("per hand: sum(totalProfit) == cashDrop - deductions",
          not bad_profit, "%d violations %s" % (len(bad_profit), bad_profit[:5]))

    rows = [(int(h), r, c) for h, r, c in
            db.execute("select siteHandNo, rush, comment from Hands") if int(h) in present]
    rush = sum(1 for h, r, c in rows if r)
    nocomment = sum(1 for h, r, c in rows if c is None)
    check("every corpus hand is flagged rush and carries a comment",
          rush == len(rows) and nocomment == 0,
          "rush=%d/%d, comment null=%d" % (rush, len(rows), nocomment))

    rit = set(int(h) for h, r in
              db.execute("select siteHandNo, runItTwice from Hands") if r and int(h) in present)
    stored_boards = collections.defaultdict(list)
    for hand_no, board_id, c1, c2, c3, c4, c5 in db.execute(
            "select h.siteHandNo, b.boardId, "
            "b.boardcard1, b.boardcard2, b.boardcard3, b.boardcard4, b.boardcard5 "
            "from Boards b join Hands h on h.id = b.handId"):
        if int(hand_no) in rit:
            stored_boards[(int(hand_no), int(board_id))].extend(
                [Card.valueSuitFromCard(c) for c in (c1, c2, c3, c4, c5) if c and c > 0])
    # Card identities, not just counts. The expectation is derived from the street markers only --
    # '[board so far] [new card]' on the deepest FIRST and SECOND lines -- while the parser builds
    # run 1 from the summary 'FIRST Board' line and run 2 from 'SECOND Board' plus the first
    # marker's prefix. So a pass means the two raw representations of the hand agree, and a
    # reconstruction off by one card fails.
    wrong = []
    for hid in sorted(rit):
        for run, board_id in (("FIRST", 1), ("SECOND", 2)):
            markers = [m.groups() for m in RE_RIT_STREET.finditer(hands[hid]) if m.group(1) == run]
            want = []
            if markers:
                deepest = max(markers, key=lambda g: STREETS.index(g[1]))
                want = deepest[2].split() + (deepest[3] or "").split()
            got = stored_boards.get((hid, board_id))
            if got is None or got != want:
                wrong.append("%s/run%d got=%s want=%s" % (hid, board_id, got, want))
    check("run-it-twice hands have Boards rows 1 and 2 holding exactly the cards the "
          "histories deal",
          not wrong, "rit hands=%d, wrong=%s" % (len(rit), wrong[:4]))

    hero_rows = [(int(h), c1, c4) for h, c1, c4 in db.execute(
        "select h.siteHandNo, hp.card1, hp.card4 from HandsPlayers hp "
        "join Players p on p.id = hp.playerId join Hands h on h.id = hp.handId "
        "where p.name = 'Hero'") if int(h) in present]
    no_cards = [h for h, c1, c4 in hero_rows if c1 <= 0]
    check("every corpus hand has a Hero row with hole cards", not no_cards,
          "%d Hero rows without cards, %d Hero rows total" % (len(no_cards), len(hero_rows)))

    # Exactly the revealed players, per hand -- no more (the parser would be inventing cards) and
    # no fewer (it would be dropping showdowns, doc section 6).
    revealed = set()
    for hid, text in hands.items():
        for rx in (RE_BODY_SHOWN, RE_SUMMARY_SHOWN):
            for name in rx.findall(text):
                revealed.add((hid, name))
        revealed.add((hid, 'Hero'))
    stored_cards = set()
    for hid, name in db.execute(
            "select h.siteHandNo, p.name from HandsPlayers hp join Players p on p.id = hp.playerId "
            "join Hands h on h.id = hp.handId where hp.card1 > 0"):
        if int(hid) in present:
            stored_cards.add((int(hid), name))
    check("rows with visible cards == every Hero row + every revealed player, exactly",
          stored_cards == revealed,
          "db=%d raw=%d, only-in-raw=%s only-in-db=%s"
          % (len(stored_cards), len(revealed),
             sorted(revealed - stored_cards)[:3], sorted(stored_cards - revealed)[:3]))

    four_cards = sum(1 for h, c1, c4 in hero_rows if c4 > 0)
    raw_plo = sum(1 for t in hands.values() if 'Omaha' in t.splitlines()[0])
    check("Hero holds four cards in exactly the Omaha hands", four_cards == raw_plo,
          "db=%d, raw Omaha=%d" % (four_cards, raw_plo))

    actions = sum(1 for h in db.execute("select h.siteHandNo from HandsActions ha "
                                        "join Hands h on h.id = ha.handId")
                  if int(h[0]) in present)
    print("\n%d hands, %d actions, %d players, $%.2f pot, $%.2f deductions, $%.2f cash drop" % (
        len(present), actions,
        db.execute("select count(*) from Players").fetchone()[0],
        sum(CENTS(RE_MONEY.search(t).group(1)) for t in hands.values()) / 100.0,
        sum(sum(CENTS(RE_MONEY.search(t).group(i)) for i in range(2, 7))
            for t in hands.values()) / 100.0,
        sum(sum(CENTS(x) for x in RE_DROP.findall(t)) for t in hands.values()) / 100.0))
    print("%d FAILURES" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
