#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""Hand scoring and all-in equity, in pure Python, for the all-in-EV graph line.

fpdb computed `HandsPlayers.allInEV` with `pokereval`, a C extension that cannot be installed in
this venv (DESIGN.md 5.1: no pip here, and no Windows wheel for 3.12), so the column is 0 for all
2070 seats in the corpus and the graph has no EV line to draw. This module supplies it.

Scoring follows the game's rules directly rather than a premise about them: a hand's value is one of
the nine categories, and within a category the tiebreak ranks decide it, packed into a single
integer so comparisons are cheap. Hold'em scores the best five of up to seven cards; Omaha hi is
restricted to exactly two hole cards and three board cards, which is the rule that makes the two
games differ.

Equity is exact when the unknown cards can be enumerated inside `exact_limit` combinations, and a
seeded sample when they cannot -- with the method and the sample count returned alongside the
number, so a caller can never present a sampled figure as an exact one.

Card ids are fpdb's (`Card.encodeCardList`): 1-52 run 2h..Ah, 2d..Ad, 2c..Ac, 2s..As, and 0 means
"not dealt", which is what `HandsPlayers.card1` holds for a seat that never showed.

    python HandEval.py            # self-test
"""
import os
import sys
import time
import random
import itertools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import Card

RANK_OF = [0] * 53
SUIT_OF = [0] * 53
for _cid in range(1, 53):
    _name = Card.valueSuitFromCard(_cid)
    RANK_OF[_cid] = "23456789TJQKA".index(_name[0]) + 2          # 2..14, ace high
    SUIT_OF[_cid] = "hdcs".index(_name[1])

DECK = tuple(range(1, 53))

# score() is called tens of millions of times a session by the sampled equities, so the two things it
# needs from a card id -- its rank and its rank bit -- are tables rather than shifts.
BIT_OF = [0] * 53
for _cid in range(1, 53):
    BIT_OF[_cid] = 1 << RANK_OF[_cid]

STRAIGHT_FLUSH, QUADS, FULL_HOUSE, FLUSH, STRAIGHT, TRIPS, TWO_PAIR, PAIR, HIGH = range(8, -1, -1)
_CATEGORY_NAMES = {STRAIGHT_FLUSH: "straight flush", QUADS: "quads", FULL_HOUSE: "full house",
                   FLUSH: "flush", STRAIGHT: "straight", TRIPS: "trips", TWO_PAIR: "two pair",
                   PAIR: "one pair", HIGH: "high card"}


def _pack(category, kickers):
    """Category in the top bits, then the tiebreak ranks four bits each, left-padded with zeros."""
    score = category
    for rank in kickers[:5]:
        score = (score << 4) | rank
    return score << ((5 - min(len(kickers), 5)) * 4)


def _straight_top(bits):
    """The highest rank that ends a five-card run present in `bits` (bit r set => rank r held).

    The wheel is the one case where the ace plays low, so the run A-5 is checked separately as 5-4-3-2
    plus the ace. This is the reference implementation; `score` uses the table built from it.
    """
    for top in range(14, 5, -1):
        window = 0x1F << (top - 4)
        if bits & window == window:
            return top
    if bits & (1 << 14) and (bits & 0x3C) == 0x3C:
        return 5
    return 0


# Every five-card hand is decided by its rank pattern, so the two things that repeat per hand --
# "is there a straight in this set of ranks" and the sorted ranks themselves -- are turned into a
# table lookup and a sort. Enumeration calls this tens of millions of times a session.
_STRAIGHT_TOP = [_straight_top(bits) for bits in range(1 << 15)]


def score(cards):
    """Score exactly five fpdb card ids as one comparable integer.

    Higher beats higher and equal ties, which is all `equity` needs. Six- and seven-card hands are
    `best_holdem`'s job: it takes the best five, because no shortcut over the 21 subsets is correct
    for every pattern (a seven-card 'full house' can hide a better one).
    """
    c0, c1, c2, c3, c4 = cards
    ranks = [RANK_OF[c0], RANK_OF[c1], RANK_OF[c2], RANK_OF[c3], RANK_OF[c4]]
    ranks.sort(reverse=True)
    a, b, c, d, e = ranks
    flush = (SUIT_OF[c0] == SUIT_OF[c1] == SUIT_OF[c2] == SUIT_OF[c3] == SUIT_OF[c4])
    top = _STRAIGHT_TOP[BIT_OF[c0] | BIT_OF[c1] | BIT_OF[c2] | BIT_OF[c3] | BIT_OF[c4]]
    if top:
        return _pack(STRAIGHT_FLUSH if flush else STRAIGHT, [top])
    if flush:
        return _pack(FLUSH, ranks)
    if a == d or b == e:                              # quads, with the odd card as kicker
        return _pack(QUADS, [a, e] if a == d else [b, a])
    if a == c and d == e:                             # trips and a pair
        return _pack(FULL_HOUSE, [a, d])
    if a == b and c == e:                             # a pair over trips
        return _pack(FULL_HOUSE, [c, a])
    if a == c:
        return _pack(TRIPS, [a, d, e])
    if b == d:
        return _pack(TRIPS, [b, a, e])
    if c == e:
        return _pack(TRIPS, [c, a, b])
    if a == b:
        if c == d:
            return _pack(TWO_PAIR, [a, c, e])
        if d == e:
            return _pack(TWO_PAIR, [a, d, c])
        return _pack(PAIR, [a, c, d, e])
    if b == c:
        if d == e:
            return _pack(TWO_PAIR, [b, d, a])
        return _pack(PAIR, [b, a, d, e])
    if c == d:
        return _pack(PAIR, [c, a, b, e])
    if d == e:
        return _pack(PAIR, [d, a, b, c])
    return _pack(HIGH, ranks)


def best_holdem(hole, board):
    """Best score from a hold'em hand: the two hole cards and 3-5 board cards."""
    cards = list(hole) + list(board)
    if len(cards) < 5:
        raise ValueError("hold'em needs at least five cards to score")
    if len(cards) == 5:
        return score(cards)
    return max(score(combo) for combo in itertools.combinations(cards, 5))


def best_omaha(hole, board):
    """Best score from an Omaha hand: exactly two hole cards and exactly three board cards."""
    if len(board) < 3:
        raise ValueError("Omaha needs at least three board cards")
    best = 0
    for h in itertools.combinations(hole, 2):
        for b in itertools.combinations(board, 3):
            value = score(list(h) + list(b))
            if value > best:
                best = value
    return best


def scorer(game):
    """The per-game function that turns (hole, board) into a score."""
    if game == "omahahi":
        return best_omaha
    return best_holdem


# --------------------------------------------------------------------------- equity


def equity(hands, board, game="holdem", exact_limit=250000, samples=20000, seed=20261014):
    """Each seat's share of the pot, as (shares, {'method', 'combinations', 'samples'}).

    `hands` is one hole-card list per seat; an empty list means a seat whose cards were never seen,
    which is dealt from the unseen deck like any other unknown. `board` is what is already out.

    Below `exact_limit` combinations every way the cards can fall is enumerated and the answer is
    exact. Above it the same quantity is estimated by a seeded sample -- reproducible hand for hand,
    but an estimate, and `method` says so. Callers must show that distinction rather than pass a
    sampled number off as the true one.
    """
    known = set(board)
    for hole in hands:
        known.update(hole)
    unseen = [c for c in DECK if c not in known]
    hole_size = 4 if game == "omahahi" else 2
    hidden = [i for i, hole in enumerate(hands) if not hole]
    need_board = 5 - len(board)
    draw = need_board + hole_size * len(hidden)
    combos = _combinations(len(unseen), draw)
    evaluate = scorer(game)
    seats = [list(h) for h in hands]
    shares = [0.0] * len(hands)
    if combos <= exact_limit:
        for picked in itertools.combinations(unseen, draw):
            at = need_board
            for i in hidden:
                seats[i] = list(picked[at:at + hole_size])
                at += hole_size
            full_board = list(board) + list(picked[:need_board])
            _add_shares(shares, [evaluate(seats[i], full_board) for i in range(len(seats))])
        shares = [s / combos for s in shares]
        return shares, {"method": "exact", "combinations": combos, "samples": combos}
    rng = random.Random(seed)
    for _ in range(samples):
        deck = rng.sample(unseen, draw)
        at = need_board
        for i in hidden:
            seats[i] = deck[at:at + hole_size]
            at += hole_size
        full_board = board + deck[:need_board]
        _add_shares(shares, [evaluate(seats[i], full_board) for i in range(len(seats))])
    shares = [s / float(samples) for s in shares]
    return shares, {"method": "sampled", "combinations": combos, "samples": samples}


def _add_shares(shares, scores):
    """Add this run-out's split into the running totals: the best score or scores take it equally."""
    best = max(scores)
    winners = 0
    for s in scores:
        if s == best:
            winners += 1
    slice_ = 1.0 / winners
    for i, s in enumerate(scores):
        if s == best:
            shares[i] += slice_


def _combinations(n, k):
    if k > n:
        return 0
    out = 1
    for i in range(k):
        out = out * (n - i) // (i + 1)
    return out


# --------------------------------------------------------------------------- self-test


def _selftest():
    """Checks whose expected values come from the rules and from published equities, not from this
    code's own output. A hand evaluator that agrees with itself still loses money."""
    checks = []
    h = lambda text: [Card.encodeCardList[c] for c in text.split()]

    def check(name, got, want):
        checks.append((name, got == want, got, want))

    def check_true(name, got):
        checks.append((name, bool(got), got, True))

    ladder = [
        ("Ah Kh Qh Jh Th", "straight flush"), ("9s 9h 9d 9c 2s", "quads"),
        ("8s 8h 8d Kc Kh", "full house"), ("Ah Th 8h 5h 2h", "flush"),
        ("9c 8d 7h 6s 5c", "straight"), ("7s 7h 7d Ac Kh", "trips"),
        ("Ts Th 4s 4h Ac", "two pair"), ("Qs Qh Td 9c 7h", "one pair"),
        ("Ah Th 8c 5s 2d", "high card")]
    for i, (cards, category) in enumerate(ladder):
        check("%s detected" % category, _category_of(score(h(cards))), category)
        if i:
            check_true("%s loses to %s" % (category, ladder[i - 1][1]),
                       score(h(cards)) < score(h(ladder[i - 1][0])))
    check("wheel is a straight", _category_of(score(h("5c 4d 3h 2s Ah"))), "straight")
    check("two wheels tie", score(h("5c 4d 3h 2s Ah")), score(h("5s 4c 3d 2h Ac")))
    check_true("six-high straight beats a wheel",
               score(h("6c 5d 4h 3s 2h")) > score(h("Ah 2c 3d 4h 5s")))
    check_true("a low straight flush beats top quads",
               score(h("6s 5s 4s 3s 2s")) > score(h("Ah Ad As Ac Ks")))
    check_true("kicker decides one pair",
               score(h("Ks Kh Ad Qc 9h")) > score(h("Kd Kc Jd Tc 8h")))
    check_true("two pair compares the second pair",
               score(h("As Ah Kd Kh Jc")) > score(h("Qs Qh Ad Ac Kh")))
    board = h("Ah Kh Qh Jh Th")
    check("both hands play the board", best_holdem(h("2c 3d"), board),
          best_holdem(h("4c 5d"), board))
    check_true("a hole card can beat the board",
               best_holdem(h("Ac Ad"), h("Kh Qh Jh Th 9c 3s 4d")) >
               best_holdem(h("Kc Kd"), h("Kh Qh Jh Th 9c 3s 4d")))
    ob = h("Ah Kh Qh Jh Th 2c 3d")
    check_true("omaha may not play five board cards",
               best_omaha(h("Ac Ad"), ob) < best_holdem(h("Ac Ad"), ob))
    check("omaha royal needs two hearts in hand",
          _category_of(best_omaha(h("Ah Kh"), h("Qh Jh Th 9c 8d 7s 6c"))), "straight flush")
    # Equity. The cases with a certain answer -- a dealt river, a board both players play -- are
    # checked exactly; the preflop ones against published figures from PokerStove/Equilab, with a
    # tolerance, because 1.7M run-outs is over the exact limit and is sampled instead.
    shares, meta = equity([h("Ah As"), h("Kh Kd")], h("Qc Jd 7h 6s 2d"), "holdem")
    check("a dealt river is exact", meta["method"], "exact")
    check("AA beats KK with no cards to come", shares, [1.0, 0.0])
    shares, _ = equity([h("2c 3d"), h("4h Ad")], h("9c 8d 7h 6s 5c"), "holdem")
    check("both players play the board straight", shares, [0.5, 0.5])
    shares, _ = equity([h("Ah 5h"), h("Kh Qh")], h("3h 4h 9d Jc 2h"), "holdem")
    check_true("the wheel straight flush beats a king-high flush", shares[0] == 1.0)
    shares, meta = equity([h("Ah As"), h("Kh Kd")], h("Th 9h 2c"), "holdem")
    check("a flop all-in is enumerated", meta["method"], "exact")
    check_true("AA is ahead on a two-flush flop", shares[0] > 0.6)
    check_true("the shares add up", abs(sum(shares) - 1.0) < 1e-9)
    shares, meta = equity([h("Ah As"), h("Kh Kd")], [], "holdem", samples=20000)
    check("a preflop all-in is sampled", meta["method"], "sampled")
    check_true("AA vs KK is about 82 %", abs(shares[0] - 0.8195) < 0.01)
    shares, meta = equity([h("Ah As"), []], [], "holdem", samples=20000)
    check_true("AA vs a random hand is about 85 %", abs(shares[0] - 0.852) < 0.01)
    shares, _ = equity([h("Ah Kh"), h("Qh Qd")], [], "holdem", samples=20000)
    check_true("AKs is a small dog to QQ", shares[0] < 0.5)
    om, ometa = equity([h("Ah As Ad Ac"), h("Kh Kd Kc 2h")], h("Qh Qd 3c 7s 9d"), "omahahi")
    check("omaha equity is exact", ometa["method"], "exact")
    check_true("omaha aces full beats kings full", om[0] == 1.0)
    print("HandEval self-test")
    bad = 0
    for name, ok, got, want in checks:
        if not ok:
            bad += 1
            print("  FAIL %-42s got %r want %r" % (name, got, want))
    print("%d checks, %d failures" % (len(checks), bad))
    return bad


def _category_of(value):
    return _CATEGORY_NAMES[value >> 20]


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8", errors="replace")
    started = time.time()
    bad = _selftest()
    print("%.1fs" % (time.time() - started))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
