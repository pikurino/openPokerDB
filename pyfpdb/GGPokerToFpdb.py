#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published by
#    the Free Software Foundation; either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program. If not, see <http://www.gnu.org/licenses/>.
########################################################################

"""GGPoker 'Rush & Cash' cash-game hand histories.

Every pattern below encodes a shape measured on the corpus described in
docs/gg-hand-history-format.md (the counts quoted in the comments come from that document). Where
the document lists a case the corpus does not contain -- tournaments, antes, bring-ins, other
currencies, 'Mucked', 'Side pot', heads-up Rush & Cash -- this parser raises FpdbParseError instead
of guessing, so a format change fails loudly at import time rather than silently producing bad
statistics.
"""

import L10n
_ = L10n.get_translation()

import re
import datetime
import logging
from HandHistoryConverter import *
from decimal_wrapper import Decimal

log = logging.getLogger("parser")


class GGPoker(HandHistoryConverter):

    sitename = "GGPoker"
    filetype = "text"
    codepage = "utf8"          # doc §1: UTF-8, \n line endings, no BOM
    siteId = 21                # must match the Sites row added by Database.fillDefaultData

    substitutions = {
        # Doc §7.2: aliases are 6-8 chars of [0-9a-f] and 29 of them are all digits, so no player
        # pattern may require a letter. 'Hero' is the local player (doc §1).
        'PLYR': r'(?P<PNAME>\S+)',
        # Doc §3: money is always '$' followed by 0-2 decimals, never thousands-separated. Another
        # currency makes these patterns fail rather than mis-parse (doc §9).
        'CUR': r"\$",
        'AMT': r"[\d.]+",
    }

    limits = {'No Limit': 'nl', 'Pot Limit': 'pl'}
    games = {"Hold'em": ('hold', 'holdem'),      # doc §3: 167 Hold'em + 178 Omaha hands
             'Omaha': ('hold', 'omahahi')}

    # Doc §8.2: the three EV Cashout verbs, named for what they mean so the Hands.comment stays
    # readable. A verb outside this map raises instead of silently changing shape.
    cashoutNotes = {'Chooses to EV Cashout': 'choseEvCashout',
                    'Pays Cashout Risk': 'paidCashoutRisk',
                    'Receives Cashout': 'receivedCashout'}

    # Doc §1/§2: a file is a flat concatenation of hand blocks separated by blank lines. 345 hands
    # in 10 files give exactly 335 separators, so no hand contains an internal blank line.
    re_SplitHands = re.compile(r'(?:\s?\n){2,}')

    # Doc §3 line 1. The date separator is a SOLIDUS, verified byte-wise (0x2f): editors and
    # consoles render this header with hyphens, which is why a '\d{4}-\d{2}-\d{2}' pattern matches
    # nothing in this corpus. Hand ids are 'RC' + digits; the digits alone are unique over 345/345
    # hands and Hands.siteHandNo is a BIGINT, so the prefix is stripped.
    re_GameInfo = re.compile(r"""
          ^Poker\ Hand\ \#RC(?P<HID>\d+):\s+
          (?P<GAME>Hold'em|Omaha)\s(?P<LIMIT>No\ Limit|Pot\ Limit)\s
          \((?P<CURRENCY>\$)(?P<SB>[\d.]+)/\$(?P<BB>[\d.]+)\)\s-\s
          (?P<DATETIME>\d{4}/\d{2}/\d{2}\s\d{2}:\d{2}:\d{2})$
        """, re.MULTILINE | re.VERBOSE)

    # Doc §3 line 2 / §7.1: 'RushAndCash<id>' is unique per hand (345 ids over 345 hands) so it is
    # not a table identity -- see readHandInfo. Requiring the prefix keeps the GG products this
    # corpus does not cover (regular cash tables, tournaments) out instead of mis-importing them.
    re_TableInfo = re.compile(r"""
          ^Table\ '(?P<TABLE>RushAndCash(?P<TABLEID>\d+))'\ (?P<MAX>\d+)-max
          \sSeat\ \#(?P<BUTTON>\d+)\ is\ the\ button$
        """, re.MULTILINE | re.VERBOSE)

    # Doc §3 line 3: 6 seats per hand (1725 + 345 lines), always '($<stack> in chips)'.
    re_PlayerInfo = re.compile(r"""
          ^Seat\ (?P<SEAT>\d+):\ (?P<PNAME>\S+)\ \(\$(?P<CASH>[\d.]+)\ in\ chips\)$
        """, re.MULTILINE | re.VERBOSE)

    # Doc §3 lines 5/6, §4: exactly one SB and one BB line per hand, both before
    # *** HOLE CARDS ***. UNSEEN: antes, bring-ins, straddles (doc §9).
    re_PostSB = re.compile(r"^%(PLYR)s: posts small blind %(CUR)s(?P<SB>[\d.]+)$"
                           % substitutions, re.MULTILINE)
    re_PostBB = re.compile(r"^%(PLYR)s: posts big blind %(CUR)s(?P<BB>[\d.]+)$"
                           % substitutions, re.MULTILINE)

    # Doc §3 line 4: promotional money that is in the pot but contributed by no player (4 hands).
    # Note the spaces around ':' and the trailing space.
    re_CashDrop = re.compile(r"^Cash Drop to Pot : total \$(?P<AMT>[\d.]+)[ ]*$", re.MULTILINE)

    # Doc §6: 2070 'Dealt to' lines, cards on exactly 345 of them, and every one of those is Hero.
    # The cardless lines carry a trailing space.
    re_DealtTo = re.compile(r"^Dealt to (?P<PNAME>\S+)(?: \[(?P<CARDS>[^\]]+)\])?[ \t]*$",
                            re.MULTILINE)

    # Street sections. Doc §2/§8.3: in the two run-it-twice hands the markers gain a 'FIRST '
    # prefix. Run 1 is where all the betting happens, so its streets keep fpdb's unsuffixed names
    # and the SECOND markers are deliberately not streets here -- readRunItTwice stores that board.
    re_Sections = re.compile(r"""
          \*\*\*\ HOLE\ CARDS\ \*\*\*(?P<PREFLOP>.*?)(?=\n\*\*\*|\Z)
          (?:\n\*\*\*\ (?:FIRST\ )?FLOP\ \*\*\*(?P<FLOP>.*?)(?=\n\*\*\*|\Z))?
          (?:\n\*\*\*\ (?:FIRST\ )?TURN\ \*\*\*(?P<TURN>.*?)(?=\n\*\*\*|\Z))?
          (?:\n\*\*\*\ (?:FIRST\ )?RIVER\ \*\*\*(?P<RIVER>.*?)(?=\n\*\*\*|\Z))?
        """, re.DOTALL | re.VERBOSE)

    # Doc §3 lines 9/10 and DESIGN.md §3.6: FLOP carries one bracket, TURN and RIVER always carry
    # two -- '[board so far] [new card]' (208/208 lines). fpdb stores only the new cards per street
    # (that is what Hand.py's own re-serialisation does).
    re_StreetBoard = re.compile(r"^ \[(?P<SHARED>[^\]]*)\](?: \[(?P<NEW>[^\]]+)\])?")

    # Doc §8.3: the summary names both boards. 'FIRST Board' is the complete 5-card run-1 board;
    # 'SECOND Board' carries only the cards dealt after the shared prefix.
    re_RunItTwice = re.compile(r"^Hand was run two times$", re.MULTILINE)
    re_RunBoard = re.compile(r"^(?P<RUN>FIRST|SECOND) Board \[(?P<CARDS>[^\]]+)\]$", re.MULTILINE)
    re_Run2Marker = re.compile(r"^\*\*\* SECOND (?:FLOP|TURN|RIVER) \*\*\* \[([^\]]*)\]"
                               r"(?: \[([^\]]+)\])?", re.MULTILINE)

    # Doc §4: after '<player>: ' exactly these verbs occur and nothing else. A '<token>: ' line with
    # any other verb is a format change and raises.
    re_ActionLine = re.compile(r"^%(PLYR)s: (?P<VERB>folds|checks|calls|bets|raises|shows|"
                               r"Chooses to EV Cashout|Pays Cashout Risk|Receives Cashout)"
                               r"(?P<ARGS>.*)$" % substitutions, re.MULTILINE)
    re_Amount = re.compile(r"^ \$(?P<AMT>[\d.]+)(?: and is all-in)?$")
    re_RaiseAmounts = re.compile(r"^ \$(?P<BY>[\d.]+) to \$(?P<TO>[\d.]+)(?: and is all-in)?$")
    re_CashoutAmount = re.compile(r"^ \(\$(?P<AMT>[\d.]+)\)$")

    # Doc §4/§6: 'shows' is not an action and is not confined to the showdown section; the hand-rank
    # parenthetical is optional (115 with, 20 without).
    re_ShownCards = re.compile(r"^%(PLYR)s: shows \[(?P<CARDS>[^\]]+)\](?: \((?P<STRING>.*)\))?$"
                               % substitutions, re.MULTILINE)

    # Doc §3 line 15 / §8.1: '<player> collected $<amt> from pot' is the only authoritative per-
    # player win amount. 8 hands have more than one line (side pots without markup), main pot first.
    re_CollectPot = re.compile(r"^%(PLYR)s collected %(CUR)s(?P<POT>[\d.]+) from pot$"
                               % substitutions, re.MULTILINE)

    # Doc §5: the summary money line, fixed field order. The 'Uncalled bet ($N) returned to X' line
    # is not parsed at all: Pot.end() works the return out from the committed amounts itself.
    re_RakeSplit = re.compile(r"""
          ^Total\ pot\ \$(?P<POT>[\d.]+)\ \|\ Rake\ \$(?P<RAKE>[\d.]+)
          \ \|\ Jackpot\ \$(?P<JACKPOT>[\d.]+)
          \ \|\ Bingo\ \$(?P<BINGO>[\d.]+)
          \ \|\ Fortune\ \$(?P<FORTUNE>[\d.]+)
          \ \|\ Tax\ \$(?P<TAX>[\d.]+)$
        """, re.MULTILINE | re.VERBOSE)

    # Doc §3 lines 17/19 / §4: per-seat summary clauses. Only the 'showed' ones carry cards and a
    # hand rank; the second outcome clause of a run-it-twice hand and the cashout suffixes are cut
    # off the rank string (doc §8.2/§8.3).
    re_SummaryShown = re.compile(r"""
          ^Seat\ (?P<SEAT>\d+):\ (?P<PNAME>\S+)\ (?:\((?:button|small\ blind|big\ blind)\)\ )?
          showed\ \[(?P<CARDS>[^\]]+)\]\ and\ (?:won|lost)(?:\ \(\$(?P<POT>[\d.]+)\))?
          \ with\ (?P<STRING>.+)$
        """, re.MULTILINE | re.VERBOSE)
    re_SummaryTail = re.compile(r", (?:and (?:won|lost)|Cashout Risk|EV Cashout)")

    # Doc §3 line 18: 'Board [...] ' restates the run-1 board in the 182 single-board hands.
    re_SummaryBoard = re.compile(r"^Board \[(?P<CARDS>[^\]]+)\]$", re.MULTILINE)

    # Doc §6: rank is 2-9/T/J/Q/K/A (always 'T', never '10') and the suit is lowercase.
    CARD_RE = re.compile(r"^[2-9TJQKA][shdc]$")

    def readSupportedGames(self):
        # doc §3: Hold'em No Limit and Omaha Pot Limit only.
        return [["ring", "hold", "nl"],
                ["ring", "hold", "pl"]]

    def determineGameType(self, handText):
        m = self.re_GameInfo.search(handText)
        if not m:
            log.error(_("GGPokerToFpdb.determineGameType: '%s'") % handText[0:200])
            raise FpdbParseError

        info = {}
        info['type'] = 'ring'
        info['base'], info['category'] = self.games[m.group('GAME')]
        info['limitType'] = self.limits[m.group('LIMIT')]
        info['sb'] = m.group('SB')
        info['bb'] = m.group('BB')
        info['currency'] = 'USD'              # the only currency in the corpus (doc §3)
        return info

    def compilePlayerRegexs(self, hand):
        pass

    def readHandInfo(self, hand):
        m = self.re_GameInfo.search(hand.handText)
        if not m:
            log.error(_("GGPokerToFpdb.readHandInfo: '%s': no hand header") % hand.handText[0:200])
            raise FpdbParseError
        # DESIGN.md §3.5: the header clock is the authoritative one. Files are newest-first and the
        # filename clock is 6 h ahead of it, so import the header time and treat the offset as data.
        hand.startTime = datetime.datetime.strptime(m.group('DATETIME'), "%Y/%m/%d %H:%M:%S")
        hand.handid = m.group('HID')

        t = self.re_TableInfo.search(hand.handText)
        if not t:
            log.error(_("GGPokerToFpdb.readHandInfo: '%s': no table line") % hand.handText[0:200])
            raise FpdbParseError
        hand.maxseats = int(t.group('MAX'))
        # fpdb prints 'Seat #%s is the button' straight out of buttonpos, so it stays 1-based, as in
        # the PokerStars converter.
        hand.buttonpos = int(t.group('BUTTON'))

        # DESIGN.md §3.1: fast fold means the site's table id changes on every hand, so keying
        # Hands.tableName on it would give 345 one-hand tables. Use a stable logical name per game
        # and stakes, built from fpdb's own gametype words, and keep the real id in the comment.
        category = self.games[m.group('GAME')][1]
        limit = self.limits[m.group('LIMIT')]
        hand.tablename = "RushAndCash-%s-%s-%s/%s" % (category, limit,
                                                      m.group('SB'), m.group('BB'))
        hand.rush = True
        self.note(hand, "rushTable=%s" % t.group('TABLE'))

    def note(self, hand, text):
        """Accumulate the GG-specific extras that Hands.comment exists to carry."""
        existing = getattr(hand, 'comment', None)
        hand.comment = text if not existing else "%s; %s" % (existing, text)

    def readPlayerStacks(self, hand):
        for a in self.re_PlayerInfo.finditer(hand.handText):
            hand.addPlayer(int(a.group('SEAT')), a.group('PNAME'), a.group('CASH'))

    def markStreets(self, hand):
        m = self.re_Sections.search(hand.handText)
        hand.addStreets(m)

    def readCommunityCards(self, hand, street):
        # Hand.py:1026 calls this for every street whose section is non-empty, in street order.
        m = self.re_StreetBoard.match(hand.streets[street])
        if not m:
            log.error(_("GGPokerToFpdb.readCommunityCards: '%s': unexpected board on %s: %r")
                      % (hand.handid, street, hand.streets[street][0:60]))
            raise FpdbParseError
        if street == 'FLOP':
            if m.group('NEW') is not None:
                # Doc §9: a second bracket on the flop line is not a shape this corpus has.
                log.error(_("GGPokerToFpdb.readCommunityCards: '%s': two brackets on the flop")
                          % hand.handid)
                raise FpdbParseError
            cards = m.group('SHARED')
        else:
            if m.group('NEW') is None:
                log.error(_("GGPokerToFpdb.readCommunityCards: '%s': %s has no new card")
                          % (hand.handid, street))
                raise FpdbParseError
            cards = m.group('NEW')
        hand.setCommunityCards(street, self.checkCards(hand, cards.split(' '), street))

        if street == 'FLOP':
            # Run it twice always has a flop, and both boards are needed before DerivedStats runs.
            self.readRunItTwice(hand)

    def checkCards(self, hand, cards, where):
        for c in cards:
            if not self.CARD_RE.match(c):
                log.error(_("GGPokerToFpdb: '%s': bad card '%s' in %s") % (hand.handid, c, where))
                raise FpdbParseError
        return cards

    def readRunItTwice(self, hand):
        """Store both boards under the keys DerivedStats looks for.

        DerivedStats.py:177-189 builds hands['boards'] from hand.board[street + str(i + 1)] for
        i in range(hand.runItTimes), so run 1 needs FLOP1/TURN1/RIVER1 in addition to the
        unsuffixed keys it uses for boardcard1..5. No other parser sets runItTimes; GG is the first.
        """
        if not self.re_RunItTwice.search(hand.handText):
            return
        boards = dict((m.group('RUN'), m.group('CARDS').split(' '))
                      for m in self.re_RunBoard.finditer(hand.handText))
        if 'FIRST' not in boards or 'SECOND' not in boards:
            log.error(_("GGPokerToFpdb.readRunItTwice: '%s': no FIRST/SECOND Board line")
                      % hand.handid)
            raise FpdbParseError

        # Doc §8.3: the two runs are not symmetric (measured: one hand has a 5-card FIRST board and
        # a 1-card SECOND board). GGPoker reports only the second run's new cards, so run 2's full
        # board is the shared prefix -- the first bracket of the first SECOND street marker -- plus
        # those cards.
        markers = self.re_Run2Marker.findall(hand.handText)
        prefix = markers[0][0].split(' ') if markers and markers[0][0] else []
        run2 = prefix + boards['SECOND']
        if len(run2) > 5:
            log.error(_("GGPokerToFpdb.readRunItTwice: '%s': second board has %d cards")
                      % (hand.handid, len(run2)))
            raise FpdbParseError

        self.setRunBoard(hand, 1, boards['FIRST'])
        self.setRunBoard(hand, 2, run2)
        hand.runItTimes = 2

    def setRunBoard(self, hand, run, cards):
        cards = self.checkCards(hand, cards, "run %d board" % run)
        hand.board['FLOP%d' % run] = cards[0:3]
        hand.board['TURN%d' % run] = cards[3:4]
        hand.board['RIVER%d' % run] = cards[4:5]

    def readButton(self, hand):
        pass            # readHandInfo takes Seat #n from the table line

    def readAntes(self, hand):
        pass            # UNSEEN: this format has no antes (doc §9)

    def readBringIn(self, hand):
        pass            # UNSEEN: no All-In or Fold bring-ins in this corpus (doc §9)

    def readBlinds(self, hand):
        m = self.re_PostSB.search(hand.handText)
        if not m:
            log.error(_("GGPokerToFpdb.readBlinds: '%s': no small blind line") % hand.handid)
            raise FpdbParseError
        hand.addBlind(m.group('PNAME'), 'small blind', m.group('SB'))
        m = self.re_PostBB.search(hand.handText)
        if not m:
            log.error(_("GGPokerToFpdb.readBlinds: '%s': no big blind line") % hand.handid)
            raise FpdbParseError
        hand.addBlind(m.group('PNAME'), 'big blind', m.group('BB'))

        # Doc §3 line 4: money in the pot that no player contributed. Pot.markTotal() and end() sum
        # pot.common, so putting it there keeps fpdb's pot equal to the site's 'Total pot', which is
        # what makes the invariant in getRake provable. It is deliberately not attributed to a
        # player: DerivedStats.py:257 reads common per player to work out totalProfit.
        for drop in self.re_CashDrop.finditer(hand.handText):
            amount = Decimal(drop.group('AMT'))
            hand.pot.common['CashDrop'] = hand.pot.common.get('CashDrop', Decimal(0)) + amount
            self.note(hand, "cashDrop=$%s" % drop.group('AMT'))

    def readHeroCards(self, hand):
        for m in self.re_DealtTo.finditer(hand.streets['PREFLOP']):
            if m.group('CARDS') is None:
                continue            # the five cardless 'Dealt to' lines per hand (doc §6)
            if m.group('PNAME') != 'Hero':
                # Doc §9: cards on a non-Hero dealt-to line means the format changed.
                log.error(_("GGPokerToFpdb.readHeroCards: '%s': cards dealt to '%s'")
                          % (hand.handid, m.group('PNAME')))
                raise FpdbParseError
            hand.hero = m.group('PNAME')
            cards = self.checkCards(hand, m.group('CARDS').split(' '), 'hole cards')
            hand.addHoleCards('PREFLOP', hand.hero, closed=cards, shown=False, mucked=False,
                              dealt=True)
        if not hand.hero:
            log.error(_("GGPokerToFpdb.readHeroCards: '%s': no Hero cards") % hand.handid)
            raise FpdbParseError

    def readAction(self, hand, street):
        for m in self.re_ActionLine.finditer(hand.streets[street]):
            name = m.group('PNAME')
            verb = m.group('VERB')
            args = m.group('ARGS')
            if verb == 'folds':
                self.requireEmptyArgs(hand, street, verb, args)
                hand.addFold(street, name)
            elif verb == 'checks':
                self.requireEmptyArgs(hand, street, verb, args)
                hand.addCheck(street, name)
            elif verb in ('calls', 'bets'):
                a = self.re_Amount.match(args)
                if not a:
                    self.unsupported(hand, street, verb, args)
                if verb == 'calls':
                    hand.addCall(street, name, a.group('AMT'))
                else:
                    hand.addBet(street, name, a.group('AMT'))
            elif verb == 'raises':
                a = self.re_RaiseAmounts.match(args)
                if not a:
                    self.unsupported(hand, street, verb, args)
                hand.addRaiseBy(street, name, a.group('BY'))
                # Doc §4: in 'raises $A to $B', B - A is the amount being raised against, so
                # _addRaise's own arithmetic must land exactly on B. If it does not, the format --
                # or our reading of it -- has changed.
                if hand.lastBet[street] != Decimal(a.group('TO')):
                    log.error(_("GGPokerToFpdb.readAction: '%s': %s raises to %s computed %s")
                              % (hand.handid, street, a.group('TO'), hand.lastBet[street]))
                    raise FpdbParseError
            elif verb == 'shows':
                pass                # not an action; readShowdownActions handles it (doc §6)
            else:
                # Doc §8.2: the EV Cashout verbs settle a hand, they wager nothing, but the player
                # stops acting afterwards. Keep them out of HandsActions and into the comment.
                note = self.cashoutNotes.get(verb)
                if note is None:
                    self.unsupported(hand, street, verb, args)
                a = self.re_CashoutAmount.match(args)
                if a:
                    self.note(hand, "%s=$%s" % (note, a.group('AMT')))
                else:
                    self.requireEmptyArgs(hand, street, verb, args)
                    self.note(hand, note)

    def requireEmptyArgs(self, hand, street, verb, args):
        if args.strip():
            self.unsupported(hand, street, verb, args)

    def unsupported(self, hand, street, verb, args):
        log.error(_("GGPokerToFpdb.readAction: '%s': unrecognised action '%s' '%s' on %s")
                  % (hand.handid, verb, args, street))
        raise FpdbParseError

    def readShowdownActions(self, hand):
        # Doc §6/DESIGN.md §3.6: 'shows' is not confined to the showdown section -- in 8 hands every
        # one of them appears before *** FLOP *** (all-in preflop). Scan the whole hand.
        for m in self.re_ShownCards.finditer(hand.handText):
            cards = self.checkCards(hand, m.group('CARDS').split(' '), 'shown cards')
            hand.addShownCards(cards=cards, player=m.group('PNAME'), shown=True)

    def readCollectPot(self, hand):
        # Doc §8.1: side pots carry no markup, only several 'collected' lines, main pot first, which
        # is the order they appear in the text.
        for m in self.re_CollectPot.finditer(hand.handText):
            hand.addCollectPot(player=m.group('PNAME'), pot=m.group('POT'))

    def readShownCards(self, hand):
        # The summary restates the shown cards (doc §6) and adds the hand rank, which is the part
        # the body 'shows' lines do not always carry (16 + 4 lines have no rank).
        for m in self.re_SummaryShown.finditer(hand.handText):
            cards = self.checkCards(hand, m.group('CARDS').split(' '), 'summary shown cards')
            string = self.re_SummaryTail.split(m.group('STRING'))[0]
            hand.addShownCards(cards=cards, player=m.group('PNAME'), shown=True, string=string)

    def getRake(self, hand):
        """Rake is a five-way split and Jackpot is not part of Rake (DESIGN.md §3.3).

        Reading only the 'Rake' field understates the house's cut by $0.73 of $4.57 (16%) on the
        corpus. The base class derives rake from pot minus collected; GGPoker reports the
        components, so use them and assert the measured invariant instead.
        """
        m = self.re_RakeSplit.search(hand.handText)
        if not m:
            log.error(_("GGPokerToFpdb.getRake: '%s': no summary money line") % hand.handid)
            raise FpdbParseError

        rake = sum([Decimal(m.group(k)) for k in ('RAKE', 'JACKPOT', 'BINGO', 'FORTUNE', 'TAX')])
        reported = Decimal(m.group('POT'))

        # Invariant, measured on 345/345 hands with 0 violations:
        #   Total pot == sum(collected) + Rake + Jackpot + Bingo + Fortune + Tax
        collected = sum(hand.collectees.values())
        if reported != collected + rake:
            log.error(_("GGPokerToFpdb.getRake: '%s': pot %s != collected %s + deductions %s")
                      % (hand.handid, reported, collected, rake))
            raise FpdbParseError
        # fpdb builds its own pot from the betting; it has to equal the site's number, which is how
        # a missed forced bet or an unparsed money line shows up.
        if reported != hand.totalpot:
            log.error(_("GGPokerToFpdb.getRake: '%s': site pot %s != calculated pot %s")
                      % (hand.handid, reported, hand.totalpot))
            raise FpdbParseError

        board = self.re_SummaryBoard.search(hand.handText)
        if board:
            seen = hand.board['FLOP'] + hand.board['TURN'] + hand.board['RIVER']
            if ' '.join(seen) != board.group('CARDS'):
                log.error(_("GGPokerToFpdb.getRake: '%s': board %s != streets %s")
                          % (hand.handid, board.group('CARDS'), ' '.join(seen)))
                raise FpdbParseError

        hand.rake = rake
        self.note(hand, "rake=$%s jackpot=$%s bingo=$%s fortune=$%s tax=$%s"
                  % (m.group('RAKE'), m.group('JACKPOT'), m.group('BINGO'),
                     m.group('FORTUNE'), m.group('TAX')))

    def readOther(self, hand):
        # Doc §8.1 cross-check: per player, the summary won/collected amounts equal the body
        # 'collected' amounts in 344/345 hands. The exception is an EV-cashout hand whose summary
        # says won ($0) while the body says collected $2.13, so the body is the money of record and
        # a disagreement here is a note, not an error.
        summary = {}
        for seat in self.re_SummarySeat.finditer(hand.handText):
            for amount in self.re_SummaryMoney.findall(seat.group('CLAUSES')):
                name = seat.group('PNAME')
                summary[name] = summary.get(name, Decimal(0)) + Decimal(amount)
        for name, amount in summary.items():
            body = hand.collectees.get(name, Decimal(0))
            if amount != body:
                self.note(hand, "%s: summary $%s != body $%s" % (name, amount, body))

    re_SummarySeat = re.compile(r"^Seat \d+: (?P<PNAME>\S+) (?:\([^)]*\) )?"
                                r"(?P<CLAUSES>(?:folded|collected|showed).*)$", re.MULTILINE)
    re_SummaryMoney = re.compile(r"(?:won|collected) \(?\$([\d.]+)\)?")

    def readTourneyResults(self, hand):
        pass            # UNSEEN: tournaments are out of scope for this format (doc §9)
