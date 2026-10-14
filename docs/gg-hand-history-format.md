# GGPoker "Rush & Cash" hand-history format — measured spec

Source of truth: the 10 files in `%USERPROFILE%\Documents\Projects\Poker\history\games\*.txt`
(345 hands). Regenerate the inventory with `python tools/gg_format_probe.py <corpus-dir>`; if this
doc and that script disagree, the script is right and this doc is stale.

Every count below is a count of lines in that corpus. Anything marked **UNSEEN** does not occur in
the corpus and the parser must reject it loudly rather than guess (see §9).

Scope: GGPoker cash, Rush & Cash fast-fold, 6-max, `$0.01/$0.02`, Hold'em No Limit and Omaha Pot
Limit. That is all this corpus contains.

The corpus itself is **not in this repository**: hand histories are private play data, so
`pyfpdb/regression-test-files/cash/GGPoker/` is git-ignored and exists only on the machine that
collected it. The player aliases, hand ids, table ids and timestamps in the examples below are
therefore placeholders that match the measured *shape* (§7.2: 6–8 chars of `[0-9a-f]`, `RC` +
digits) but belong to no real hand. Counts, line grammar and amount behaviour come straight from
the corpus.

---

## 1. File layout

```
GG<YYYYMMDD>-<HHMM> - <RushAndCashTABLEID> - <SB> - <BB> - <SEATS>max.txt
```

Example: `GG20260115-1628 - RushAndCash1000001 - 0.01 - 0.02 - 6max.txt`

- A file is a flat concatenation of hand blocks: no file header, no blank-line requirement.
- **Hands are ordered newest-first** (reverse chronological). Measured: all 10 files are strictly
  descending by header timestamp *and* by hand id. Hand-id order == time order, so sorting by hand
  id rebuilds chronology. Never assume file order is import order.
- The table id in the filename matches **at most one** hand header in that file (§7.1).
- Filename clock = oldest hand's header clock + exactly 6 h, truncated to the minute. Measured
  offset over 10 files: 359.1–360.0 min. Header times are the site clock, the filename is local
  (UTC+6). **Use the header time as the authoritative timestamp.**
- Encoding: UTF-8, `\n` line endings, no BOM. The local player is always the literal token `Hero`.

## 2. Hand block skeleton

Every hand in the corpus (345/345) has this shape; line classes are the ones in §3:

```
Poker Hand #<HANDID>: <GAME> ($<SB>/$<BB>) - <DATE TIME>              1  always
Table '<TABLEID>' <N>-max Seat #<BTN> is the button                   1  always
Seat <i>: <player> ($<stack> in chips)                                6  always (6-max)
[Cash Drop to Pot : total $<amt>]                                     0..1
<sb-player>: posts small blind $<sb>                                  1
<bb-player>: posts big blind $<bb>                                    1
*** HOLE CARDS ***                                                    1
Dealt to <seat-i-player> [<cards>]                                    6  in seat order
<preflop action lines>
*** FLOP *** [<3 cards>]          (or *** FIRST FLOP ***)             0..1
<flop action lines>
*** TURN *** [<board>] [<new card>]                                   0..1
<turn action lines>
*** RIVER *** [<board>] [<new card>]                                  0..1
<river action lines>
[Uncalled bet ($<amt>) returned to <player>]                          0..1
[<player>: shows [<cards>] [(<hand rank>)]]                           0..n  may appear EARLY, §6
*** SHOWDOWN ***      (or *** FIRST SHOWDOWN *** / *** SECOND SHOWDOWN ***)
<player> collected $<amt> from pot                                    1..n  side pots, §8.1
*** SUMMARY ***                                                       1  always
Total pot $<pot> | Rake $<r> | Jackpot $<j> | Bingo $<b> | Fortune $<f> | Tax $<t>   1  always
[Hand was run two times] [FIRST Board [...]] [SECOND Board [...]]
Board [<5 cards>]              (only when not run twice)
Seat <i>: <player>[ (<role>)] <outcome clause>[, <clause>]...          6  one per seat
```

Measured street-marker counts: `*** HOLE CARDS ***` 345, `*** FLOP ***` 182, `*** TURN ***` 119,
`*** RIVER ***` 82, `*** SHOWDOWN ***` 343 (the other 2 hands are run-it-twice, §8.3),
`*** SUMMARY ***` 345. **161 hands deal no board at all** (everyone but one player folds preflop);
161 no-board + 182 single-board + 2 run-it-twice = 345.

## 3. Line reference table

Money is always `$` followed by a number with **0, 1 or 2 decimals**, never a thousands separator in
this corpus (largest amount anywhere: `$29.96`).

| Count | Line shape | Notes |
|---|---|---|
| 345 | `Poker Hand #RC900000001: Omaha Pot Limit ($0.01/$0.02) - 2026/01/15 10:42:55` | hand id is `RC` + digits. `<GAME>` ∈ {`Hold'em No Limit` (167), `Omaha Pot Limit` (178)}. **The timestamp is `YYYY/MM/DD HH:MM:SS` — solidus separators, plain space before the time** (confirmed by an ord dump of the bytes: `0x2f`). A regex written as `\d{4}-\d{2}-\d{2}` matches nothing in this corpus. |
| 345 | `Table 'RushAndCash1000002' 6-max Seat #1 is the button` | `Seat #<n>` is 1-based; always `6-max` here |
| 1725 + 345 | `Seat 2: a1b2c3d4 ($5.38 in chips)` / `Seat 1: Hero ($2 in chips)` | 6 per hand, 2070 total |
| 4 | `Cash Drop to Pot : total $0.2` | spaces around `:` **and a trailing space**. Sits after the seat lines, before the blinds. Promotional money that is in the pot but was contributed by no player. |
| 287 + 58 | `a1b2c3d4: posts small blind $0.01` | 1 per hand, always before `*** HOLE CARDS ***`. **UNSEEN**: ante, bring-in, straddle. |
| 288 + 57 | `0f1e2d3: posts big blind $0.02` | 1 per hand |
| 1725 | `Dealt to a1b2c3d4` | **trailing space, no cards** — 5 of the 6 `Dealt to` lines per hand |
| 345 | `Dealt to Hero [7d Qs 9c 9d]` | cards for Hero only; 2 in Hold'em (167), 4 in Omaha (178) |
| 1652 | `b2c3d4e5: folds` | §4 for the closed verb set |
| 182 | `*** FLOP *** [3s 7s 6c]` | one bracket |
| 119 / 82 | `*** TURN *** [Ac Js 5d] [8d]` | **two brackets**: board so far, then the new card. Same shape for `*** RIVER ***`. |
| 2 / 1 | `*** FIRST FLOP *** [...]`, `*** SECOND TURN *** [..] [..]` | run-it-twice prefixes, §8.3 |
| 232 + 62 | `Uncalled bet ($0.04) returned to c3d4e5f6` | after the last betting line, **before** `shows` / `*** SHOWDOWN ***`. Absent in 51 hands. |
| 91 + 24 | `d4e5f6a7: shows [Jc 8d 7d Ac] (two pair, Jacks and Eights)` | rank parenthetical optional |
| 16 + 4 | `e5f6a7b8: shows [8h 2s As Ad]` | the same line without a rank |
| 288 + 67 | `c3d4e5f6 collected $0.05 from pot` | the **only** authoritative per-player win amount; 8 hands have >1 line |
| 345 | `Total pot $0.12 \| Rake $0 \| Jackpot $0 \| Bingo $0 \| Fortune $0 \| Tax $0` | fixed field order, ` \| ` separators, §5 |
| 182 | `Board [3s 6c 3d Kc Td]` | summary board; only when the hand was not run twice |
| 2 | `Hand was run two times` then `FIRST Board [...]`, `SECOND Board [...]` | §8.3 |
| 2070 | `Seat 6: f6a7b8c9 showed [Ts Ks Kd 6d] and lost with a pair of Kings` | all summary `Seat <i>: ...` lines, 6 per hand — the outcome clause vocabulary is in §4 |

## 4. Action verbs and summary clauses (closed sets, measured)

After `<player>: ` exactly these occur — nothing else:

| Count | Verb | Meaning |
|---|---|---|
| 1652 | `folds` | no money |
| 457 | `checks` | no money |
| 389 | `calls $0.05` | **additional** amount put in, not the running total |
| 374 | `raises $0.04 to $0.06` | `by` amount, then `to` total |
| 345 | `posts small blind $0.01` | pre-`*** HOLE CARDS ***` |
| 345 | `posts big blind $0.02` | idem |
| 227 | `bets $0.15` | amount == total (no prior wager to beat) |
| 115 | `shows [cards] (rank)` | not an action, §6 |
| 20 | `shows [cards]` | idem |
| 4 | `Chooses to EV Cashout` | **not a betting action**, §8.2 |
| 3 | `Pays Cashout Risk ($0.35)` | idem |
| 1 | `Receives Cashout ($1.5)` | idem |

`and is all-in` is a **suffix** on `calls`/`bets`/`raises` (48 lines), e.g.
`0a1b2c3d: raises $0.63 to $0.93 and is all-in`. Match the amounts first, strip the suffix.

Amount semantics, and this is the trap: every money amount on an action line is the **additional**
chips put in on that street, never a stack or a running total. `raises $A to $B` is the only
two-number verb and satisfies `B - A == <to-total being raised against>` (BB $0.02 →
`raises $0.05 to $0.07`). Confirmed by a completed small blind: facing $0.07 after posting $0.01,
the SB line is `calls $0.06`.

Summary per-seat clause inventory (a line is `Seat <i>: <name>[ (<role>)] <clause>[, <clause>]`):
`folded before Flop (didn't bet)` 1041, `folded before Flop` 444, `collected ($N)` 161,
`won ($N)` 122, `folded on the Flop` 86, `folded on the Turn` 53, `folded on the River` 28,
`showed [cards] and won ($N) with <rank>` / `showed [cards] and lost with <rank>` and their
`(button|small blind|big blind)`-prefixed variants, plus the rare appended clauses
`, Cashout Risk ($N)`, `, EV Cashout ($N)`, `, and won ($N) with <rank>`,
`, and lost with <rank>` (§8.2, §8.3). `<role>` is only present for seats 1–3 relative to the
button and is a fact the body lines do not give you — take positions from it.

## 5. Money and the rake split

`Total pot $X | Rake $r | Jackpot $j | Bingo $b | Fortune $f | Tax $t`

Measured across 345 hands:

| Field | hands with non-zero | max |
|---|---|---|
| Rake | 112 | $0.10 |
| Jackpot | 44 | $0.03 |
| Bingo | **0** | $0 |
| Fortune | **0** | $0 |
| Tax | **0** | $0 |

Total deductions $4.57 against $148.24 of pots. In 44 hands `r != r+j+b+f+t`: **Jackpot is a
separate deduction, not part of Rake** — reading only `Rake` understates the house's cut by $0.73
(16%) on this corpus.

**Invariant (345/345 hands, 0 violations):**

```
Total pot == sum(all "collected ... from pot" amounts) + Rake + Jackpot + Bingo + Fortune + Tax
```

Assert it per hand at import. It is also how you notice the §3 `Cash Drop to Pot` money, which is in
the pot but contributed by no player.

## 6. Hole cards, showdown cards, and *when* they appear

- `Dealt to <alias>` appears for all 6 seats but is cardless except for Hero (measured: 2070 `Dealt
  to` lines, exactly 345 carry cards, all Hero). Opponent hole cards are **never** known pre-showdown.
- Card tokens: rank ∈ `2..9 T J Q K A` (**`T` for ten, never `10`**), suit ∈ `s h d c`, lowercase,
  space-separated inside `[...]`. 52 distinct tokens appear; no formatting variants observed.
- `shows` lines are **not** confined to the showdown section. Measured over **62 hands** (135 lines):
  in **8 hands every `shows` line appears before `*** FLOP ***`** (all-in preflop: cards are revealed
  right after the last call, then the board lines follow with no action between), in 43 hands at least
  one appears after the river marker, and in 11 hands they appear only between the flop and the river.
  A parser that only reads `shows` inside `*** SHOWDOWN ***` loses those 8 hands.
- Summary `showed [...] and ...` restates the same cards; redundant with §3.

## 7. Two structural properties that break fpdb's assumptions

### 7.1 Fast fold: the table id changes on literally every hand

Measured: 345 hands, **345 distinct** `RushAndCash<id>` table names, and all 344 consecutive hand
pairs (in time order) differ. Importing naively yields 345 one-hand tables. Map them to one logical
table and keep the raw id elsewhere — see DESIGN.md §3.1.

### 7.2 Player identity is an ephemeral alias

Measured: 1103 distinct aliases over 10 files; only 220 appear in more than one file; the most
frequent appears in 5 files; several file pairs share zero aliases.

Charset: 6–8 characters of `[0-9a-f]` (measured lengths: 8 → 1030 aliases, 7 → 69, 6 → 4, and **29
of them are all digits** — `9000001`, `900002`, `9000003`). Any regex that requires a letter in a
player token silently drops those players; match `(\S+)` and validate separately. Local player is
`Hero`, which is the only non-hex seat name in the corpus.

## 8. GGPoker-only features

### 8.1 Side pots without side-pot markup
8 hands contain >1 `collected ... from pot` line; sometimes the same player twice (`1b2c3d4e
collected $1.54` then `$3.07`). Order is main pot first. There is no `Side pot` line. Call
`hand.collectPot()` once per line.

**Cross-check (344/345 hands):** per player, `sum(summary won/collected amounts) == sum(body
collected)`. The one exception is the EV-cashout hand of §8.2, whose summary says `won ($0)` while
the body says `collected $2.13`.

### 8.2 EV Cashout (a player settles the hand at equity)
Three verbs (§4) plus two summary suffixes: `... and won ($2.78) with a flush Queen high, Cashout
Risk ($0.35)` and `... and won ($0) with two pair, Aces and Sixes, EV Cashout ($3.63)`. 3 hands in
the corpus (`RC900000002`, which has two `Chooses to EV Cashout` lines, plus 2 Hold'em hands). These
are **not** betting actions — they change no wager total — but the cashed-out
player stops acting for the rest of the hand. Implemented: they stay out of `HandsActions` and the
amounts go to `Hands.comment` as `choseEvCashout`, `paidCashoutRisk=$n`, `receivedCashout=$n`; §5
still holds in these hands.

### 8.3 Run It Twice
2 hands. Markers become `*** FIRST FLOP ***`, `*** FIRST TURN ***`, `*** FIRST RIVER ***`,
`*** SECOND RIVER ***`, `*** FIRST SHOWDOWN ***`, `*** SECOND SHOWDOWN ***`; the summary gains
`Hand was run two times` + `FIRST Board [...]` + `SECOND Board [...]`, and per-seat lines gain a
second clause (`... and won ($0.96) with X, and lost with Y`).

The two runs are **not** symmetric, and the reason is the street the betting ended on:
`RC900000003` was all-in **on the flop**, so run 2 gets its own `*** SECOND TURN *** [8h Td 3d] [Kh]`
plus `SECOND RIVER`; `RC900000004` was all-in **on the turn**, so run 2 has only
`*** SECOND RIVER *** [Tc Js Ts 6s] [As]` and shares run 1's flop and turn. Each
`*** <FIRST|SECOND> STREET *** [board] [new]` line repeats the *shared* prefix in its first bracket,
not that run's running board. The summary then lists only the second run's **new** cards —
`SECOND Board [As]` (1 card) and `SECOND Board [Kh 8s]` (2) — while `FIRST Board [...]` is always the
full 5. So expect a second board of any length 1..5.

Implemented: `readRunItTwice` builds run 2 as *that shared prefix + the new cards*, writes
`hand.board['FLOP1'..'RIVER1']` and `['FLOP2'..'RIVER2']`, and sets `hand.runItTimes = 2`.
`DerivedStats.py:177-189` turns those into `hands['boards']` and `Hand.py:295` inserts them, giving
**two `Boards` rows per run-it-twice hand** (`boardId` 1 and 2, five cards each — measured: run 1
`Tc Js Ts 6s 3c`, run 2 `Tc Js Ts 6s As` for `RC900000004`). `Hands.boardcard1..5` holds run 1 only;
run 2 exists nowhere else in the schema. `TestGgImport.py` therefore asserts, for both runs, that
the `Boards` row's five card ids **are** the cards on that run's deepest
`*** <FIRST|SECOND> STREET ***` line — an independent derivation, since the parser reads run 1 from
`FIRST Board` and run 2 from `SECOND Board` plus the first marker's prefix. Changing one card of a
`SECOND RIVER` line in a scratch copy makes that check fail with exit 1. The row-comparison harness
cannot see this: it deletes `boards` before comparing (§DESIGN.md 6).

## 9. Absent from the corpus — fail loudly, do not guess

Raise (do not silently mis-import) on: heads-up (`1-max`) Rush & Cash, tournaments
(`Poker Hand #... Tournament ...`, buy-in/fee lines), ante games, `All-In or Fold` brings-in, mixed
games (PLO5, Big Open, Crazy House), non-`$` currencies, `Mucked` (**UNSEEN** anywhere here),
`Side pot` lines, more than one board bracket per street beyond the two-bracket shape, or a
`Dealt to <non-Hero> [cards]` line — that last one would mean §6's "only Hero is dealt" is no longer
true, i.e. the format changed and needs a new test, not a silent parse.

## 10. Mapping to fpdb `[IMPLEMENTED in pyfpdb/GGPokerToFpdb.py, 2026-10-13]`

These are the calls the shipped parser makes, not proposals. fpdb's own conventions win where this
table and intuition disagree: seats and the button position stay **1-based** (that is what
`PokerStarsToFpdb.py` passes and what `Hand.py:977` prints), and Hero lives in `hand.hero`.

| Hand-history line | fpdb call (as implemented) |
|---|---|
| 1 | `determineGameType` + `readHandInfo` → `hand.startTime`, `hand.handid` (digits after `RC`), stakes, currency |
| 2 | `readHandInfo` → `hand.maxseats`, `hand.buttonpos` = `Seat #n` as written, raw table id into `hand.comment`, `hand.rush = True`, `hand.tablename` normalised per (game, stakes) |
| 3 | `readPlayerStacks` → `hand.addPlayer(seat, name, chips)` with the seat number as printed |
| 4 | `readBlinds` → `hand.pot.common['CashDrop'] += amount` (money no player contributed, so `hand.totalpot` equals the site's `Total pot` and §5 is provable) + note in `hand.comment` |
| 5 | `readBlinds` → `hand.addBlind` for SB/BB; raises if either line is missing |
| 6,7 | `readHeroCards` → `hand.hero = 'Hero'`, `hand.addHoleCards('PREFLOP', hero, closed=cards, dealt=True)`; raises if a non-Hero line carries cards |
| 8–11 | `markStreets` (one `re_Sections` match fills PREFLOP/FLOP/TURN/RIVER) + `readCommunityCards` + `readAction` → `addFold/addCheck/addCall/addBet/addRaiseBy`; `addRaiseBy` is followed by an assertion that `hand.lastBet[street]` landed on the printed `to $B` |
| 12 | **not parsed.** `Pot.end()` (`Hand.py:1820+`) already returns the uncalled bet from the committed amounts; parsing it would double-count. 294 of 345 hands have such a line and §5 holds on all of them |
| 13,14 | `readShowdownActions` scans `shows` over the **whole hand** (8 hands show before the flop marker) → `hand.addShownCards` |
| 15 | `readCollectPot` → one `hand.addCollectPot` per line, in file order (main pot first) |
| 16 | `getRake` → `hand.rake = Rake+Jackpot+Bingo+Fortune+Tax`, asserting §5 and `Total pot == hand.totalpot` before accepting the hand; components kept in `hand.comment` |
| 17–19 | `readShownCards` (summary `showed` lines carry the hand rank), `readOther` (per-player summary-vs-body money cross-check, noted not raised), `readRunItTwice` → `hand.board['FLOP1'..'RIVER2']` + `hand.runItTimes = 2`, which is what `DerivedStats.py:177` reads |
