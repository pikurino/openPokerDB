#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""Verify GGPokerSummary against the format recorded in docs/gg-hand-history-format.md section 12.

Parse-level only: it never opens a database, so it cannot touch an imported corpus. Two tiers --

  * regression-test-files/summaries/GGPoker/: three fixtures in the measured shape with invented
    numbers, asserting the exact fields the parser is supposed to land on;
  * the real corpus (default C:/Users/jarug/Documents/Projects/Poker/history/mtts/summaries,
    skipped with a note if absent): every file must parse, and each parsed figure must equal the
    amount printed in the text, re-derived here independently of the parser's own regexes.

    python TestGgSummary.py [summaries_dir] [--verbose]
"""
import sys
import os
import re
import glob
import datetime
import optparse
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import GGPokerSummary
import IdentifySite

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "regression-test-files", "summaries", "GGPoker")
DEFAULT_CORPUS = r"C:\Users\jarug\Documents\Projects\Poker\history\mtts\summaries"

failures = []
checks = 0


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append("%s: got %r, want %r" % (label, got, want))
    elif verbose:
        print("ok   %s = %r" % (label, got))


def parse(text):
    """Build a GGPokerSummary without running TourneySummary.__init__ (that opens a database)."""
    o = object.__new__(GGPokerSummary.GGPokerSummary)
    o.summaryText = text
    o.players, o.ranks, o.winnings, o.winningsCurrency = [], {}, {}, {}
    o.rebuyCounts, o.addOnCounts, o.koCounts = {}, {}, {}
    for name in ("hero", "startTime", "endTime", "tourNo", "currency", "buyinCurrency",
                 "tourneyName"):
        setattr(o, name, None)
    for name in ("buyin", "fee", "entries", "prizepool", "koBounty", "maxseats"):
        setattr(o, name, 0)
    o.isKO = o.isRebuy = o.isAddOn = o.isSng = False
    o.gametype = {'category': None, 'limitType': None, 'mix': 'none'}
    o.addPlayer = GGPokerSummary.TourneySummary.addPlayer.__get__(o)
    o.parseSummary()
    return o


def test_fixtures():
    # Fixture values are invented; the shape is the measured one.
    expected = {
        "NLHE-USD-MTT-900000011.txt": dict(
            tourNo=900000011, tourneyName="Synthetic Classic $1",
            category="holdem", limitType="nl", buyin=92, fee=8, isKO=False, koBounty=0,
            entries=3120, prizepool=276060, buyinCurrency="USD", currency="USD",
            startTime=datetime.datetime(2026, 10, 4, 13, 30, 0), rank=1402, winnings=0),
        "NLHE-USD-KO-900000012.txt": dict(
            tourNo=900000012, tourneyName="Synthetic Bounty Series $2.50",
            category="holdem", limitType="nl", buyin=130, fee=20, isKO=True, koBounty=100,
            entries=5243, prizepool=681590, buyinCurrency="USD", currency="USD",
            startTime=datetime.datetime(2026, 10, 4, 21, 5, 0), rank=2050, winnings=500),
        # A name containing a comma: the header regex must keep the whole thing.
        "NLHE-FREE-MTT-900000013.txt": dict(
            tourNo=900000013, tourneyName="Synthetic $50,000 Mega Flipout",
            category="holdem", limitType="nl", buyin=0, fee=0, isKO=False, koBounty=0,
            entries=9999, prizepool=5000000, buyinCurrency="FREE", currency="USD",
            startTime=datetime.datetime(2026, 10, 5, 4, 20, 0), rank=6100, winnings=0),
    }
    for fname, want in sorted(expected.items()):
        path = os.path.join(FIXTURES, fname)
        if not os.path.exists(path):
            failures.append("missing fixture %s" % path)
            continue
        o = parse(open(path, encoding="utf-8").read())
        tag = fname
        check(tag + " tourNo", o.tourNo, want["tourNo"])
        check(tag + " name", o.tourneyName, want["tourneyName"])
        check(tag + " category", o.gametype["category"], want["category"])
        check(tag + " limitType", o.gametype["limitType"], want["limitType"])
        check(tag + " buyin cents", o.buyin, want["buyin"])
        check(tag + " fee cents", o.fee, want["fee"])
        check(tag + " isKO", o.isKO, want["isKO"])
        check(tag + " koBounty cents", o.koBounty, want["koBounty"])
        check(tag + " entries", o.entries, want["entries"])
        check(tag + " prizepool cents", o.prizepool, want["prizepool"])
        check(tag + " buyinCurrency", o.buyinCurrency, want["buyinCurrency"])
        check(tag + " currency", o.currency, want["currency"])
        check(tag + " startTime", o.startTime, want["startTime"])
        check(tag + " players", o.players, ["Hero"])
        check(tag + " rank", o.ranks.get("Hero"), want["rank"])
        check(tag + " winnings cents", o.winnings.get("Hero"), want["winnings"])

    # One summary file per tournament, but the importer splits on the header anyway.
    order = sorted(expected)
    joined = "".join(open(os.path.join(FIXTURES, f), encoding="utf-8").read() for f in order)
    chunks = [c for c in re.split(GGPokerSummary.GGPokerSummary.getSplitRe(None, joined[:1000]),
                                  joined) if c.strip()]
    check("split finds 3 summaries in one concatenated file", len(chunks), 3)
    for i, chunk in enumerate(chunks):
        check("chunk %d keeps its tournament number" % i, chunk.startswith("Tournament #"), True)
        check("chunk %d parses" % i, parse(chunk).tourNo, expected[order[i]]["tourNo"])


def test_identification():
    patterns = IdentifySite.IdentifySite.getSiteRegex(None)
    fixture = open(os.path.join(FIXTURES, "NLHE-USD-MTT-900000011.txt"), encoding="utf-8").read()
    check("summary is identified", bool(patterns["GGPokerSummary"].search(fixture)), True)
    cash = ("Poker Hand #RC900000001: Rush And Cash ($0.01/$0.02) - 2026/10/04 12:00:00\n"
            "Table '1' 6-max Seat #3 is the button\n")
    check("a cash hand is not claimed as a summary",
          bool(patterns["GGPokerSummary"].search(cash)), False)
    check("a summary is not claimed as a hand history",
          bool(patterns["GGPoker"].search(fixture)), False)


def test_corpus(corpus):
    files = sorted(glob.glob(os.path.join(corpus, "*.txt")))
    if not files:
        print("SKIP real corpus: no files under %s" % corpus)
        return
    cents = lambda s: int(100 * Decimal(s.replace(",", "").replace("$", "")))
    for path in files:
        text = open(path, encoding="utf-8").read()
        name = os.path.basename(path)[:30]
        o = parse(text)
        # Re-derive each figure straight from the text, independently of the parser.
        check(name + " tourNo", o.tourNo,
              int(re.search(r"^Tournament \#(\d+),", text, re.MULTILINE).group(1)))
        check(name + " buy-in parts", o.buyin + o.fee,
              sum(cents(p) for p in re.findall(r"\$[\d.,]+",
                                               re.search(r"^Buy-in: (.*)$", text,
                                                         re.MULTILINE).group(1))[:2]))
        check(name + " entries", o.entries,
              int(re.search(r"^(\d+) Players$", text, re.MULTILINE).group(1)))
        check(name + " prizepool", o.prizepool,
              cents(re.search(r"Total Prize Pool: (\$[\d.,]+)", text).group(1)))
        check(name + " rank", o.ranks["Hero"],
              int(re.search(r"^(\d+)\w* : Hero", text, re.MULTILINE).group(1)))
        check(name + " winnings", o.winnings["Hero"],
              cents(re.search(r"Hero, (\$[\d.,]+)", text).group(1)))
        # The header clock is a solidus; a dash here means the format changed under us.
        sep = re.search(r"Tournament started \d{4}(.)\d{2}(.)\d{2}", text)
        check(name + " start date separators are 0x2f",
              [ord(sep.group(1)), ord(sep.group(2))], [0x2f, 0x2f])
    print("real corpus: %d files checked" % len(files))


def main():
    global verbose
    parser = optparse.OptionParser(usage="usage: %prog [summaries_dir] [options]")
    parser.add_option("-v", "--verbose", action="store_true", default=False)
    opts, args = parser.parse_args()
    verbose = opts.verbose
    corpus = args[0] if args else DEFAULT_CORPUS

    test_fixtures()
    test_identification()
    test_corpus(corpus)

    print("%d checks, %d failures" % (checks, len(failures)))
    for f in failures:
        print("FAIL %s" % f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
