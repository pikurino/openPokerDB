#!/usr/bin/env python
# -*- coding: utf-8 -*-

#Copyright 2026 openPokerDB contributors
#This program is free software: you can redistribute it and/or modify
#it under the terms of the GNU Affero General Public License as published by
#the Free Software Foundation, version 3 of the License.
#
#This program is distributed in the hope that it is useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#GNU General Public License for more details.
#
#You should have received a copy of the GNU Affero General Public License
#along with this program. If not, see <http://www.gnu.org/licenses/>.
#In the "official" distribution you can find the license in agpl-3.0.txt.

"""GGPoker tournament summary files -- the only place the real-money side of a tournament exists.

GG writes one small file per tournament you played, and it is the *only* source for buy-in, fee,
bounty, entries, prize pool and your finishing rank: the hand histories contain none of that (they
carry chip amounts only). Verified on 10 real summaries -- all 10 share this exact shape, 8 lines,
one tournament per file. The copy below is the committed synthetic fixture
regression-test-files/summaries/GGPoker/NLHE-USD-MTT-900000011.txt: real tournament numbers, names,
aliases, clocks and finish data are never written into this repository.

    Tournament #900000011, Synthetic Classic $1, Hold'em No Limit
    Buy-in: $0.92+$0.08
    3120 Players
    Total Prize Pool: $2,760.6
    Tournament started 2026/10/04 13:30:00
    1402th : Hero, $0
    You finished the tournament in 1402th place.
    You received a total of $0.

Two things are worth stating because they are easy to get wrong:

* the start date uses a SOLIDUS, not a dash. It renders as a dash in some consoles, which is how
  this parser was nearly written to fail on 10/10 files;
* the buy-in has 1, 2 or 3 '+'-separated parts. Freerolls are a bare '$0'. The two-part case is
  base+fee. The three-part case is base+fee+bounty, which is measurable rather than assumed: every
  two-part buy-in in the corpus pays an 8% fee ($0.92/$0.08, $1.84/$0.16) and the middle part of
  each three-part buy-in is the same 8% ($1.30/$0.20/$1.00 on a $2.50 'Bounty Hunters'), leaving
  the last part as the bounty.

The summary lists only you -- other players' ranks and winnings are not in it, so a summary
updates one TourneysPlayers row, not the field.
"""

import L10n
_ = L10n.get_translation()

import datetime
import re

from decimal_wrapper import Decimal

from Exceptions import FpdbParseError
from TourneySummary import TourneySummary
from HandHistoryConverter import log


class GGPokerSummary(TourneySummary):

    limits = {'No Limit': 'nl', 'Pot Limit': 'pl'}
    games = {"Hold'em": ('hold', 'holdem'),
             'Omaha':    ('hold', 'omahahi')}

    # NAME is greedy up to the LAST comma on the line: tournament names contain commas
    # ('Daily $100,000 #ThanksGG Flipout').
    re_SummaryInfo = re.compile(
        r"^Tournament \#(?P<TOURNO>\d+), (?P<NAME>.+), (?P<GAME>Hold'em|Omaha) (?P<LIMIT>No Limit|Pot Limit)$",
        re.MULTILINE)

    re_Entries = re.compile(r"^(?P<ENTRIES>\d+)\s+Players$", re.MULTILINE)

    re_PrizePool = re.compile(r"^Total\s+Prize\s+Pool:\s+\$(?P<PRIZEPOOL>\d[\d.,]*)$", re.MULTILINE)

    re_Start = re.compile(r"^Tournament\s+started\s+(?P<DATETIME>\d{4}/\d{2}/\d{2}\s\d{2}:\d{2}:\d{2})\s*$",
                          re.MULTILINE)

    re_Result = re.compile(r"""^(?P<RANK>\d+)(?:st|nd|rd|th)\s*:\s*(?P<PNAME>[^,]+?),\s*
          \$(?P<WINNINGS>\d[\d.,]*)\s*$""", re.MULTILINE | re.VERBOSE)

    @staticmethod
    def getSplitRe(self, head):
        # Zero-width so the tournament number survives the split; each chunk starts at a header.
        return re.compile(r"(?=^Tournament \#\d+, )", re.MULTILINE)

    @staticmethod
    def cents(text):
        return int(100 * Decimal(text.replace(",", "").replace("$", "")))

    def parseSummary(self):
        m = self.re_SummaryInfo.search(self.summaryText)
        if m is None:
            log.error(_("GGPokerSummary.parseSummary: no tournament header in: '%s'")
                      % self.summaryText[:200])
            raise FpdbParseError
        self.tourNo = int(m.group('TOURNO'))
        self.tourneyName = m.group('NAME')
        self.gametype['category'] = self.games[m.group('GAME')][1]
        self.gametype['limitType'] = self.limits[m.group('LIMIT')]

        parts = re.findall(r"\$\d[\d.,]*", self._buy_in_line())
        if not parts:
            log.error(_("GGPokerSummary.parseSummary: no 'Buy-in:' line for tournament #%s")
                      % self.tourNo)
            raise FpdbParseError
        # 1 part = freeroll, 2 = base+fee, 3 = base+fee+bounty (see module docstring).
        self.buyin = self.cents(parts[0])
        self.fee = self.cents(parts[1]) if len(parts) > 1 else 0
        if len(parts) == 3:
            self.isKO = True
            self.koBounty = self.cents(parts[2])
        elif len(parts) > 3:
            log.error(_("GGPokerSummary.parseSummary: unexpected buy-in '%s'") % parts)
            raise FpdbParseError
        if self.buyin == 0 and self.fee == 0:
            self.buyinCurrency = "FREE"
        else:
            self.buyinCurrency = "USD"
        self.currency = "USD"

        m = self.re_Entries.search(self.summaryText)
        if m is None:
            log.error(_("GGPokerSummary.parseSummary: no '<n> Players' line for tournament #%s")
                      % self.tourNo)
            raise FpdbParseError
        self.entries = int(m.group('ENTRIES'))

        m = self.re_PrizePool.search(self.summaryText)
        if m is None:
            log.error(_("GGPokerSummary.parseSummary: no prize pool line for tournament #%s")
                      % self.tourNo)
            raise FpdbParseError
        self.prizepool = self.cents(m.group('PRIZEPOOL'))

        m = self.re_Start.search(self.summaryText)
        if m is None:
            log.error(_("GGPokerSummary.parseSummary: no start time for tournament #%s")
                      % self.tourNo)
            raise FpdbParseError
        self.startTime = datetime.datetime.strptime(m.group('DATETIME'), "%Y/%m/%d %H:%M:%S")

        m = self.re_Result.search(self.summaryText)
        if m is None:
            log.error(_("GGPokerSummary.parseSummary: no result line for tournament #%s")
                      % self.tourNo)
            raise FpdbParseError
        name = m.group('PNAME').strip()
        rank = int(m.group('RANK'))
        winnings = self.cents(m.group('WINNINGS'))
        self.hero = name
        ko_count = 0  # GG summaries do not report knockouts won.
        self.addPlayer(rank, name, winnings, self.currency, 0, 0, ko_count)

    def _buy_in_line(self):
        for line in self.summaryText.splitlines():
            if line.startswith("Buy-in:"):
                return line
        return ""
