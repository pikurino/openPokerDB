#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""Metagame: decision-level facts about the field, and custom reports built on top of them.

`StatsEngine` answers "how did *I* play". This answers "how does everyone else play, and how do
they play on *this* board" -- a different shape of question, because the unit is the **decision**
and not the hand. One 6-max hand yields about nine population decisions, so the 345-hand GGPoker
corpus is 1,725 opponent seats and ~3,100 actions, of which only some were real choices.

Why a replay and not `HudCache`: the schema already holds many of these numbers per player
(`street0_4BDone`, `foldToStreet3CBChance`, ...), written by `DerivedStats` at import. This module
recomputes from `HandsActions` instead, for two reasons. The cache cannot be grouped by anything
but player and gametype -- no board, no bet size, no spot -- and it is a black box this project had
never verified. Recomputing buys the extra dimensions and turns the cache into an independent
cross-check; `_selftest` asserts the two agree, which is the first evidence that `DerivedStats`
is right.

Measured facts this module rests on, from the 345-hand GGPoker corpus (every hand 6-max, one date)
and re-measured on the 19-site, 1394-cash-hand harness corpus:

* Scope is cash hold'em-family hands: `Gametypes.base == 'hold'` (the same test `Card.py:48-52` uses
  to name a game's streets) and `tourneyId is null` (`Filters`' own default). Stud and draw games
  number streets 0..6 with no flop, turn or river, so 'flop texture' has no meaning there, and a
  tournament's blind is a chip level, not money -- mixing them in was measured to read 658 river bets
  where the cash corpus holds 318.
* Population decisions: faced a 3-bet 176 (68.8% folded), faced a 4-bet **15** (53.3% folded),
  faced a flop bet 155, a turn bet 81, a **river bet 42** (28 folds / 14 calls, a 33.3% call
  rate). The small denominators are the point: at this volume a 4-bet report is 15 observations,
  so every cell carries its `n` and a cell under `min_n` prints "not yet data", never a percentage
  that would be read as a fact. ±5pp on that river-call rate needs ~2,800 hands; ±3pp ~7,800.
* Money is integer cents, as stored; `bb` divides by that hand's own `Gametypes.bigBlind`, the
  same rule `StatsEngine` uses, so a report stays correct across stakes. A raise row puts
  `amount + amountCalled` into the pot -- the raise-by *plus* what it called. `raiseTo - committed`
  is a plausible-looking alternative that is wrong, because `raiseTo` restarts each street: it read
  28 of 347 street pots low, while the rule used here reproduces 341 and never once read high.
* `Hands.street1Pot/2/3` is the pot *once that street's wagering is done*, not before it
  (`Hand.py:1029-1032` marks the total after reading the street), which is what the replay above is
  checked against. The few comparisons that fall short are short by the same amount on every street
  of the hand: preflop dead money in `pot.common` (GGPoker Cash Drop, other sites' bonuses), which
  no action row carries.
* `Hands.texture` is NULL in all 345 rows, so board texture is computed here from card ids.
* Card ids decode as `suit * 13 + rank` with ranks `2..A` and suits in the order **h, d, c, s**:
  id 1 is `2h`, 13 `Ah`, 14 `2d`, 27 `2c`, 52 `As` (cross-checked against `Card.valueSuitFromCard`).
* Opponents' hole cards exist only where a showdown revealed them -- 107 of 1,725 opponent seats,
  and a fold never reveals anything. So strength-conditioned population stats are one-sided by
  construction, which is why every measure here is about *actions*, which are always visible.
* On the river specifically, this replay and `DerivedStats`' own `HudCache` agree to the unit on both
  corpora (36 bets / 14 calls on the GGPoker one, 318 / 242 on the harness) -- which is the first
  independent evidence that the cached stat columns are right.

A report is data, not code -- see `PRESETS` and `run()`:

    {"name": "River calls by river card", "measures": ["river-faced-bet__n", ...],
     "group_by": ["river_effect", "bet_size"], "where": {"date_from": "2026-10-01"},
     "min_n": 20, "split": "field"}

so the GUI can offer any combination of the registries below without a new line of code.
"""

import os
import sys
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import StatsEngine
from StatsEngine import Filters

RANKS = "23456789TJQKA"
SUIT_ORDER = "hdcs"                                  # measured, see the docstring
# The verbs that are postings rather than decisions. Kept local so this module stays importable
# without PySide6 (HudReplayer owns the same list, but it is a GUI module).
POSTINGS = frozenset(["ante", "small blind", "big blind", "secondsb", "bringin", "completes"])
DECISIONS = ("folds", "calls", "raises", "bets", "checks")
STREET_LABEL = {0: "preflop", 1: "flop", 2: "turn", 3: "river"}

REPORT_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "fpdb", "reports")


# ----------------------------------------------------------------------------- cards and texture

def card_name(cid):
    """Database card id -> 'As'. 0 and None mean 'no card'."""
    return "" if not cid else RANKS[(cid - 1) % 13] + SUIT_ORDER[(cid - 1) // 13]


def rank_of(cid):
    return (cid - 1) % 13                            # 0 = two ... 12 = ace


def suit_of(cid):
    return (cid - 1) // 13                           # 0 h, 1 d, 2 c, 3 s


def _rank_set(cards):
    """Ranks on the board, ace counted low as well so a wheel run-out needs no special case."""
    ranks = set(rank_of(c) for c in cards)
    if 12 in ranks:
        ranks.add(-1)
    return ranks


def _longest_run(ranks):
    best = run = 0
    prev = None
    for r in sorted(ranks):
        run = run + 1 if prev is not None and r == prev + 1 else 1
        prev = r
        best = max(best, run)
    return best


def flop_dims(cards):
    """Texture of the first three board cards, split into orthogonal labels.

    Orthogonal is deliberate: 'wet' as a single word hides whether it is wet because of suits or
    because of connectivity, and those two call for different lines.
    """
    ranks = sorted(rank_of(c) for c in cards)
    counts = defaultdict(int)
    suits = defaultdict(int)
    for c in cards:
        counts[rank_of(c)] += 1
        suits[suit_of(c)] += 1
    uniq = sorted(set(ranks))
    max_gap = max((b - a for a, b in zip(uniq, uniq[1:])), default=0)
    if _longest_run(_rank_set(cards)) >= 3:
        structure = "connected"
    elif max_gap <= 2:
        structure = "gapped"
    else:
        structure = "disconnected"
    high = max(ranks)
    most_suit = max(suits.values())
    most_rank = max(counts.values())
    return {
        "flop_pairing": ("trips" if most_rank == 3 else "paired" if most_rank == 2
                         else "unpaired"),
        "flop_suits": ("monotone" if most_suit == 3 else "two-tone" if most_suit == 2
                       else "rainbow"),
        "flop_structure": structure,
        "flop_high": ("ace-high" if high == 12 else "king-high" if high == 11
                      else "queen-high" if high == 10 else "jack-high or lower"),
        "flop_breadth": ("broadway" if min(ranks) >= 8 else "small" if max(ranks) <= 4
                         else "mixed"),
    }


def street_card_dims(board, new_card):
    """What the card that just fell *did* to the cards already out.

    `board` is the 3 (turn) or 4 (river) cards already there. The priority order is the argument:
    a card that pairs the board and simultaneously completes a flush is reported as the pairing,
    because that is the change that moves whose range is now ahead.
    """
    after = list(board) + [new_card]
    board_ranks = [rank_of(c) for c in board]
    suit_count = defaultdict(int)
    before_suit = defaultdict(int)
    for c in after:
        suit_count[suit_of(c)] += 1
    for c in board:
        before_suit[suit_of(c)] += 1
    nr = rank_of(new_card)
    if nr in board_ranks:
        effect = "paired the board"
    elif max(suit_count.values()) >= 4 and max(before_suit.values()) < 4:
        effect = "completed a flush"
    elif _longest_run(_rank_set(after)) >= 5 and _longest_run(_rank_set(board)) < 5:
        effect = "completed a straight"
    elif nr > max(board_ranks):
        effect = "overcard"
    elif nr < min(board_ranks):
        effect = "undercard"
    else:
        effect = "middle card"
    sc = max(suit_count.values())
    return {"effect": effect,
            "suits": "four to a flush" if sc >= 4 else "three of a suit" if sc == 3
                     else "no flush draw"}


# ----------------------------------------------------------------------------- spots

# A spot is the *situation a decision was taken in*, so the same verb means different things in
# different spots and a report can tell them apart.
SPOTS = [
    ("pf-unopened", "Preflop, unopened to act"),
    ("pf-faced-open", "Preflop, faced an open raise"),
    ("pf-faced-3bet", "Preflop, faced a 3-bet"),
    ("pf-faced-4bet", "Preflop, faced a 4-bet"),
    ("pf-faced-5bet", "Preflop, faced a 5-bet or more"),
    ("flop-cbet-spot", "Flop, preflop raiser with no bet out (c-bet spot)"),
    ("flop-first-to-act", "Flop, first to act without the initiative"),
    ("flop-probe-spot", "Flop, checked to (probe spot)"),
    ("flop-faced-bet", "Flop, faced a bet"),
    ("flop-faced-raise", "Flop, faced a raise"),
    ("turn-cbet-spot", "Turn, preflop raiser with no bet out"),
    ("turn-first-to-act", "Turn, first to act without the initiative"),
    ("turn-probe-spot", "Turn, checked to"),
    ("turn-faced-bet", "Turn, faced a bet"),
    ("turn-faced-raise", "Turn, faced a raise"),
    ("river-cbet-spot", "River, preflop raiser with no bet out"),
    ("river-first-to-act", "River, first to act without the initiative"),
    ("river-probe-spot", "River, checked to"),
    ("river-faced-bet", "River, faced a bet"),
    ("river-faced-raise", "River, faced a raise"),
]
SPOT_LABELS = dict(SPOTS)
SPOT_KEYS = [k for k, _ in SPOTS]

BET_SIZE_BUCKETS = ((0.0, 0.25, "tiny (<25% pot)"), (0.25, 0.40, "small (25-40% pot)"),
                    (0.40, 0.60, "half (40-60% pot)"), (0.60, 0.80, "big (60-80% pot)"),
                    (0.80, 1.00, "pot-sized (80-100%)"), (1.00, 99.0, "overbet (>100%)"))


def bet_size_label(pct):
    for lo, hi, label in BET_SIZE_BUCKETS:
        if lo <= pct < hi:
            return label
    return "other"


# ----------------------------------------------------------------------------- decisions

class Decision(object):
    """One decision: who chose what, in which spot, on which board, for how much, and how it went."""

    __slots__ = ("hand_id", "pid", "name", "street", "spot", "act", "position", "gametype_id",
                 "stake", "day", "bet_pct", "pot", "bb", "profit_bb", "won", "revealed", "hero",
                 "all_in", "dims", "check_raised", "faced_bet_pct")

    def dimension(self, key):
        if key == "spot":
            return SPOT_LABELS.get(self.spot, self.spot)
        if key == "action":
            return self.act
        if key == "street":
            return STREET_LABEL.get(self.street, "?")
        if key in ("position", "stake", "day"):
            return getattr(self, key) or "-"
        if key == "who":
            return "Hero" if self.hero else "Field"
        if key == "bet_size":
            return bet_size_label(self.faced_bet_pct) if self.faced_bet_pct else "no bet faced"
        if key == "outcome":
            return "won the hand" if self.won else "lost the hand"
        return self.dims.get(key) or "no board"

    def counters(self):
        """The tallies this decision contributes to, as {counter: amount}.

        Every rate is kept as a numerator/denominator pair rather than a divided float, so rows
        re-aggregate across any grouping without weighting errors.
        """
        s = self.spot
        c = {"__decisions": 1, "%s__n" % s: 1, "%s__%s" % (s, self.act): 1,
             "%s__bb" % s: self.profit_bb}
        if self.won:
            c["%s__won" % s] = 1
        if self.act in ("bets", "raises"):
            c["%s__aggr" % s] = 1
        # The same tally again under `all__`, counting the row rather than a named spot. Without it
        # every measure belongs to one spot, so a report grouped *by* spot -- the first thing anyone
        # asks for -- puts a 3-bet column on the river-bet row and reads as a wall of zeros. Measured
        # exactly that on the shipped presets before this family existed.
        c["all__n"] = 1
        c["all__%s" % self.act] = 1
        c["all__bb"] = self.profit_bb
        if self.won:
            c["all__won"] = 1
        if self.act in ("bets", "raises"):
            c["all__aggr"] = 1
        if self.check_raised:
            c["checkraise__n"] = 1
        if not self.hero:
            c["field__decisions"] = 1
        return c


# ----------------------------------------------------------------------------- the replay

def board_dims(board):
    """Per-street dimension dicts for one board, computed once per hand."""
    out = {0: {}, 1: {}, 2: {}, 3: {}}
    flop = board[:3]
    if len(flop) < 3:
        return out
    base = flop_dims(flop)
    for st in (1, 2, 3):
        out[st] = dict(base)
    if len(board) >= 4:
        turn = street_card_dims(flop, board[3])
        for st in (2, 3):
            out[st]["turn_effect"] = turn["effect"]
            out[st]["turn_suits"] = turn["suits"]
        out[1]["turn_effect"] = "not dealt"
        out[1]["turn_suits"] = "not dealt"
    if len(board) >= 5:
        river = street_card_dims(board[:4], board[4])
        out[3]["river_effect"] = river["effect"]
        out[3]["river_suits"] = river["suits"]
        for st in (1, 2):
            out[st]["river_effect"] = "not dealt"
            out[st]["river_suits"] = "not dealt"
    return out


class Replay(object):
    """Turn filtered hands into `Decision` objects, one pass over `HandsActions` per hand."""

    def __init__(self, conn, filters=None, hero="Hero"):
        self.conn = conn
        self.filters = filters or Filters()
        self.hero = hero
        self._stakes = {}
        # Decision rows the walk reaches but does not turn into a `Decision`, counted by verb, so
        # `_selftest` can prove nothing was lost: raw verb rows == decisions + skipped.
        self.skipped = defaultdict(int)
        # hand_id -> {street: money in the pot once that street's wagering is done}. Measured against
        # Hands.street1Pot/2/3 over the 345-hand corpus: 341 of 347 comparisons agree exactly, 6 have
        # this replay below the column, 0 above. The 6 are hands whose pot holds GGPoker Cash Drop
        # money (GGPokerToFpdb.py:436 puts it in `pot.common`, never in `HandsActions`), so a replay
        # from action rows alone cannot see it. See the docstring for the other model this ruled out.
        self.street_pots = {}
        # Hand ids the walk reached, whatever it decided about them, so `_selftest` can prove the
        # filter and the replay agree on which hands exist.
        self.hands = set()

    def stake_label(self, gid):
        if gid not in self._stakes:
            sb, bb = self.conn.execute("select smallBlind, bigBlind from Gametypes where id = ?",
                                       (gid,)).fetchone() or (0, 0)
            self._stakes[gid] = "$%.2f/$%.2f" % ((sb or 0) / 100.0, (bb or 0) / 100.0)
        return self._stakes[gid]

    def decisions(self):
        """Yield every decision in the filtered corpus, oldest hand first."""
        cur = self.conn.cursor()
        for hid, started in sorted(self.filters.hand_ids(cur), key=lambda r: r[1]):
            for d in self._hand(cur, hid, str(started)[:10]):
                yield d

    def _hand(self, cur, hand_id, day):
        meta = cur.execute(
            "select h.startTime, h.gametypeId, g.bigBlind, g.base, h.boardcard1,"
            " h.boardcard2, h.boardcard3, h.boardcard4, h.boardcard5 from Hands h"
            " join Gametypes g on g.id = h.gametypeId where h.id = ?", (hand_id,)).fetchone()
        if not meta or meta[3] != "hold":
            # Stud and draw games number their streets 0..6 with no flop, turn or river, so a
            # "flop texture" or a "c-bet spot" has no meaning there and the street walk below would
            # index a street it has no label for. `Gametypes.base` is the canonical test -- the same
            # one `Card.py:48-52` uses to decide a game's street names -- and it covers hold'em,
            # Omaha and Omaha Hi/Lo together, all of which do have the three streets this reports on.
            return
        self.hands.add(hand_id)
        if not meta[2]:
            # No big blind means no bb-normalised outcome, so no decisions -- but the rows still
            # have to be accounted for or the conservation check in `_selftest` would pass by
            # accident whenever a hand was dropped whole.
            for verb, n in cur.execute(
                    "select a.name, count(*) from HandsActions ha join Actions a"
                    " on a.id = ha.actionId where ha.handId = ? and a.name in (%s)"
                    " group by 1" % ",".join("?" * len(DECISIONS)),
                    [hand_id] + list(DECISIONS)).fetchall():
                self.skipped[verb] += n
            return
        _started, gid, big_blind = meta[0], meta[1], meta[2]
        board = [c for c in meta[4:9] if c]
        dims = board_dims(board)
        seats = {}
        for pid, name, pos, winnings, profit, card1 in cur.execute(
                "select hp.playerId, p.name, hp.position, hp.winnings, hp.totalProfit, hp.card1"
                " from HandsPlayers hp join Players p on p.id = hp.playerId"
                " where hp.handId = ?", (hand_id,)).fetchall():
            seats[pid] = {"name": name, "pos": "Hero" if name == self.hero
                          else StatsEngine.position_label(pos),
                          "w": winnings or 0, "p": profit or 0, "c": card1 or 0}
        actions = cur.execute(
            "select ha.street, ha.playerId, a.name, ha.amount, ha.raiseTo, ha.amountCalled,"
            " ha.allIn from HandsActions ha join Actions a on a.id = ha.actionId"
            " where ha.handId = ? order by ha.actionNo", (hand_id,)).fetchall()

        committed = defaultdict(int)
        state = defaultdict(lambda: {"aggr": 0, "raises": 0, "actors": 0, "bettor": None,
                                     "checked": set()})
        all_in_street = {}
        last_pf_raiser = None
        pot = 0
        street_end = {}
        prev_street = None
        stake = self.stake_label(gid)

        for street, pid, verb, amount, raise_to, called, all_in in actions:
            if prev_street is not None and street != prev_street:
                street_end[prev_street] = pot
            prev_street = street
            seat = seats.get(pid)
            if seat is None:
                if verb in DECISIONS:
                    self.skipped[verb] += 1
                continue
            # Money first, with fpdb's own semantics: a raise row holds (raise-by, raise-to,
            # amount-called) and the pot grows by the raise-by *plus* the amount called on top of
            # what was already in front of the raiser. `raiseTo - what that player had committed` is
            # not the same rule and is the wrong one: `raiseTo` restarts each street, so that model
            # read 28 of 347 street pots low on this corpus, while this one reproduced 341 of them
            # and was never high. `HudReplayer` uses the same rule for the same reason.
            if verb == "raises":
                added = (amount or 0) + (called or 0)
            elif verb in ("calls", "bets") or verb in POSTINGS:
                added = amount or 0
            else:
                added = 0
            my_before = committed[pid]
            committed[pid] = my_before + added
            pot += added
            if all_in:
                all_in_street[pid] = street
            if verb not in DECISIONS:
                continue
            # A seat already all-in has no further decision: any later row for it is the site
            # recording the uncalled remainder, not a choice.
            if pid in all_in_street and all_in_street[pid] < street:
                self.skipped[verb] += 1
                continue

            st = state[street]
            biggest = max(committed.values()) if committed else 0
            to_call = max(0, biggest - my_before)
            pot_before = pot - added
            faced_pct = (to_call / float(pot_before + to_call)) if to_call and pot_before + to_call else 0.0

            if street == 0:
                spot = ("pf-unopened" if st["raises"] == 0 else
                        "pf-faced-open" if st["raises"] == 1 else
                        "pf-faced-3bet" if st["raises"] == 2 else
                        "pf-faced-4bet" if st["raises"] == 3 else "pf-faced-5bet")
            else:
                name = STREET_LABEL[street]
                if st["aggr"] == 0:
                    # Three different situations with no bet out, and they are not interchangeable:
                    # the preflop raiser betting here is a c-bet, an opponent betting *first to act*
                    # is an opportunistic lead, and an opponent betting after someone checked is a
                    # probe. Lumping them (as an earlier draft did) read 238 "probe spots" on a
                    # 345-hand corpus, which is mostly just checking round to the raiser.
                    if pid == last_pf_raiser:
                        spot = "%s-cbet-spot" % name
                    elif st["checked"]:
                        spot = "%s-probe-spot" % name
                    else:
                        spot = "%s-first-to-act" % name
                else:
                    spot = "%s-faced-bet" % name if st["aggr"] == 1 else "%s-faced-raise" % name

            check_raise = verb == "raises" and pid in st["checked"]
            if verb == "raises":
                st["raises"] += 1
                if street == 0:
                    last_pf_raiser = pid
            if verb in ("bets", "raises"):
                st["aggr"] += 1
                st["bettor"] = pid
            st["actors"] += 1

            d = Decision()
            d.hand_id, d.pid, d.name, d.street, d.spot, d.act = (
                hand_id, pid, seat["name"], street, spot, verb)
            d.position, d.gametype_id, d.stake, d.day = (
                seat["pos"], gid, stake, day)
            d.hero = seat["name"] == self.hero
            d.bet_pct = d.faced_bet_pct = faced_pct
            d.pot, d.bb = pot_before, big_blind
            d.profit_bb = seat["p"] / float(big_blind)
            d.won = seat["w"] > 0
            d.revealed = seat["c"] > 0
            d.all_in = bool(all_in)
            d.dims = dims[street]
            d.check_raised = check_raise
            yield d
        if prev_street is not None:
            street_end[prev_street] = pot
        self.street_pots[hand_id] = street_end


# ----------------------------------------------------------------------------- registries

def _build_measures():
    """Every spot, answered five ways: how often it folds / calls / raises, wins, and earns bb.

    Generated rather than hand-written so a new spot costs nothing, and so the builder's list can
    never drift from the classifier's.
    """
    out = []
    # `all__` first: these are the ones to use whenever the report is *grouped by spot*, because a
    # spot-specific measure on a row that is a different spot is always zero. The labels are kept
    # short on purpose -- they are grid and CSV column headings, and "Of this row's decisions: fold"
    # across five columns pushed the rest of the report off the screen.
    out.append(("all__n", "Decisions", "any spot", "int"))
    for act, alabel in (("folds", "fold"), ("calls", "call"), ("raises", "raise"),
                        ("bets", "bet"), ("checks", "check")):
        out.append(("all__%s" % act, "Any spot: %s" % alabel, "any spot", "pct"))
    out.append(("all__aggr", "Any spot: aggressive (bet or raise)", "any spot", "pct"))
    out.append(("all__won", "Any spot: won the hand", "any spot", "pct"))
    out.append(("all__bb", "Any spot: bb per decision", "any spot", "bb"))
    for spot in SPOT_KEYS:
        label = SPOT_LABELS[spot]
        out.append(("%s__n" % spot, label, "decisions", "int"))
        for act, alabel in (("folds", "fold"), ("calls", "call"), ("raises", "raise"),
                            ("bets", "bet"), ("checks", "check")):
            out.append(("%s__%s" % (spot, act), "%s: %s" % (label, alabel), "reaction", "pct"))
        out.append(("%s__aggr" % spot, "%s: aggressive (bet or raise)" % label, "reaction", "pct"))
        out.append(("%s__won" % spot, "%s: won the hand" % label, "outcome", "pct"))
        out.append(("%s__bb" % spot, "%s: bb per decision" % label, "outcome", "bb"))
    out.append(("checkraise__n", "Check-raises (any street)", "special", "count"))
    out.append(("field__decisions", "Field decisions in scope", "special", "count"))
    return out


MEASURES = _build_measures()
MEASURE_LABEL = {k: label for k, label, family, kind in MEASURES}
MEASURE_FAMILY = {k: family for k, label, family, kind in MEASURES}
MEASURE_KIND = {k: kind for k, label, family, kind in MEASURES}

DIMENSIONS = [
    ("spot", "Spot / situation"),
    ("street", "Street"),
    ("action", "Action taken"),
    ("bet_size", "Facing bet size (% pot)"),
    ("flop_pairing", "Flop: pairing"),
    ("flop_suits", "Flop: suits"),
    ("flop_structure", "Flop: connectivity"),
    ("flop_high", "Flop: highest card"),
    ("flop_breadth", "Flop: card range"),
    ("turn_effect", "Turn card did"),
    ("turn_suits", "Turn card: suits"),
    ("river_effect", "River card did"),
    ("river_suits", "River card: suits"),
    ("position", "Position"),
    ("stake", "Stake"),
    ("day", "Day"),
    ("who", "Hero or field"),
    ("outcome", "Hand outcome"),
]
DIMENSION_LABEL = dict(DIMENSIONS)

# The first street on which each board dimension exists. Grouping by `river_effect` asks a question
# about the river, so a preflop decision or a hand that ended on the turn has no answer for it -- and
# putting those under one label ("not dealt", as an earlier draft did) produced a 1,791-decision row
# that swallowed a flop-texture report whole. The runner drops them and says how many.
DIMENSION_STREET = {}
for _key, _label in DIMENSIONS:
    if _key.startswith("flop_"):
        DIMENSION_STREET[_key] = 1
    elif _key.startswith("turn_"):
        DIMENSION_STREET[_key] = 2
    elif _key.startswith("river_"):
        DIMENSION_STREET[_key] = 3



def measure_denominator(key):
    """The `n` a measure divides by: its spot's own decision count, or None for a raw count.

    Derived from the key rather than stored, so a new measure cannot forget its denominator and
    quietly divide by the whole group.
    """
    base = key.rsplit("__", 1)[0]
    den = base + "__n"
    return den if den in MEASURE_KIND and key != den else None


# ----------------------------------------------------------------------------- the runner

def run(conn, spec, hero="Hero"):
    """Evaluate a report spec. Returns (columns, rows, meta).

    `columns` is the group labels, then `n`, then one column per measure plus a hidden
    `<measure>__n` beside it: a percentage without its denominator is the thing that gets people
    into trouble, so the builder keeps both on the row and lets the view decide what to show.
    `meta` counts the decisions that were left out, so a view can say what it did not plot.
    """
    group_by = list(spec.get("group_by") or [])
    measures = list(spec.get("measures") or [])
    split = spec.get("split", "field")
    min_n = spec.get("min_n", 1)
    filters = Filters(**(spec.get("where") or {}))
    # A grouping dimension this decision has not reached yet disqualifies it, not just its row.
    floor = max([DIMENSION_STREET.get(g, 0) for g in group_by] or [0])
    totals = defaultdict(lambda: defaultdict(float))
    seen = out_of_scope = 0
    for d in Replay(conn, filters, hero).decisions():
        if split == "field" and d.hero:
            continue
        if split == "hero" and not d.hero:
            continue
        seen += 1
        if d.street < floor:
            out_of_scope += 1
            continue
        key = tuple(d.dimension(g) for g in group_by) if group_by else ("all decisions",)
        bucket = totals[key]
        for cname, val in d.counters().items():
            bucket[cname] += val
    columns = [(DIMENSION_LABEL[g], g) for g in group_by]
    columns.append(("decisions", "n"))
    for m in measures:
        columns.append((MEASURE_LABEL.get(m, m), m))
    rows = []
    for key, c in totals.items():
        row = {}
        for i, g in enumerate(group_by):
            row[g] = key[i]
        row["n"] = int(c.get("__decisions", 0))
        for m in measures:
            den = measure_denominator(m)
            kind = MEASURE_KIND.get(m, "count")
            num = c.get(m, 0)
            d = c.get(den, 0) if den else row["n"]
            if kind == "pct":
                row[m] = None if d < min_n else 100.0 * num / d
            elif kind == "bb":
                row[m] = None if d < min_n else num / d
            else:
                row[m] = int(num)
            # Both halves of every cell stay on the row, not just the divided one: a view that shows
            # "33.3%" has to be able to say 14/42 next to it, and reconstructing the numerator from
            # the percentage rounds. A raw count has no denominator, so it gets none -- `14 of 295`
            # beside a column that means "14 bets" reads as a rate it never claimed to be.
            row[m + "__n"] = 0 if kind in ("int", "count") else int(d)
            row[m + "__num"] = int(num) if kind != "bb" else num
        rows.append(row)
    rows.sort(key=lambda r: -r["n"])
    return columns, rows, {"decisions": seen, "out_of_scope": out_of_scope,
                           "grouped": sum(r["n"] for r in rows)}


# ----------------------------------------------------------------------------- the grid shape

def cell_text(kind, value):
    """How a measure reads in a grid cell. A suppressed cell says so rather than printing a dash.

    "not yet data" is deliberate: the reason a 15-observation 4-bet report is dangerous is that a
    blank looks like zero rather than like nothing measured yet.
    """
    if value is None:
        return "not yet data"
    if kind == "pct":
        return "%.1f%%" % value
    if kind == "bb":
        return "%.2f" % value
    if kind == "money":
        return money_text(value)
    return "%d" % value


def money_text(cents):
    return ("-$%.2f" if cents < 0 else "$%.2f") % (abs(cents) / 100.0)


def grid_report(conn, spec, hero="Hero"):
    """The same report as `run()`, in the row shape `HudMain.StatsModel` already renders.

    One grid for every tab is the point: the population tab gets the theme, the column widths, the
    money colours, the tooltips and the CSV export for free, and cannot drift into a second widget
    that renders the same number a different way. Column 0 is the group label, so a multi-dimension
    grouping joins its values -- 'two-tone / paired / connected' -- exactly as HM2 labels a
    texture-split row.
    """
    group_by = list(spec.get("group_by") or [])
    measures = list(spec.get("measures") or [])
    columns, rows, meta = run(conn, spec, hero)
    fields = {}
    for m in measures:
        fields[m] = (MEASURE_LABEL.get(m, m), MEASURE_KIND.get(m, "count"))
    out = []
    for row in rows:
        label = " / ".join(str(row[g]) for g in group_by) if group_by else "all decisions"
        cell = {"group": (label,), "_label": label, "_counters": {"hands": row["n"]},
                "n": row["n"]}
        for m in measures:
            kind = MEASURE_KIND.get(m, "count")
            value = row[m]
            cell[m] = (cell_text(kind, value),
                       row[m + "__num"] if kind != "bb" else value,
                       row[m + "__n"])
        out.append(cell)
    return out, measures, fields, meta



# ----------------------------------------------------------------------------- presets and files

PRESETS = [
    {"name": "Field reaction by spot",
     "measures": ["all__n", "all__folds", "all__calls", "all__raises", "all__aggr", "all__won",
                  "all__bb"],
     "group_by": ["spot"], "min_n": 15, "split": "field"},
    {"name": "Field reaction by street",
     "measures": ["all__n", "all__folds", "all__calls", "all__raises", "all__aggr", "all__bb"],
     "group_by": ["street"], "min_n": 15, "split": "field"},
    {"name": "River calls by the river card",
     "measures": ["river-faced-bet__n", "river-faced-bet__calls", "river-faced-bet__folds",
                  "river-faced-bet__won", "river-faced-bet__bb"],
     "group_by": ["river_effect"], "min_n": 10, "split": "field"},
    {"name": "Fold to a 3-bet and 4-bet",
     "measures": ["pf-faced-open__n", "pf-faced-open__folds", "pf-faced-open__calls",
                  "pf-faced-open__raises", "pf-faced-3bet__folds", "pf-faced-3bet__calls",
                  "pf-faced-3bet__raises", "pf-faced-4bet__folds", "pf-faced-4bet__calls",
                  "pf-faced-4bet__raises"],
     "group_by": [], "min_n": 15, "split": "field"},
    {"name": "C-bet frequency by flop texture",
     "measures": ["flop-cbet-spot__n", "flop-cbet-spot__bets", "flop-cbet-spot__checks",
                  "flop-cbet-spot__bb"],
     "group_by": ["flop_suits", "flop_pairing", "flop_structure"], "min_n": 10, "split": "field"},
    {"name": "What the field does on a paired turn",
     "measures": ["turn-faced-bet__n", "turn-faced-bet__folds", "turn-faced-bet__calls",
                  "turn-cbet-spot__bets", "turn-cbet-spot__n"],
     "group_by": ["turn_effect"], "min_n": 10, "split": "field"},
    {"name": "Bet size the field faces, and how it answers",
     "measures": ["all__n", "all__folds", "all__calls", "all__raises"],
     "group_by": ["bet_size", "street"], "min_n": 10, "split": "field"},
    {"name": "Hero against the field, same row",
     "measures": ["all__n", "all__folds", "all__calls", "all__raises", "all__bb"],
     "group_by": ["who", "street"], "min_n": 1, "split": "both"},
]


def save_spec(spec, directory=None):
    """Persist a report so it survives a restart and can be shared as a file."""
    directory = directory or REPORT_DIR
    if not os.path.isdir(directory):
        os.makedirs(directory)
    name = "".join(ch for ch in str(spec.get("name", "report")) if ch not in '\\/:*?"<>|')
    path = os.path.join(directory, "%s.report.json" % (name or "report"))
    with open(path, "w") as handle:
        json.dump(spec, handle, indent=1, sort_keys=True)
    return path


def load_specs(directory=None):
    directory = directory or REPORT_DIR
    out = list(PRESETS)
    if os.path.isdir(directory):
        for fn in sorted(os.listdir(directory)):
            if fn.endswith(".report.json"):
                try:
                    with open(os.path.join(directory, fn)) as handle:
                        spec = json.load(handle)
                except (ValueError, IOError):
                    continue
                if isinstance(spec, dict) and spec.get("measures"):
                    out.append(spec)
    return out


# ----------------------------------------------------------------------------- self-test

def _selftest(conn, hero="Hero"):
    """Reconcile the replay against raw SQL and against `HudCache`, and print each check.

    The conservation law is the load-bearing one: every `HandsActions` row with a decision verb
    belongs to exactly one spot, so summing the spots must reproduce the raw verb counts exactly.
    A spot model that double-counts or drops a situation cannot survive that, and the check costs
    nothing to keep.
    """
    cur = conn.cursor()
    checks, fails = [], []

    def check(label, got, want):
        checks.append(label)
        if got != want:
            fails.append("%s: got %r want %r" % (label, got, want))
        print("%-58s %-14s %s" % (label, "" if got == want else "FAIL", got))

    replay = Replay(conn, Filters(), hero)
    decs = list(replay.decisions())
    # The same scope the replay walks, read straight out of the schema: hold'em-family (`base`) and
    # cash (`tourneyId is null`, which is `Filters`' own default). Naming both here is what makes the
    # conservation law below mean something -- without it a corpus of tournament hands silently
    # halves every count and the check passes on the hands that happened to agree.
    cash_holdem = cur.execute("select count(*) from Hands h join Gametypes g"
                              " on g.id = h.gametypeId"
                              " where g.base = 'hold' and h.tourneyId is null").fetchone()[0]
    raw = dict(cur.execute("select a.name, count(*) from HandsActions ha join Actions a"
                           " on a.id = ha.actionId"
                           " join Hands h on h.id = ha.handId"
                           " join Gametypes g on g.id = h.gametypeId and g.base = 'hold'"
                           " where h.tourneyId is null and a.name in (%s)"
                           % ",".join("?" * len(DECISIONS))
                           + " group by 1", DECISIONS).fetchall())
    check("the replay reached every cash hold'em hand the filter selected",
          len(replay.hands), cash_holdem)
    for verb in DECISIONS:
        check("decisions of type %s == raw HandsActions rows" % verb,
              sum(1 for d in decs if d.act == verb) + replay.skipped.get(verb, 0), raw.get(verb, 0))
    check("no decision sits outside the spot list",
          sum(1 for d in decs if d.spot not in SPOT_LABELS), 0)
    check("a decision with no board dimension is only a preflop one",
          sum(1 for d in decs if d.street and not d.dims), 0)

    # The pot model, against the columns the importer itself wrote. `Hands.streetNPot` is the pot
    # once street N's wagering is done, not before it -- `Hand.py:1029-1032` calls `markTotal(street)`
    # *after* reading that street's actions, and the replay reproduces that reading on 341 of the
    # 347 comparisons on the GGPoker corpus and 2067 of 2138 across the 19-site harness. Two rules
    # carry the rest, and both are site-independent:
    #   * the replay never exceeds the column. A replay that claims more money than the site recorded
    #     is a broken model, not a gap in the data, so `above` must stay 0 on any corpus.
    #   * when it falls short, it falls short by the *same* amount on every street of that hand.
    #     Measured on all 32 shortchanging hands across both corpora: never varies. That is what
    #     preflop dead money looks like -- `pot.common` (GGPoker Cash Drop, other sites' bonuses and
    #     forfeited antes) is summed into `markTotal` but never written as an action row, so it is
    #     missing from all three streets equally. A shortfall that *moved* between streets would say
    #     the raise model mis-added one street, which is the bug this check exists to catch.
    agree = below = above = varying = short_hands = compared = 0
    for hid, ends in replay.street_pots.items():
        row = cur.execute("select street1Pot, street2Pot, street3Pot from Hands where id = ?",
                          (hid,)).fetchone()
        gaps = [col - ends[st] for st, col in ((1, row[0]), (2, row[1]), (3, row[2]))
                if col and st in ends]
        compared += len(gaps)
        agree += sum(1 for g in gaps if not g)
        above += sum(1 for g in gaps if g < 0)
        below += sum(1 for g in gaps if g > 0)
        short_hands += any(g > 0 for g in gaps)
        if len(set(g for g in gaps if g)) > 1:
            varying += 1
    check("replay never exceeds the stored street pot", above, 0)
    check("a shortfall is the same on every street of its hand (dead money)", varying, 0)
    # The two rules above are not enough on their own, and a sabotage run proved it: dropping the
    # called part out of a *preflop* raise makes every street of that hand short by the same amount,
    # so it looks exactly like dead money and slips past both. This floor is what catches it. It is a
    # floor and not an equality because dead money is real and its share varies by site.
    check("the model reproduces the importer's street pots (measured 98.3% and 96.7%)",
          100.0 * agree / compared >= 95.0, True)
    # The sharper version of the same idea: the *change* from one stored street pot to the next is the
    # money wagered on that street alone, so it cannot be cancelled out by an error on an earlier
    # street. Exact on both corpora -- 171 of 171 comparable steps on the GGPoker one, 1156 of 1156
    # on the 19-site one -- so a single disagreement names the street that is wrong.
    steps = steps_bad = 0
    for hid, ends in replay.street_pots.items():
        row = cur.execute("select street1Pot, street2Pot, street3Pot from Hands where id = ?",
                          (hid,)).fetchone()
        for first, second, col_a, col_b in ((1, 2, row[0], row[1]), (2, 3, row[1], row[2])):
            if not col_b or first not in ends or second not in ends:
                continue                          # the hand ended, or was all-in before this street
            steps += 1
            steps_bad += (ends[second] - ends[first]) != (col_b - col_a)
    check("money wagered on each street == the importer's pot-to-pot step", steps_bad, 0)
    print("%-58s %d of %d agreed, %d short on %d hands; %d street steps"
          % ("  street-pot reconciliation", agree, compared, below, short_hands, steps))

    # `HudCache` is DerivedStats' own output: if the two agree, both are probably right. Scoped to
    # hold'em-family gametypes *and* to gametypes that actually carry cash hands, for the same reason
    # the replay is scoped that way -- `Filters` excludes tournament hands. Unscoped on the 19-site
    # harness the cache read 658 river bets against this replay's 318, and every one of the
    # difference was a tour gametype. Skipped where the cache is empty: an import that never ran
    # `DerivedStats` is an absence, not a disagreement.
    cached = cur.execute("select coalesce(sum(street3Bets),0), coalesce(sum(street3Calls),0),"
                         " count(*) from HudCache c join Gametypes g on g.id = c.gametypeId"
                         " and g.base = 'hold' where exists (select 1 from Hands h"
                         " where h.gametypeId = c.gametypeId and h.tourneyId is null)").fetchone()
    mine = (sum(1 for d in decs if d.street == 3 and d.act == "bets"),
            sum(1 for d in decs if d.street == 3 and d.act == "calls"))
    if cached[2]:
        check("river bets: this replay == HudCache.street3Bets", mine[0], cached[0])
        check("river calls: this replay == HudCache.street3Calls", mine[1], cached[1])
    else:
        print("%-58s (no rows)" % "HudCache cross-check")

    # Texture, on boards named by card id so the labels cannot drift with the code.
    def ids(names):
        return [RANKS.index(n[:-1]) + 1 + 13 * SUIT_ORDER.index(n[-1]) for n in names]

    def one(name):
        return ids([name])[0]
    check("As Kh Qh flop is two-tone broadway",
          (flop_dims(ids(["As", "Kh", "Qh"]))["flop_suits"],
           flop_dims(ids(["As", "Kh", "Qh"]))["flop_breadth"]), ("two-tone", "broadway"))
    check("9h 8h 7h flop is monotone and connected",
          (flop_dims(ids(["9h", "8h", "7h"]))["flop_suits"],
           flop_dims(ids(["9h", "8h", "7h"]))["flop_structure"]), ("monotone", "connected"))
    check("Ah Ad 2c flop is paired, ace-high, rainbow",
          (flop_dims(ids(["Ah", "Ad", "2c"]))["flop_pairing"],
           flop_dims(ids(["Ah", "Ad", "2c"]))["flop_high"],
           flop_dims(ids(["Ah", "Ad", "2c"]))["flop_suits"]), ("paired", "ace-high", "rainbow"))
    check("Th into 9h 8h 7h completes a flush",
          street_card_dims(ids(["9h", "8h", "7h"]), one("Th"))["effect"], "completed a flush")
    check("Ad into Ah Kd 7c pairs the board",
          street_card_dims(ids(["Ah", "Kd", "7c"]), one("Ad"))["effect"], "paired the board")
    check("Kh into 9d 7c 4h is an overcard",
          street_card_dims(ids(["9d", "7c", "4h"]), one("Kh"))["effect"], "overcard")
    check("8c into Ah Kd 7c is a middle card",
          street_card_dims(ids(["Ah", "Kd", "7c"]), one("8c"))["effect"], "middle card")
    check("2d into Ah Kd 7c is an undercard",
          street_card_dims(ids(["Ah", "Kd", "7c"]), one("2d"))["effect"], "undercard")
    check("Tc into 9h 8d 7c 6s completes a straight",
          street_card_dims(ids(["9h", "8d", "7c", "6s"]), one("Tc"))["effect"],
          "completed a straight")
    check("5c into Ah 2d 3c 4s completes the wheel",
          street_card_dims(ids(["Ah", "2d", "3c", "4s"]), one("5c"))["effect"],
          "completed a straight")
    check("card id 1 is 2h and id 52 is As", (card_name(1), card_name(52)), ("2h", "As"))

    # A report runs, and a percentage whose denominator is too small is refused. Grouping by street
    # puts several river-free rows on the same grid, so the checks name the river row rather than
    # taking whichever row sorted first.
    def river_row(rows):
        for r in rows:
            if r["street"] == "river":
                return r
        return {}    # The row-generic family has to be the same tally as the spot family seen from the other side:
    # grouped by spot, `all__n` on each row is that spot's own count, so the two must add to the same
    # decisions. A drift here means `counters()` counts one family and not the other.
    _c, spot_rows, _m = run(conn, {"name": "t", "measures": ["all__n", "all__folds", "all__calls",
                                                             "all__raises", "all__bets",
                                                             "all__checks"],
                                   "group_by": ["spot"], "min_n": 1, "split": "both"})
    check("grouped by spot, all__n on each row is that spot's own count",
          sorted((r["spot"], r["all__n"]) for r in spot_rows),
          sorted((SPOT_LABELS[s], sum(1 for d in decs if d.spot == s))
                 for s in set(d.spot for d in decs)))
    check("the row-generic verbs add to the row",
          sum(r["all__folds__num"] + r["all__calls__num"] + r["all__raises__num"]
              + r["all__bets__num"] + r["all__checks__num"] for r in spot_rows), len(decs))

    n_river = sum(1 for d in decs if d.spot == "river-faced-bet" and not d.hero)

    def river_cell(min_n):
        return river_row(run(conn, {"name": "t", "measures": ["river-faced-bet__calls"],
                                     "group_by": ["street"], "min_n": min_n,
                                     "split": "field"})[1])
    check("min_n above the whole corpus suppresses every rate",
          all(r["river-faced-bet__calls"] is None for r in
              run(conn, {"name": "t", "measures": ["river-faced-bet__calls"],
                         "group_by": ["street"], "min_n": 10 ** 6, "split": "field"})[1]), True)
    check("with min_n 1 the same cell is a number",
          isinstance(river_cell(1).get("river-faced-bet__calls"), float), True)
    check("its denominator is the river-bet count, not the whole street's",
          river_cell(1).get("river-faced-bet__calls__n"), n_river)
    # The boundary is on the spot's own denominator, so one decision more than the sample kills the
    # cell and exactly the sample keeps it. Derived from `n_river` rather than hard-coded, because
    # the same check has to bite on a 345-hand corpus and a 4,000-hand one.
    check("a sample one short of min_n prints nothing rather than a thin fact",
          river_cell(n_river + 1).get("river-faced-bet__calls"), None)
    check("a sample exactly at min_n prints the rate",
          river_cell(n_river).get("river-faced-bet__calls") is not None, True)
    # Splitting by Hero/Field must not lose or invent decisions: the two halves add back to all of
    # them, which is the property the GUI's "population excludes Hero" default rests on.
    for split, want in (("field", sum(1 for d in decs if not d.hero)),
                        ("hero", sum(1 for d in decs if d.hero)),
                        ("both", len(decs))):
        check("split %s totals the decisions it should" % split,
              sum(r["n"] for r in run(conn, {"name": "t", "measures": [], "group_by": [],
                                             "split": split})[1]), want)
    print("selftest: %d checks, %d failures" % (len(checks), len(fails)))
    for f in fails:
        print("  FAIL " + f)
    return len(fails)


def main(argv=None):
    import sqlite3
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else os.path.join(
        os.environ.get("APPDATA", ""), "fpdb", "database", "fpdb.db3")
    conn = sqlite3.connect(path)
    try:
        return 1 if _selftest(conn) else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
