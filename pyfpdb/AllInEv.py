#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""All-in expected value per seat, straight from the fpdb database.

`HandsPlayers.allInEV` is 0 for all 2070 seats in the corpus, because fpdb filled it through
`pokereval` -- a C extension this venv cannot install (DESIGN.md 5.1). That left the results graph
with three of the four lines its owner asked for. This module computes the missing one, in cents.

A hand is worth its recorded result unless the seat put its stack in; there, the result is replaced by
what the seat was *running for*, weighted by its equity at the moment the money went in:

    EV = share x (the pot slices it could win, net of the fee) - what it put in

The pot is built the way a dealer builds it (`pot_slices`): the antes first, then one slice per stack
level, each slice contested only by the seats that covered it and had not folded. That is what makes a
short all-in, a covered all-in and an uncalled over-bet come out of the same code, and it replaces the
single `run_for` this file used to assume.

Two numbers are read from the hand rather than assumed:

* the fee. GG deducts a jackpot drop from the pot at showdown and fpdb stores it per seat in
  `HandsPlayers.rake`, so the model subtracts that measured amount from the slices, pro rata. Nothing
  here guesses a drop rate.
* the run-out. Equity comes from `HandEval.equity`, over the cards genuinely unknown when the money
  went in: the board still to come, plus any caller whose cards never showed. `HandEval` enumerates
  those exactly when the number of run-outs is small enough and samples them otherwise, and says which
  it did; the detail dict carries `method`, so a sampled figure can always be told from an exact one.

Because the slices and the fee are measured, the arithmetic can be checked against the results the
database already recorded: in a hand whose money went in on the river the run-out is already dealt, so
the predicted profit must equal the recorded `totalProfit` to the cent. `reconcile` runs that over the
corpus and prints the discrepancy distribution instead of asserting a passing number.

    python AllInEv.py                 # reconcile, then report Hero's all-in hands
    python AllInEv.py --write         # fill HandsPlayers.allInEV for every seat
"""
import os
import sys
import time
import sqlite3
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import HandEval

# Verbs that move money into the pot without raising, and the raise verb whose fpdb `amount` field
# means "raise by" rather than "raise to" (Hand.py:1068 and its addRaiseBy callers).
WAGER_VERBS = frozenset(["calls", "bets", "completes", "brings in", "bringin"])
POST_VERBS = frozenset(["ante", "small blind", "big blind", "secondsb"])
RAISE_VERBS = frozenset(["raises"])
FOLD_VERB = "folds"
# How much of the board is already dead when the money goes in on each street, so equity only has to
# run out what is left. fpdb numbers the streets -1 for the blinds, 0 preflop, 1 flop, 2 turn, 3 river.
BOARD_AT_STREET = {0: 0, 1: 3, 2: 4, 3: 5}

ACTIONS_SQL = ("select ha.street, ha.playerId, a.name, ha.amount, ha.raiseTo, ha.amountCalled, "
               "ha.allIn, ha.actionNo from HandsActions ha join Actions a on a.id = ha.actionId "
               "where ha.handId = ? order by ha.actionNo")

# Run-outs drawn when a shove cannot be enumerated exactly. A preflop all-in has ~1.7M run-outs in
# hold'em and ~1.1M in Omaha, which at this scorer's speed is minutes per hand, so those are sampled --
# and 5000 draws put the standard error on a seat's share near 0.7%, i.e. about 1.4 bb on a 200 bb
# all-in. That is well inside the noise the line exists to remove, and it keeps a whole-corpus pass to
# tens of seconds rather than an hour. Raise it if a specific hand's number ever needs to be tighter.
SAMPLES = 5000


def money_in(actions):
    """{pid: [ante_cents, wager_cents]} -- what each seat put in, with the antes kept apart.

    A raise adds `amount` (raise by) plus `amountCalled`, which is fpdb's own convention. Antes are
    tracked separately because they are dead money that everyone still holding cards is running for,
    not a wager that sets a pot level.
    """
    out = defaultdict(lambda: [0, 0])
    for street, pid, verb, amount, raise_to, called, all_in, action_no in actions:
        amount = amount or 0
        if verb in RAISE_VERBS:
            added = amount + (called or 0)
        elif verb in WAGER_VERBS or verb in POST_VERBS:
            added = amount
        else:
            added = 0
        out[pid][0 if street < 0 else 1] += added
    return dict(out)


def folded_pids(actions):
    """The seats that folded at some point. Their money stays in the pot; their equity is nil."""
    return set(pid for _s, pid, verb, *_rest in actions if verb == FOLD_VERB)


def all_in_moments(actions):
    """{pid: (street, actionNo)} -- when each seat last put its stack in, after the blinds.

    A seat's `allIn` flag rides on the wager that moved its stack, so a fold can never be that action;
    the check is written out because a mis-set flag would turn a folded hand into an all-in one. Posting
    a short stack into the blinds (street -1) is not a shove either.
    """
    found = {}
    for street, pid, verb, amount, raise_to, called, all_in, action_no in actions:
        if not all_in or verb == FOLD_VERB or street < 0:
            continue
        at = (street, action_no)
        if pid not in found or at > found[pid]:
            found[pid] = at
    return found


def pot_slices(money, folded):
    """[(slice_cents, [eligible pid, ...]), ...] -- the main pot first, then each side pot.

    The ante pool is the bottom slice, and every seat that did not fold is running for it. Above it
    comes one slice per distinct wager level: the money between the previous level and this one is
    contested only by the seats that covered it, so a seat that folded -- or that was shorter --
    contributes to a slice it cannot win. That is exactly what a side pot is, and it is why an uncalled
    over-bet needs no special case: the slice above the last caller's level is eligible to one seat,
    whose share of it is therefore 1.0.
    """
    wagers = dict((pid, m[1]) for pid, m in money.items())
    slices = []
    antes = sum(m[0] for m in money.values())
    if antes:
        eligible = [p for p in sorted(wagers) if wagers[p] and p not in folded]
        if eligible:
            slices.append((antes, eligible))
    previous = 0
    for level in sorted(set(v for v in wagers.values() if v)):
        amount = sum(min(v, level) - min(v, previous) for v in wagers.values() if v)
        eligible = [p for p, v in sorted(wagers.items()) if v >= level and p not in folded]
        if amount and eligible:
            slices.append((amount, eligible))
        previous = level
    return slices


def split_fee(slices, fee):
    """Cents of `fee` charged against each slice, pro rata, with the rounding remainder on the largest.

    GG takes one drop on the whole pot, so any split across slices is a choice; pro rata is the one
    that leaves a single-slice pot -- which is every all-in pot in this corpus -- exactly right.
    """
    total = sum(a for a, _e in slices)
    if not total or fee <= 0:
        return [0] * len(slices)
    out = [int(fee * a / total) for a, _e in slices]
    if len(slices) > 1:
        biggest = max(range(len(slices)), key=lambda i: slices[i][0])
        out[biggest] += fee - sum(out)
    return out


def seat_evs(con, hand_id, samples=SAMPLES):
    """{pid: (ev_cents, detail)} for every seat in this hand.

    A seat that never went all-in gets its recorded result and a nil detail, because there is nothing
    to replace: its money either won uncontested or it folded, and either way the real number is already
    the expected one. Only an all-in seat gets an equity, and `detail` is non-nil exactly for those, so
    a caller can tell 'this seat chopped an all-in for nothing' apart from 'no all-in here'.
    """
    meta = con.execute(
        "select h.boardcard1, h.boardcard2, h.boardcard3, h.boardcard4, h.boardcard5, "
        "g.category, g.bigBlind from Hands h join Gametypes g on g.id = h.gametypeId "
        "where h.id = ?", (hand_id,)).fetchone()
    if meta is None:
        return {}
    board_full = [c for c in meta[:5] if c]
    game, bb = meta[5], meta[6] or 1
    hole_size = 4 if game == "omahahi" else 2
    seats = con.execute(
        "select hp.playerId, p.name, hp.card1, hp.card2, hp.card3, hp.card4, hp.totalProfit, "
        "hp.rake from HandsPlayers hp join Players p on p.id = hp.playerId where hp.handId = ?",
        (hand_id,)).fetchall()
    actions = con.execute(ACTIONS_SQL, (hand_id,)).fetchall()
    money = money_in(actions)
    moments = all_in_moments(actions)
    slices = pot_slices(money, folded_pids(actions))
    fees = split_fee(slices, sum(s[7] or 0 for s in seats))
    by_pid = dict((s[0], s) for s in seats)
    holes = dict((pid, [c for c in by_pid[pid][2:2 + hole_size] if c])
                 for pid in money if pid in by_pid)
    equity_cache = {}
    out = {}
    for seat in seats:
        pid, actual = seat[0], seat[6] or 0
        put_in = sum(money.get(pid, [0, 0]))
        moment = moments.get(pid)
        if moment is None or put_in <= 0:
            out[pid] = (actual, None)
            continue
        board = board_full[:BOARD_AT_STREET.get(moment[0], 5)]
        ev = -put_in
        contested = 0
        sampled = False
        for (amount, eligible), slice_fee in zip(slices, fees):
            if pid not in eligible:
                continue                  # money this seat put into a pot it cannot win
            key = (tuple(eligible), len(board))
            if key not in equity_cache:
                shares, how = HandEval.equity([holes[p] for p in eligible], board, game,
                                              samples=samples)
                equity_cache[key] = (dict(zip(eligible, shares)), how["method"])
            shares, method = equity_cache[key]
            sampled = sampled or method == "sampled"
            contested += amount
            ev += shares[pid] * (amount - slice_fee)
        out[pid] = (int(round(ev)),
                    {"hand_id": hand_id, "player": seat[1], "street": moment[0], "game": game,
                     "board": board, "put_in": put_in, "pot": sum(a for a, _e in slices),
                     "fee": sum(fees), "contested": contested, "slices": len(slices),
                     "method": "sampled" if sampled else "exact",
                     "actual": actual, "bb": bb})
    return out


def hand_ev(con, hand_id, player):
    """(ev_cents, detail) for one seat in one hand, or (None, reason) when it was not all-in there.

    A convenience wrapper over `seat_evs` for the single-hand tools; the graph and the column fill both
    go through `seat_evs`, which wants every seat at once.
    """
    pid = con.execute("select id from Players where name = ?", (player,)).fetchone()
    if pid is None:
        return None, "no player named %s" % player
    rows = seat_evs(con, hand_id)
    if not rows:
        return None, "no such hand"
    ev, detail = rows.get(pid[0], (None, None))
    return (ev, detail) if detail else (None, "not all-in")


def reconcile(con):
    """Check the pot arithmetic against the results the database already recorded.

    In a hand whose money went in on the river the board is fully dealt, so every seat's share is 0, 1
    or an equal split, and the predicted profit must equal `totalProfit` to the cent. Anything else is a
    bug in the slice, ante or fee handling -- which is the reason to run this instead of trusting the
    model. Hands are skipped, and counted, when a caller's cards never showed: no deterministic answer
    exists to check against, and inventing one would only make the test agree with itself.
    """
    diffs = defaultdict(int)
    checked = no_board = no_cards = no_river = 0
    worst = (0, None)
    for hand_id, in con.execute("select id from Hands order by id"):
        board = con.execute("select boardcard1, boardcard2, boardcard3, boardcard4, boardcard5 "
                            "from Hands where id = ?", (hand_id,)).fetchone()
        if not all(board):
            no_board += 1
            continue                      # no full board, so the run-out is genuinely still luck
        moments = all_in_moments(con.execute(ACTIONS_SQL, (hand_id,)).fetchall())
        if not moments:
            continue
        if max(street for street, _n in moments.values()) < 3:
            no_river += 1
            continue                      # the shove was earlier: the result is not settled by cards
        rows = seat_evs(con, hand_id)
        if any(d["method"] == "sampled" for _e, d in rows.values() if d):
            no_cards += 1
            continue
        for pid, (ev, d) in rows.items():
            if d is None:
                continue
            checked += 1
            diff = abs(ev - d["actual"])
            diffs[diff] += 1
            if diff > worst[0]:
                worst = (diff, (hand_id, pid))
    print("reconcile: %d river all-in seats predicted against their recorded result" % checked)
    print("  hands skipped: %d without a full board, %d all-in before the river, "
          "%d with an unrevealed caller" % (no_board, no_river, no_cards))
    for diff in sorted(diffs):
        print("  off by %d cents: %d seats" % (diff, diffs[diff]))
    if worst[1]:
        print("  worst: hand %d pid %d off by %d cents" % (worst[1][0], worst[1][1], worst[0]))
    return diffs


def fill_column(con, progress=None, samples=SAMPLES):
    """Write `HandsPlayers.allInEV`, and `wentAllInOnStreet`, for the all-in seats.

    Only hands containing an all-in wager need an equity -- 33 of the 345 in this corpus -- so the rest
    of the table is left alone rather than rewritten with its own result. `wentAllInOnStreet` arrives in
    the same pass because fpdb never filled it (NULL for all 2070 seats here) and the graph needs it: it
    is what distinguishes an all-in hand from a hand that merely happens to have allInEV of 0.
    """
    started = time.time()
    hands = [r[0] for r in con.execute(
        "select distinct handId from HandsActions where allIn = 1 and street >= 0 order by handId")]
    written = 0
    for i, hand_id in enumerate(hands):
        for pid, (ev, detail) in seat_evs(con, hand_id, samples).items():
            if detail is None:
                continue
            con.execute("update HandsPlayers set allInEV = ?, wentAllInOnStreet = ? "
                        "where handId = ? and playerId = ?", (ev, detail["street"], hand_id, pid))
            written += 1
        if progress:
            progress(i + 1, len(hands))
    con.commit()
    return written, len(hands), time.time() - started


def _report(con, player="Hero", limit=400):
    """Print every all-in hand this seat played, with the arithmetic behind its EV."""
    hands = [r[0] for r in con.execute(
        "select distinct ha.handId from HandsActions ha join HandsPlayers hp "
        "on hp.handId = ha.handId and hp.playerId = ha.playerId join Players p "
        "on p.id = hp.playerId where p.name = ? and ha.allIn = 1 and ha.street >= 0 "
        "order by ha.handId limit ?", (player, limit))]
    rows = []
    for hand_id in hands:
        ev, detail = hand_ev(con, hand_id, player)
        if ev is None:
            print("  hand %s: %s" % (hand_id, detail))
            continue
        rows.append((hand_id, detail, ev))
    print("%s: %d all-in hands" % (player, len(rows)))
    total_ev = total_actual = 0.0
    for hand_id, d, ev in rows:
        ev_bb = ev / float(d["bb"])
        actual_bb = d["actual"] / float(d["bb"])
        total_ev += ev_bb
        total_actual += actual_bb
        print("  hand %-5s %-8s street %d in %5d pot %5d run %5d fee %3d %-9s EV %+7.1f bb  "
              "actual %+7.1f bb" % (hand_id, d["game"], d["street"], d["put_in"], d["pot"],
                                    d["contested"], d["fee"], "(%s)" % d["method"], ev_bb, actual_bb))
    print("all-in hands: EV %+.1f bb vs actual %+.1f bb, %+.1f bb of variance removed"
          % (total_ev, total_actual, total_ev - total_actual))
    return rows


def database_path():
    """The fpdb database this install uses, from the same config the importer writes."""
    import Configuration
    config = Configuration.Config()
    return os.path.join(config.dir_database, config.get_db_parameters()["db-databaseName"])


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    path = database_path()
    print(path)
    con = sqlite3.connect(path)
    try:
        print("--- reconcile")
        reconcile(con)
        print("--- hero")
        _report(con)
        if "--write" in argv:
            written, hands, elapsed = fill_column(con)
            print("wrote allInEV for %d seats across %d all-in hands in %.1fs"
                  % (written, hands, elapsed))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
