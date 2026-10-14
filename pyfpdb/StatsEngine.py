"""StatsEngine: PokerTracker-style cash statistics aggregated straight out of the database.

Why a separate module and not the HUD cache or the `HandsPlayers` per-street raise columns:
measured on the 345-hand GGPoker corpus, DerivedStats *does* fill most player stat columns
correctly -- `street0VPI` is set on 544 rows, `street0_3BChance`/`street0_3BDone` on 715/72,
`raiseFirstInChance`/`raisedFirstIn` on 1102/246, the continuation-bet and fold-to-c-bet pairs,
the seen-street and showdown flags, all live. What those columns cannot give is the
*count* of bets and raises per street (`street0Raises..street4Raises` are 0 in every row, because
`DerivedStats.streetXRaises` deliberately zeroes them -- see DerivedStats.py:702-713), so
Aggression Factor, Aggression Frequency and per-street bet/fold/raise percentages have no home in
the schema. `allInEV` is 0 everywhere because `pokereval` is not installed (DerivedStats.py:30-34).
`street4*` is 0 too, and that is *correct* for hold'em: fpdb numbers the streets preflop=0,
flop=1, turn=2, river=3 and reserves street 4 for stud/draw.

So this engine has two data sources, and each stat names exactly one:

* `HandsPlayers` columns -- summed as numerator/denominator pairs for every stat DerivedStats
  already computes (VPIP, PFR, 3/4/cold-4 bet, fold-to-3/4-bet, squeeze, called-raise, RFI,
  steal, fold-blind-to-steal, c-bet and fold-to-c-bet per street, seen-flop/turn/river,
  went-to-showdown and the money columns). Aggregating the proven columns keeps the grid in exact
  agreement with the importer's own output and with `TestGgImport.py`.
* a replay of `HandsActions` -- only for the fields the schema does not store: the per-street
  counts of bets / raises / calls / checks / folds (aggression), plus limp, cold-call-preflop and
  went-all-in.

Everything is reported as numerator and denominator, never a pre-divided float, so rows can be
re-aggregated across any grouping (player, stake, day, position) without weighting errors.
Money is integer cents, the way the database stores it; `bb_net` is the one float, each hand's
profit divided by that hand's own big blind from `Gametypes`, so bb/100 stays right across stakes.
"""

from collections import defaultdict

# fpdb stores HandsPlayers.position as text: 'S' and 'B' are the blinds and the remaining seats
# count back from the button, so '0' is the button. Measured against seatNo on the 6-max GG corpus.
POSITION_LABELS = {"S": "SB", "B": "BB", "0": "BTN", "1": "CO", "2": "HJ", "3": "UTG",
                   "4": "UTG+1", "5": "UTG+2", "6": "MP1", "7": "MP2"}
POSITION_ORDER = ("BTN", "CO", "HJ", "UTG", "UTG+1", "UTG+2", "SB", "BB")
# PT3: "Steal Raise = an open raise by the cutoff, button, or small blind."
STEAL_POSITIONS = ("BTN", "CO", "SB")
STREETS = (0, 1, 2, 3)
STREET_NAME = {0: "PF", 1: "F", 2: "T", 3: "R"}
# The verb names as the Actions lookup table spells them (measured: ids 6-11, 14).
VERB_CALL, VERB_BET, VERB_RAISE, VERB_CHECK, VERB_FOLD = (
    "calls", "bets", "raises", "checks", "folds")

# counter_key -> HandsPlayers column. Each is a per-hand-player 0/1 flag; the engine sums them.
# This is the measured-live subset of DerivedStats' output.
HP_FIELDS = {
    "vpip": "street0VPI", "pfr": "street0Aggr",
    "b3": "street0_3BDone", "b3_opp": "street0_3BChance",
    "b4": "street0_4BDone", "b4_opp": "street0_4BChance",
    "cb4": "street0_C4BDone", "cb4_opp": "street0_C4BChance",
    "f3b": "street0_FoldTo3BDone", "f3b_opp": "street0_FoldTo3BChance",
    "f4b": "street0_FoldTo4BDone", "f4b_opp": "street0_FoldTo4BChance",
    "sqz": "street0_SqueezeDone", "sqz_opp": "street0_SqueezeChance",
    "callraise": "street0CalledRaiseDone", "callraise_opp": "street0CalledRaiseChance",
    "rfi": "raisedFirstIn", "rfi_opp": "raiseFirstInChance",
    "steal": "raiseToStealDone", "steal_opp": "raiseToStealChance",
    "steal_success": "success_Steal",
    "f_sb_steal": "foldedSbToSteal", "f_sb_steal_opp": "foldSbToStealChance",
    "f_bb_steal": "foldedBbToSteal", "f_bb_steal_opp": "foldBbToStealChance",
    "saw_f": "street1Seen", "saw_t": "street2Seen", "saw_r": "street3Seen",
    "won_f": "wonWhenSeenStreet1", "won_t": "wonWhenSeenStreet2", "won_r": "wonWhenSeenStreet3",
    "sd": "sawShowdown", "won_sd": "wonAtSD",
    "cb_f": "street1CBDone", "cb_f_opp": "street1CBChance",
    "cb_t": "street2CBDone", "cb_t_opp": "street2CBChance",
    "cb_r": "street3CBDone", "cb_r_opp": "street3CBChance",
    "fcb_f": "foldToStreet1CBDone", "fcb_f_opp": "foldToStreet1CBChance",
    "fcb_t": "foldToStreet2CBDone", "fcb_t_opp": "foldToStreet2CBChance",
    "fcb_r": "foldToStreet3CBDone", "fcb_r_opp": "foldToStreet3CBChance",
    "showed": "showed",
}
HP_MONEY = {"net": "totalProfit", "rake": "rake", "winnings": "winnings",
            "ev_cents": "allInEV", "allin_street": "wentAllInOnStreet"}


def position_label(raw):
    return POSITION_LABELS.get(str(raw), "")


def replay_actions(actions):
    """Count per-street actions and the fields the schema does not store, for one hand.

    `actions` is the hand's `HandsActions` rows in action order, each a tuple
    ``(street, playerId, verb, amount, raiseTo, allIn)``. Returns ``{playerId: {counter: value}}``.
    The per-street bet/raise/call/check/fold counts feed aggression; limp, cold-call-preflop and
    went-all-in have no column at all and only exist here.
    """
    per = defaultdict(lambda: defaultdict(float))
    by_street = defaultdict(list)
    for street, pid, verb, _amount, _raise_to, all_in in actions:
        by_street[street].append((pid, verb, all_in))
    for street in STREETS:
        rows = by_street.get(street, [])
        if not rows:
            continue
        raise_seen = False          # a bet/raise has happened this round (preflop open)
        invested = set()            # players who have already put money in voluntarily
        for pid, verb, all_in in rows:
            c = per[pid]
            if all_in:
                c["allin"] += 1
            if verb == VERB_CALL:
                c["call%d" % street] += 1
                if street == 0:
                    if not raise_seen:
                        c["limp"] += 1        # called the BB with no raise in front
                    elif pid not in invested:
                        c["ccpf"] += 1        # cold call facing a raise, no prior investment
            elif verb == VERB_BET:
                c["bet%d" % street] += 1
                c["agg%d" % street] += 1
                if street == 0:
                    raise_seen = True
            elif verb == VERB_RAISE:
                c["raise%d" % street] += 1
                c["agg%d" % street] += 1
                if street == 0:
                    raise_seen = True
            elif verb == VERB_CHECK:
                c["check%d" % street] += 1
            elif verb == VERB_FOLD:
                c["fold%d" % street] += 1
            if verb in (VERB_CALL, VERB_BET, VERB_RAISE):
                invested.add(pid)
    return per


class Filters(object):
    """A WHERE clause over the Hands/Gametypes join plus an optional player-name constraint."""

    def __init__(self, date_from=None, date_to=None, site=None, gametype_id=None,
                 limit_type=None, category=None, player=None, min_seats=None,
                 max_seats=None, tourney_id=None, include_tournaments=False):
        # keep the arguments as attributes so a caller (e.g. the hand-history panel) can rebuild a
        # narrower Filters from an existing one without re-parsing the SQL.
        self.date_from, self.date_to, self.site = date_from, date_to, site
        self.gametype_id, self.limit_type, self.category = gametype_id, limit_type, category
        self.player, self.min_seats, self.max_seats = player, min_seats, max_seats
        self.tourney_id = tourney_id
        clauses, params = [], []
        if tourney_id:
            clauses.append("h.tourneyId = ?")
            params.append(tourney_id)
        elif not include_tournaments:
            # Cash queries must skip tournament hands: a tour gametype's blind is a chip level
            # (30000/60000 chips reads as '$300/$600'), so leaving tour hands in a cash By-Stakes
            # grid invents high-stakes rows and corrupts every per-stake winrate.
            clauses.append("h.tourneyId is null")
        if date_from:
            clauses.append("h.startTime >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("h.startTime <= ?")
            params.append(date_to + " 23:59:59" if len(date_to) == 10 else date_to)
        if site:
            clauses.append("s.name = ?")
            params.append(site)
        if gametype_id:
            clauses.append("h.gametypeId = ?")
            params.append(gametype_id)
        if limit_type:
            clauses.append("g.limitType = ?")
            params.append(limit_type)
        if category:
            clauses.append("g.category = ?")
            params.append(category)
        if min_seats:
            clauses.append("h.seats >= ?")
            params.append(min_seats)
        if max_seats:
            clauses.append("h.seats <= ?")
            params.append(max_seats)
        if player:
            clauses.append("p.name LIKE ?")
            params.append("%%%s%%" % player)
        self.sql = (" where " + " and ".join(clauses)) if clauses else ""
        self.params = params

    def hand_ids(self, cur):
        """Hand ids matching the filters, newest first, as (id, startTime) tuples."""
        return cur.execute(
            "select h.id, h.startTime from Hands h "
            "join Gametypes g on g.id = h.gametypeId "
            "left join Sites s on s.id = g.siteId "
            "join HandsPlayers hp on hp.handId = h.id "
            "join Players p on p.id = hp.playerId"
            + self.sql + " group by h.id, h.startTime order by h.startTime desc, h.id desc",
            self.params).fetchall()


# Each stat is (key, label, kind, numerator_key(s), denominator_key). `kind` tells the GUI how to
# format: 'pct' -> 100*n/d, 'ratio' -> n/d, 'money' -> cents as $, 'bb' -> bb/100, 'int' -> n,
# 'count' -> n. A None denominator means the value is the numerator itself.
def _ratio(c, num, den):
    n = sum(c.get(k, 0) for k in num) if isinstance(num, tuple) else c.get(num, 0)
    d = c.get(den, 0) if den is not None else None
    return (n, d)


STATS = [
    # --- results ---
    ("hands", "Hands", "int", "hands", None),
    ("net", "Net Won", "money", "net", None),
    ("bb100", "bb/100", "bb", "bb_net", "hands"),
    ("ev", "All-In EV", "money", "ev", None),
    ("evbb100", "EV bb/100", "bb", "bb_ev", "hands"),
    ("won", "Won $", "money", "winnings", None),
    ("won_pct", "Won %", "pct", "won_hands", "hands"),
    ("rake", "Rake", "money", "rake", None),
    ("net_sd", "Net $SD", "money", "net_sd", None),
    ("net_nsd", "Net Non-SD", "money", "net_nsd", None),
    # --- preflop ---
    ("vpip", "VPIP", "pct", "vpip", "hands"),
    ("pfr", "PFR", "pct", "pfr", "hands"),
    ("b3", "3-Bet", "pct", "b3", "b3_opp"),
    ("b4", "4-Bet", "pct", "b4", "b4_opp"),
    ("cb4", "Cold 4-Bet", "pct", "cb4", "cb4_opp"),
    ("f3b", "Fold to 3-Bet", "pct", "f3b", "f3b_opp"),
    ("f4b", "Fold to 4-Bet", "pct", "f4b", "f4b_opp"),
    ("sqz", "Squeeze", "pct", "sqz", "sqz_opp"),
    ("callraise", "Cold Call vs Raise", "pct", "callraise", "callraise_opp"),
    ("limp", "Limp", "pct", "limp", "hands"),
    ("ccpf", "CCPF", "pct", "ccpf", "hands"),
    ("rfi", "RFI", "pct", "rfi", "rfi_opp"),
    ("steal", "Attempt Steal", "pct", "steal", "steal_opp"),
    ("steal_success", "Steal", "pct", "steal_success", "steal"),
    ("f_bb_steal", "Fold BB to Steal", "pct", "f_bb_steal", "f_bb_steal_opp"),
    ("f_sb_steal", "Fold SB to Steal", "pct", "f_sb_steal", "f_sb_steal_opp"),
    # --- postflop ---
    ("cb_f", "C-Bet Flop", "pct", "cb_f", "cb_f_opp"),
    ("cb_t", "C-Bet Turn", "pct", "cb_t", "cb_t_opp"),
    ("cb_r", "C-Bet River", "pct", "cb_r", "cb_r_opp"),
    ("fcb_f", "Fold to CB Flop", "pct", "fcb_f", "fcb_f_opp"),
    ("fcb_t", "Fold to CB Turn", "pct", "fcb_t", "fcb_t_opp"),
    ("fcb_r", "Fold to CB River", "pct", "fcb_r", "fcb_r_opp"),
    ("saw_f", "Saw Flop", "pct", "saw_f", "hands"),
    ("saw_t", "Saw Turn", "pct", "saw_t", "saw_f"),
    ("saw_r", "Saw River", "pct", "saw_r", "saw_t"),
    ("wtsd", "WTSD", "pct", "sd", "hands"),
    ("w$sd", "W$SD", "pct", "won_sd", "sd"),
    ("wwsf", "WWSF", "pct", "won_f", "saw_f"),
    ("w$wsf", "W$WSF", "pct", "won_f", "saw_f"),
    # --- aggression (replay-derived; PT AF and Aggr% count the flop/turn/river streets only) ---
    ("aggf", "Aggr Factor", "ratio", "agg_post", "call_post"),
    ("aggq", "Aggr %", "pct", "agg_post", "aggq_den"),
    ("bet_f", "Bet Flop %", "pct", "bet1", "street1_actions"),
    ("raise_f", "Raise Flop %", "pct", "raise1", "street1_actions"),
    ("fold_f", "Fold Flop %", "pct", "fold1", "street1_actions"),
    ("allin", "Went All-In %", "pct", "allin_hands", "hands"),
]

# Derived helper keys the aggregate pass fills so the formulas above read a single counter.
# agg/call/fold/bet/raise per street come straight from the replay (flop=1, turn=2, river=3).
DERIVED = {
    "agg_post": lambda c: c.get("agg1", 0) + c.get("agg2", 0) + c.get("agg3", 0),
    "call_post": lambda c: c.get("call1", 0) + c.get("call2", 0) + c.get("call3", 0),
    "aggq_den": lambda c: (c.get("agg1", 0) + c.get("agg2", 0) + c.get("agg3", 0)
                           + c.get("call1", 0) + c.get("call2", 0) + c.get("call3", 0)
                           + c.get("fold1", 0) + c.get("fold2", 0) + c.get("fold3", 0)),
    # a decision to bet/fold on the flop is any flop action by a player still to act
    "street1_actions": lambda c: (c.get("bet1", 0) + c.get("raise1", 0) + c.get("call1", 0)
                                  + c.get("check1", 0) + c.get("fold1", 0)),
}


class Engine(object):
    """Streams hands and accumulates counters into one dict per group."""

    def __init__(self, conn):
        self.conn = conn
        self._verbs = None

    def _load_hand(self, cur, hand_id):
        meta = cur.execute(
            "select h.gametypeId, h.startTime, h.seats, g.smallBlind, g.bigBlind, g.category, "
            "g.limitType, s.name from Hands h left join Gametypes g on g.id = h.gametypeId "
            "left join Sites s on s.id = g.siteId where h.id = ?", (hand_id,)).fetchone()
        players = cur.execute(
            "select hp.playerId, p.name, hp.position, " +
            ", ".join(hp_col for hp_col in HP_FIELDS.values()) + ", " +
            ", ".join(HP_MONEY.values()) +
            " from HandsPlayers hp join Players p on p.id = hp.playerId where hp.handId = ?",
            (hand_id,)).fetchall()
        actions = cur.execute(
            "select ha.street, ha.playerId, a.name, ha.amount, ha.raiseTo, ha.allIn "
            "from HandsActions ha join Actions a on a.id = ha.actionId where ha.handId = ? "
            "order by ha.actionNo", (hand_id,)).fetchall()
        return meta, players, actions

    @staticmethod
    def _split_row(meta, player_row):
        """Return (pid, name, position, counter dict) for one HandsPlayers row."""
        (pid, name, position) = player_row[0], player_row[1], player_row[2]
        hp_cols = list(HP_FIELDS.values())
        money_cols = list(HP_MONEY.values())
        vals = player_row[3:]
        hp = dict(zip(hp_cols + money_cols, vals))
        c = defaultdict(float)
        for key, col in HP_FIELDS.items():
            c[key] += 1 if hp[col] else 0
        net = hp["totalProfit"] or 0
        c["net"] += net
        c["rake"] += hp["rake"] or 0
        c["winnings"] += hp["winnings"] or 0
        c["hands"] += 1
        if hp["winnings"]:
            c["won_hands"] += 1
        saw_sd = bool(hp["sawShowdown"])
        if saw_sd:
            c["net_sd"] += net
        else:
            c["net_nsd"] += net
        big_blind = (meta[4] or 1)
        c["bb_net"] += net / float(big_blind or 1)
        # `allInEV` only carries a number for the rows AllInEv.fill_column touched, and it marks them
        # with wentAllInOnStreet; every other hand's expected value is simply the result it got.
        if hp["wentAllInOnStreet"] is None:
            c["ev"] += net
            c["bb_ev"] += net / float(big_blind or 1)
        else:
            ev = hp["allInEV"] or 0
            c["ev"] += ev
            c["bb_ev"] += ev / float(big_blind or 1)
            c["ev_hands"] += 1
        return pid, name, position_label(position), c

    def aggregate(self, filters=None, group_by=("player",)):
        """Return {group_key_tuple: counter dict} over every hand matching `filters`.

        `group_by` names any of 'player', 'day', 'gametype', 'site', 'game'. A single 'position'
        grouping is handled by `position_report`, which re-uses these per-hand replays.
        """
        filters = filters or Filters()
        cur = self.conn.cursor()
        hand_ids = filters.hand_ids(cur)
        groups = defaultdict(lambda: defaultdict(float))
        n = 0
        for hand_id, _start in hand_ids:
            meta, players, actions = self._load_hand(cur, hand_id)
            replayed = replay_actions(actions)
            for prow in players:
                pid, _name, poslabel, c = self._split_row(meta, prow)
                for k, v in replayed.get(pid, {}).items():
                    if k == "allin":
                        c["allin_hands"] += 1 if v else 0
                    else:
                        c[k] += v
                key = self._group_key(group_by, meta, pid, _name, poslabel)
                tgt = groups[key]
                for k, v in c.items():
                    tgt[k] += v
            n += 1
        for grp in groups.values():
            for name, fn in DERIVED.items():
                grp[name] = fn(grp)
        self.last_hand_count = n
        return groups

    @staticmethod
    def _group_key(group_by, meta, pid, name, poslabel):
        parts = []
        for token in group_by:
            if token == "player":
                parts.append(name)
            elif token == "day":
                parts.append((meta[1] or "")[:10])
            elif token == "gametype":
                parts.append(meta[0])
            elif token == "site":
                parts.append(meta[7])
            elif token == "game":
                parts.append("%s-%s" % (meta[5], meta[6]))
            elif token == "position":
                parts.append(poslabel)
            else:
                raise ValueError("unknown group_by token %r" % token)
        return tuple(parts)

    def series(self, filters=None, player=None):
        """Cumulative bb lines over the filtered hands in time order, for the graph.

        Returns a list of ``{"n", "t", "net", "sd", "nsd", "ev"}`` where each money value is a running
        total in big blinds. `player` restricts the walk to one name (the hero's own winrate);
        when it is None every seat at the filtered hands is summed, i.e. the whole field's net.
        The showdown and non-showdown running totals split each hand's profit by `sawShowdown`, so
        the two lines add back to the net line.

        The all-in-EV line replaces a hand's result with `HandsPlayers.allInEV` wherever the seat had
        money at risk -- which is what `AllInEv.fill_column` writes -- and keeps the real result
        everywhere else, so `ev` and `net` differ only by the variance the rail took. Before that pass
        runs, `allInEV` is 0 on rows that never carried an all-in and the two lines coincide; `ev_hands`
        in the same output says how many hands contributed an EV, so a flat overlap can be read as
        'not computed yet' rather than 'running even'.
        """
        filters = filters or Filters()
        cur = self.conn.cursor()
        hand_ids = sorted(filters.hand_ids(cur), key=lambda r: (r[1], r[0]))
        out = []
        net = sd = nsd = ev = 0.0
        n = ev_hands = 0
        for hand_id, start in hand_ids:
            meta, players, _actions = self._load_hand(cur, hand_id)
            bb = (meta[4] or 1) or 1
            took = False
            for prow in players:
                _pid, name, _pos, c = self._split_row(meta, prow)
                if player is not None and name != player:
                    continue
                net += c["net"] / float(bb)
                ev += c["bb_ev"]
                if c["ev_hands"]:
                    ev_hands += 1
                if c["sd"]:
                    sd += c["net"] / float(bb)
                else:
                    nsd += c["net"] / float(bb)
                took = True
            if not took:
                continue
            n += 1
            out.append({"n": n, "t": start, "net": net, "sd": sd, "nsd": nsd, "ev": ev,
                        "ev_hands": ev_hands})
        return out


def value(counters, key):
    """Compute a stat's (display, numerator, denominator) from an aggregated counter dict."""
    for (k, label, kind, num, den) in STATS:
        if k != key:
            continue
        n, d = _ratio(counters, num, den)
        if kind == "int" or kind == "count":
            return (int(n), n, None)
        if kind == "money":
            return (cents(n), n, None)
        if d in (None, 0):
            return ("", n, d)
        if kind == "pct":
            return ("%.1f%%" % (100.0 * n / d), n, d)
        if kind == "ratio":
            return ("%.2f" % (float(n) / d), n, d)
        if kind == "bb":
            return ("%.1f" % (100.0 * n / d), n, d)
        return ("%g" % (float(n) / d), n, d)
    raise KeyError("no such stat %r" % key)


def cents(value_):
    v = int(round(value_))
    return ("-$%.2f" if v < 0 else "$%.2f") % (abs(v) / 100.0)


def report(conn, filters=None, group_by=("player",), columns=None, min_hands=1):
    """Aggregate and render each group as {key: (display, num, den)} plus its group label."""
    engine = Engine(conn)
    groups = engine.aggregate(filters, group_by)
    columns = columns or [k for (k, _l, _kind, _n, _d) in STATS]
    rows = []
    for key, counters in groups.items():
        if counters.get("hands", 0) < min_hands:
            continue
        row = {"group": key}
        for col in columns:
            row[col] = value(counters, col)
        row["_counters"] = counters
        rows.append(row)
    rows.sort(key=lambda r: -r["_counters"].get("hands", 0))
    return rows


def ordinal(number):
    """1 -> '1st', 2 -> '2nd', 21 -> '21st'. The GG summary writes exactly this form ('1529th:')."""
    if number is None:
        return ""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return "%d%s" % (number, suffix)


# One row per tournament with a known finish (TourneysPlayers.rank, which only the summary importer
# writes). Nothing here comes from the cash STATS table: tournament HandsPlayers totals are in
# CHIPS -- measured on this corpus Hero's `totalProfit` sums to -1,000,000 in a $2 Daily Classic --
# so the only money in a tournament row is the real cents the summary stored: TourneyTypes
# (buyin/fee/koBounty), Tourneys (prizepool) and TourneysPlayers.winnings.
#
# 'money' is a win/loss and the grid colours it by sign; 'amount' is a price or a pot that carries
# no win/loss meaning, so it must not go green just because a prize pool is a positive number.
TOURNEY_COLUMNS = [
    ("date", "Date", "text"),
    ("fields", "Fields", "int"),
    ("prizepool", "Prize Pool", "amount"),
    ("cost", "Buy-In", "amount"),
    ("place", "Place", "text"),
    ("place_pct", "Place %", "pct"),
    ("winnings", "Winnings", "amount"),
    ("net", "Net", "money"),
    ("hands", "Hands", "int"),
    ("kind", "Type", "text"),
]
TOURNEY_BY_KEY = {k: (label, kind) for (k, label, kind) in TOURNEY_COLUMNS}
DEFAULT_TOURNEY_COLUMNS = [k for (k, _l, _kind) in TOURNEY_COLUMNS]


def _tourney_where(filters):
    """The tournament half of `Filters`: the same GUI filters, expressed over t/tt/p.

    `Filters.sql` is written over `Hands h join Gametypes g`, which a tournament row is not, so the
    usable clauses are re-derived from the same attributes rather than re-used. Gametype/stake
    clauses are deliberately absent: a tournament's `Gametypes` row carries level blinds, not a
    stake the player chose, so the stakes filter would silently mean something else here.
    """
    clauses, params = ["tp.rank is not null"], []
    if filters.date_from:
        clauses.append("t.startTime >= ?")
        params.append(filters.date_from)
    if filters.date_to:
        clauses.append("t.startTime <= ?")
        params.append(filters.date_to + " 23:59:59" if len(filters.date_to) == 10
                      else filters.date_to)
    if filters.site:
        clauses.append("st.name = ?")
        params.append(filters.site)
    if filters.limit_type:
        clauses.append("tt.limitType = ?")
        params.append(filters.limit_type)
    if filters.category:
        clauses.append("tt.category = ?")
        params.append(filters.category)
    if filters.player:
        clauses.append("p.name LIKE ?")
        params.append("%%%s%%" % filters.player)
    if filters.tourney_id:
        clauses.append("t.id = ?")
        params.append(filters.tourney_id)
    return " where " + " and ".join(clauses), params


def tournament_report(conn, filters=None):
    """Aggregate the stored tournament results, newest first, with a totals row on top.

    Returns rows in the same shape as `report` -- {column: (display, num, den)} plus 'group',
    '_label' and '_counters' -- so the grid model renders either report without knowing which;
    every column in TOURNEY_COLUMNS is filled and the caller picks which to show.
    'cost' is the whole ticket the player paid: buyin + fee + knockout bounty (measured: the
    $2.50 Bounty Hunters row stores 130 + 20 + 100 = 250 cents).
    """
    filters = filters or Filters()
    cur = conn.cursor()
    where, params = _tourney_where(filters)
    sql = ("select t.id, t.tourneyName, t.siteTourneyNo, t.startTime, t.entries, t.prizepool, "
           "tt.currency, tt.buyin, tt.fee, tt.koBounty, tt.knockout, tt.maxSeats, tt.category, "
           "tt.limitType, tp.rank, tp.winnings, p.id, p.name "
           "from Tourneys t "
           "left join TourneyTypes tt on tt.id = t.tourneyTypeId "
           "left join Sites st on st.id = tt.siteId "
           "join TourneysPlayers tp on tp.tourneyId = t.id "
           "join Players p on p.id = tp.playerId" + where +
           " order by t.startTime desc, t.id desc")
    # Hands played per seat, in one pass: `Hands.tourneyId` is set by the hand importer, so a
    # tournament with hands but no summary still shows its hand count here.
    hand_counts = dict(((tid, pid), n) for tid, pid, n in cur.execute(
        "select h.tourneyId, hp.playerId, count(distinct h.id) from Hands h "
        "join HandsPlayers hp on hp.handId = h.id where h.tourneyId is not null "
        "group by h.tourneyId, hp.playerId").fetchall())
    rows = []
    for (tid, name, tourno, start, entries, prizepool, currency, buyin, fee, ko_bounty,
         knockout, max_seats, category, limit_type, rank, winnings, pid, player) in \
            cur.execute(sql, params).fetchall():
        ticket = (buyin or 0) + (fee or 0) + ((ko_bounty or 0) if knockout else 0)
        net = (winnings or 0) - ticket
        hands = hand_counts.get((tid, pid), 0)
        game = {"holdem": "Hold'em", "omahahi": "Omaha"}.get(category, category or "?")
        rows.append({
            "group": (tid, player),
            "_label": "%s (#%s)" % (name, tourno),
            "_currency": currency,
            "_counters": {"hands": hands, "net": net, "winnings": winnings or 0, "cost": ticket,
                          "fields": entries or 0, "prizepool": prizepool or 0, "rank": rank,
                          "tournaments": 1, "itm": 1 if (winnings or 0) > 0 else 0},
            "date": (start or "", None, None),
            "fields": (entries, entries, None),
            "prizepool": (cents(prizepool or 0), prizepool or 0, None),
            "cost": (cents(ticket), ticket, None),
            "place": (ordinal(rank), rank, None),
            "place_pct": ("" if not entries else "%.1f%%" % (100.0 * rank / entries), rank, entries),
            "winnings": (cents(winnings or 0), winnings or 0, None),
            "net": (cents(net), net, None),
            "hands": (hands, hands, None),
            "kind": ("%s %s%s" % (game, (limit_type or "?").upper(),
                                  " %d-max" % max_seats if max_seats else ""), None, None),
        })
    if rows:
        rows.insert(0, _tourney_totals(rows))
    return rows


def _tourney_totals(rows):
    """The grid's top row: sums over the listed tournaments.

    'Place %' here is the *mean* of the per-tournament rank/field ratios, not sum(rank)/sum(fields):
    pooling the ranks would let a 15,000-entry freeroll drown a 1,800-entry Classic, and the number
    a player reads off an "average finish" column is the one he means per tournament.
    """
    c = defaultdict(float)
    for row in rows:
        for key, value_ in row["_counters"].items():
            c[key] += value_
    with_fields = [r for r in rows if r["_counters"]["fields"]]
    mean_place = (100.0 * sum(r["_counters"]["rank"] / float(r["_counters"]["fields"])
                              for r in with_fields) / len(with_fields)) if with_fields else 0
    return {"group": ("totals",), "_label": "TOTAL", "_currency": rows[0]["_currency"],
            "_counters": dict(c),
            "date": ("%d tournaments" % len(rows), None, None),
            "fields": (int(c["fields"]), c["fields"], None),
            "prizepool": (cents(c["prizepool"]), c["prizepool"], None),
            "cost": (cents(c["cost"]), c["cost"], None),
            "place": ("%d ITM" % c["itm"], None, None),
            "place_pct": ("%.1f%% mean" % mean_place, None, None),
            "winnings": (cents(c["winnings"]), c["winnings"], None),
            "net": (cents(c["net"]), c["net"], None),
            "hands": (int(c["hands"]), c["hands"], None),
            "kind": ("", None, None)}


def _selftest(conn):
    """Reconcile the engine against raw SQL over the same corpus. Prints PASS/FAIL per check."""
    cur = conn.cursor()
    total = Engine(conn).aggregate(Filters(), group_by=("site",))
    counters = next(iter(total.values())) if total else {}

    def sql_one(sql):
        return cur.execute(sql).fetchone()[0]

    checks = []

    def eq(name, got, want):
        checks.append((name, got, want, got == want))

    # The engine's default Filters are cash-only (`h.tourneyId is null`), so every baseline here is
    # scoped to cash hands too -- otherwise a combined database fails these checks by the exact count
    # of its tournament seats, which is a scoping bug in the test, not in the engine.
    CASH_HP = (" from HandsPlayers hp join Hands h on h.id = hp.handId"
               " where h.tourneyId is null")
    eq("hands", int(counters.get("hands", 0)),
       sql_one("select count(*)" + CASH_HP))
    for key, col in [("vpip", "street0VPI"), ("b3", "street0_3BDone"),
                     ("b3_opp", "street0_3BChance"), ("rfi", "raisedFirstIn"),
                     ("rfi_opp", "raiseFirstInChance"), ("cb_f", "street1CBDone"),
                     ("cb_f_opp", "street1CBChance"), ("sd", "sawShowdown"),
                     ("won_sd", "wonAtSD")]:
        eq(key, int(counters.get(key, 0)),
           sql_one("select count(*)" + CASH_HP + " and hp.%s" % col))
    eq("net", int(counters.get("net", 0)),
       sql_one("select coalesce(sum(hp.totalProfit),0)" + CASH_HP))
    eq("rake", int(counters.get("rake", 0)),
       sql_one("select coalesce(sum(hp.rake),0)" + CASH_HP))

    # HandsActions replay: the verb counts must equal the action rows exactly.
    CASH_HA = (" from HandsActions ha join Hands h on h.id = ha.handId"
               " where h.tourneyId is null")
    verb_ids = dict(cur.execute("select name, id from Actions").fetchall())
    for street, prefix in [(0, "call0"), (1, "call1"), (2, "call2"), (3, "call3")]:
        want = cur.execute("select count(*)" + CASH_HA + " and ha.street=? and ha.actionId=?",
                           (street, verb_ids["calls"])).fetchone()[0]
        eq(prefix, int(counters.get(prefix, 0)), want)
    want_raises = cur.execute("select count(*)" + CASH_HA + " and ha.actionId=?",
                              (verb_ids["raises"],)).fetchone()[0]
    eq("raise_total", int(sum(counters.get("raise%d" % s, 0) for s in STREETS)), want_raises)

    fails = [c for c in checks if not c[3]]
    for name, got, want, ok in checks:
        print("  %s %-14s engine=%-8s sql=%-8s" % ("OK  " if ok else "FAIL", name, got, want))
    print("selftest: %d checks, %d failures" % (len(checks), len(fails)))
    return 0 if not fails else 1


def _tourney_selftest(conn):
    """Reconcile `tournament_report` against raw SQL over the stored tourneys.

    Skipped (returns 0) when the database has no finished tournaments, so the cash-only corpora this
    engine is usually run against stay green. When tourneys exist, every money figure is checked
    against the columns it is built from -- the point is to catch a sign or scale slip in the cents
    arithmetic, not to re-assert a premise.
    """
    cur = conn.cursor()
    if not cur.execute("select count(*) from TourneysPlayers where rank is not null").fetchone()[0]:
        print("tourney selftest: no finished tournaments, skipped")
        return 0
    rows = [r for r in tournament_report(conn) if r["group"][0] != "totals"]
    checks = []

    def eq(name, got, want):
        checks.append((name, got, want, got == want))

    eq("events", len(rows),
       cur.execute("select count(*) from TourneysPlayers where rank is not null").fetchone()[0])
    # The whole grid's net must equal the SQL sum of (winnings - ticket) over the same rows.
    sql_net = 0
    for (tid, pid, rank, win, buyin, fee, ko, knockout) in cur.execute(
            "select tp.tourneyId, tp.playerId, tp.rank, tp.winnings, tt.buyin, tt.fee, "
            "tt.koBounty, tt.knockout from TourneysPlayers tp "
            "join Tourneys t on t.id = tp.tourneyId "
            "left join TourneyTypes tt on tt.id = t.tourneyTypeId where tp.rank is not null"):
        sql_net += (win or 0) - ((buyin or 0) + (fee or 0) + ((ko or 0) if knockout else 0))
    eq("net_sum", int(sum(r["_counters"]["net"] for r in rows)), sql_net)
    # Each row's place % must be exactly rank/fields, and its hands count the distinct tour hands.
    for row in rows:
        tid, player = row["group"]
        rank, fields = row["_counters"]["rank"], row["_counters"]["fields"]
        want = "" if not fields else "%.1f%%" % (100.0 * rank / fields)
        eq("place_pct#%d" % tid, row["place_pct"][0], want)
        want_hands = cur.execute(
            "select count(distinct h.id) from Hands h join HandsPlayers hp on hp.handId = h.id "
            "join Players p on p.id = hp.playerId where h.tourneyId = ? and p.name = ?",
            (tid, player)).fetchone()[0]
        eq("hands#%d" % tid, int(row["_counters"]["hands"]), want_hands)
    fails = [c for c in checks if not c[3]]
    for name, got, want, ok in checks:
        print("  %s %-14s report=%-12s sql=%-12s" % ("OK  " if ok else "FAIL", name, got, want))
    print("tourney selftest: %d checks, %d failures" % (len(checks), len(fails)))
    return 0 if not fails else 1


def main(argv=None):
    import sys
    import os
    import sqlite3
    argv = argv if argv is not None else sys.argv[1:]
    db_path = argv[0] if argv else os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "fpdb", "database", "fpdb")
    conn = sqlite3.connect(db_path)
    try:
        rows = report(conn, Filters(), group_by=("player",), min_hands=1)
        cols = ["vpip", "pfr", "b3", "rfi", "wtsd", "w$sd", "aggf", "bb100"]
        print("%-16s %6s %s" % ("player", "hands", "  ".join("%-11s" % c for c in cols)))
        for r in rows[:15]:
            cells = "  ".join("%-11s" % (r[c][0] or "-") for c in cols)
            print("%-16s %6d %s" % (r["group"][0][:16], r["_counters"]["hands"], cells))
        print()
        cash = _selftest(conn)
        tour = _tourney_selftest(conn)
        return 0 if cash == 0 and tour == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
