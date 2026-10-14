# OpenPokerDB — Revival Design Document

Status: **living document, written 2026-10-12.** Sections are marked `[DONE]`, `[IN PROGRESS]`, or
`[PLANNED]`. Anything not verified by running code is labelled as a plan, not as fact.

This doc exists so that a future reader (human or a cheaper model) can debug and extend this
project from the doc alone, without spelunking 50k lines of 2012-era Python. Every number here was
measured by running code over this repo or over the GGPoker corpus; if you change the code, change
the number.

---

## 1. What the project is

`OpenPokerDB` is this fork's name for a hand-history importer, statistics database, and live
on-table HUD for online poker. It revives `fpdb` ("Free Poker Database"); upstream is
`https://github.com/pikurino/openPokerDB`, cloned here as the working tree.

**Naming, so this doc stays readable.** Only user-visible text was rebranded. Where this doc says
*fpdb* it means the upstream engine and the names it still uses in the tree: the `pyfpdb/` package,
the `*ToFpdb.py` site converters, `FpdbParseError`, `HUD_config.xml`,
`%APPDATA%\fpdb\database\fpdb.db3`. Those were left alone on purpose -- they are what existing
databases, config files and the regression harness point at, and renaming them buys a user nothing.
So a sentence like "fpdb stores an unknown card as 0" is a statement about that code, not about what
this project is called.

**Measured repo state:**

| Fact | Value |
|---|---|
| Commits | 7,556 |
| First / last commit | 2008-08-04 / 2012-12-15 |
| Active years | 2008 (622), 2009 (1949), 2010 (2160), 2011 (1999), 2012 (826) |
| Python sources | 113 `.py` + 2 `.pyw`, 49,657 lines |
| License | AGPL-3.0 (60 engine files carry the Affero header) |
| Existing design docs | none upstream; `readme.txt` is now this fork's, and points here |
| Version in `setup.py` / `Makefile` | 0.27 / 0.30 (inconsistent) |

**Revival goals (agreed 2026-10-12):** drop Linux/macOS, document the design, import GGPoker Rush &
Cash hands into the database, and replace the GUI.

**Locked decisions:**

1. GUI framework = **PySide6 (Qt)**. The old GUI is PyGTK/GTK2 and cannot be ported (see §5).
2. Near-term product = **offline database + statistics**. The live table overlay comes later.
3. All 19 legacy site parsers get ported to Python 3, not just GGPoker.
4. Python target = **3.12** (installed here via uv at `%USERPROFILE%\AppData\Roaming\uv\python\
   cpython-3.12.14-...`). Python 3.14 is the system default but has no GUI/pywin32 wheels yet.

---

## 2. Architecture `[verified by reading code]`

The pipeline is strictly one-directional — with **one leak**: `Importer.py:33` does `import gtk`
unconditionally, and `Importer.progressNotify()` (554-557) pumps the GTK main loop while
`class ProgressBar` (559-656) builds a GTK window. Everything else (`Hand`, `HandHistory`, `Database`,
`SQL`, `DerivedStats`, `Card`, `Configuration`, `Options`, `IdentifySite`, `HandHistoryConverter`)
imports no GUI. The port must break that leak first: `Importer` takes an optional progress callback
and keeps only the counters, so the engine can run headless under a PySide6 front end.

```
files on disk
  -> IdentifySite.idSite()              sniff first 5000 chars against regex table
  -> Importer (thread)                  queue of file paths, per-site config
  -> <Site>ToFPDB.processHand()         subclass of HandHistoryConverter
       -> Hand (Hand.py)                in-memory model: players, streets, actions, pots
  -> HandHistory/Database.storeHand()   row assembly + INSERT
  -> SQL.py                             hand-written DDL, 3 dialects
  -> DerivedStats.py                    computes per-hand/per-player stat columns
  -> cache tables (HudCache/SessionsCache/GamesCache) for the HUD and stat views
```

### 2.1 The site-parser contract (most important abstraction)

`pyfpdb/HandHistoryConverter.py` is the abstract base. A site supplies:

| Method | Purpose |
|---|---|
`re_SplitHands` (class attr) | regex that splits one file into individual hands
`determineGameType(handText)` | returns dict `{type, base, category, limitType, hilo, smallBlind, bigBlind, currency}` or None
`readHandInfo(hand)` | startTime, handid, tablename, buttonpos, maxseats, tourNo, buyin, fee
`readPlayerStacks(hand)` | `hand.addPlayer(seat, name, chips)` per seat
`markStreets(hand)` | assigns action lines to street buckets (PREFLOP/FLOP/TURN/RIVER)
`readBlinds / readAntes / readBringIn / readButton` | forced bets
`readHeroCards / readPlayerCards` | hole cards
`readAction(hand, street)` | calls `hand.addFold/addCheck/addCall/addRaiseBy/addBet`
`readCollectPot` | winnings per player
`readShownCards` | showdown cards + descriptive hand strength
`readSummaryInfo` | anything site-specific left over
`getRake` | default = totalPot − totalCollected; raises `FpdbParseError` if negative or > 25%

Failure signalling, and the distinction matters when debugging:
- `FpdbParseError` — the whole file is bad; counted per file.
- `FpdbHandPartial` — this hand is truncated/unparseable; skipped, file continues.
- Unsupported game type → logged then `FpdbParseError` (`HandHistoryConverter.py:212-220`).

Typical parser size: 214 lines (Betfair) to 999 (Merge); modern-style sites sit at 500–650
(`PokerStarsToFpdb.py` is 546 and is the best reference implementation).

### 2.2 Registration points a new site must touch `[DONE for GGPoker 2026-10-13]`

1. `pyfpdb/GGPokerToFpdb.py` — the converter itself. **[done]**
2. `IdentifySite.py` `re_identify` dict — content-sniff regex → site name:
   `re_identify['GGPoker'] = re.compile(r'^Poker Hand \#RC\d+:', re.MULTILINE)`. **[done]** —
   it is a `MULTILINE` search over the first 5000 chars, because a file's first line is not
   necessarily a hand header.
3. `HUD_config.xml.example` + `pyfpdb/HUD_config.test.xml` — `<hhc site="GGPoker"
   converter="GGPokerToFpdb"/>` added to both. **[done]** No `<site>` HUD-layout block: GG has no
   live overlay yet (§7), and `generateSiteList` builds the site list from the `Sites` table plus the
   `<hhc>` entries, not from `<supported_sites>`.
4. `Database.py` `fillDefaultData` — `INSERT INTO Sites (id,name,code) VALUES ('21','GGPoker','GG')`,
   and `DB_VERSION` bumped **171 → 172** because the GG import is the first to write `Hands.rush` and
   `Hands.comment` (an existing DB is refused with "Please recreate tables", which is intended).
   **[done]**
5. `Options.py:86` `site_alias` — `"GG"` and `"GGPoker"` → `GGPoker`. **[done]**
6. `DetectInstalledSites.py` — Windows hand-history folder detection. **[not done]** The GG folder is
   `%USERPROFILE%\Documents\Projects\Poker\history\games` on this machine; auto-detection only matters
   for the live-HUD path.
7. `TestHandsPlayers.py` — `GGPokerErrors`, `sites['GGPoker']`, and a `walk_testfiles` block over
   `regression-test-files/cash/GGPoker/`. **[done]** No `tour/` block: the corpus has no tournament
   hands (§3.7, doc §9). That directory is **git-ignored** (§6): the walk skips it with a printed
   note when the local corpus has not been copied in, so a clone without GG hands still runs the
   rest of the suite instead of dying on a missing directory.

Missing any of 2–4 gives confusing failures: an unregistered site silently never gets imported,
and a missing `Sites` row makes `get_site_id` blow up deep inside `Hand.prepInsert`. Measured, and
worth knowing when reading a stack trace: the symptom of an unpopulated `config.site_ids` is
`KeyError: 'PokerStars'` at `Hand.py:62` — `HandHistoryConverter.__init__`'s `sitename` **parameter**
defaults to `"PokerStars"` and overwrites the subclass's class attribute, so any driver must pass
`sitename='GGPoker'` explicitly.

### 2.3 Schema relevant to GGPoker `[rush/comment now written]`

Dialect duplication is the trap: **every schema change must be applied three times** in `SQL.py` —
sqlite (the one we use), MySQL, PostgreSQL. Hands for example is created at `SQL.py:363`, `:403`,
`:442`.

Columns that matter for GG:

- `Hands`: `id, tableName, siteHandNo, gametypeId, sessionId, fileId, startTime, seats,`
  **`rush`** (BOOLEAN — no 2010-era parser wrote it; GG now sets it on all 345 hands), `boardcard1..5`,
  `texture`, `runItTwice`, `playersVpi`, `playersAtStreet1..4`, `street0Raises..street4Raises`,
  **`comment`** (VARCHAR/TEXT — GG writes `rushTable=<raw id>; rake=$a jackpot=$b bingo=$c fortune=$d
  tax=$e`, plus `cashDrop=$n`, `choseEvCashout`, `paidCashoutRisk=$n`, `receivedCashout=$n` and any
  summary/body money disagreement).
  Both keys are **only added to `DerivedStats.getHands()` when the parser set them**
  (`DerivedStats.py:155-161`, read with `hdata.get(...)` in `Database.storeHand`). Measured reason:
  writing them unconditionally made every legacy fixture raise `KeyError: 'rush'` and
  `KeyError: 'comment'` in `compare_hands_file`, because their shipped sidecars predate the columns —
  **78 new errors over 39 files, total 467 → 389 after the fix**. A schema addition must not change
  what a *dict* contains for parsers that do not use it.
- `HandsPlayers`: `handId, playerId, startCash, position, seatNo, card1..card20, startCards, ante,`
  **`winnings`, `rake`, `totalProfit`, `allInEV`**, `wonWhenSeenStreet1..4`, `comment`.
- `HandsActions`: `handId, playerId, street, actionNo, streetActionNo, actionId, amount, raiseTo,
  amountCalled, allIn` — `actionId` joins to the `Actions` seed table.
- `Gametypes`: `siteId, currency, type, base, category, limitType, hiLo, mix, smallBlind, bigBlind,
  maxSeats, ante`.
- `Sites`: `id, name, code`.

`SQL.py`'s single shared `store_hand` INSERT listed 33 columns; it now lists **35** (`rush`,
`comment` appended after `showdownPot`), with matching entries in `Database.py:1893` `self.hbulk` and
in `DerivedStats.assembleHands`. `commentTs` stays NULL: it belongs to the manual user-comment
feature, not to the parser.

`rake` is stored **per player**, derived in `DerivedStats.py:244` as `int(100*hand.rake)/len(collectees)`
— so a hand's rake is split evenly among its winners and the cents are truncated. Measured on the GG
corpus: `sum(HandsPlayers.winnings)` = $143.67 and `sum(...rake)` = $4.57 (exact), but per hand
`sum(totalProfit)` can be a cent or two off `-rake`; that is fpdb's own truncation, not a GG parse bug.

---

## 3. GGPoker corpus — measured facts `[DONE 2026-10-13]`

Corpus: `%USERPROFILE%\Documents\Projects\Poker\history\games\*.txt`, 10 files, 345 hands, 6-max
Rush & Cash, `$0.01/$0.02`, 167 Hold'em No Limit + 178 Omaha Pot Limit. 2070 seat lines, 7897 money
tokens, largest amount anywhere `$29.96`.

The exhaustive line-by-line grammar lives in **`docs/gg-hand-history-format.md`**, and
`tools/gg_format_probe.py` regenerates every number in it. Read the format doc before writing
regexes; do not guess from the samples in this file.

Six properties of this site break assumptions fpdb made in 2010.

### 3.1 Fast-fold: the table name changes on every hand

Measured: 345 hands → **345 distinct** `RushAndCash<id>` table names, and all 344 consecutive hand
pairs (in time order) differ. The id in the filename matches at most one hand in that file. fpdb
keys `Hands.tableName` and the whole HUD/GUI table concept on a persistent table.

Consequence if we do nothing: 345 distinct tables in the DB, every one with 1 hand. Any per-table
reporting becomes useless and the `Tables`/`GamesCache` path bloats.

**Decision, implemented 2026-10-13:** normalise fast-fold tables to one logical table per
**(site, game, stakes)** — `RushAndCash-<category>-<limit>-<sb>/<bb>` — which gives **2** table names
over this corpus (`RushAndCash-holdem-nl-0.01/0.02` 167 hands, `RushAndCash-omahahi-pl-0.01/0.02`
178), not 1: the corpus is two games at one stake. Keeping the game in the name is deliberate, because
fpdb's own table reports group by `tableName` and mixing Hold'em and Omaha hand counts into one row is
worse than two rows. The real per-hand id stays in `Hands.comment`, and `Hands.rush = True` on all 345
(`rush BOOLEAN` already existed in all three dialects in `SQL.py` and was written by no parser — GG is
the first).

### 3.2 Player aliases are ephemeral

Measured over the whole corpus: **1,103 unique aliases, only 220 of which appear in more than one
file**; the most frequent appears in 5 files, and several file pairs share exactly **0**. Aliases are
6–8 chars of `[0-9a-f]` (lengths: 1030 × 8, 69 × 7, 4 × 6), and **29 are all digits** (`9467263`,
`178665`) — so never write a player regex that requires a letter. The local player is always the
literal `Hero`, the only non-hex seat name present.

Consequence: `Players.name` is fpdb's identity key, so on GGPoker it silently produces a stream of
throwaway players. Long-lived per-opponent HUD stats keyed on name are **not achievable from these
files alone.** Options: accept per-session identity, or find a stable id source (GG seat/history
export with real ids). Do not pretend names are stable. `[OPEN QUESTION — biggest product risk]`

### 3.3 Rake is split five ways, and Jackpot is not part of Rake

`Total pot $0.12 | Rake $0 | Jackpot $0.03 | Bingo $0 | Fortune $0 | Tax $0` — present in all 345
hands. Measured: Rake non-zero in 112 hands (max $0.10), Jackpot non-zero in **44** hands (max
$0.03), Bingo/Fortune/Tax non-zero in **0**. Deductions total $4.57 against $148.24 of pots, so
reading only `Rake` understates the cut by $0.73 (16%) here. fpdb has one `rake` number per player.

**Hard invariant, verified on 345/345 hands with 0 violations:**

```
Total pot == sum(all "collected ... from pot") + Rake + Jackpot + Bingo + Fortune + Tax
```

Use it as the per-hand acceptance test. It is also the only way to catch `Cash Drop to Pot` money
(4 hands), which is in the pot but contributed by no player.

**Decision, implemented 2026-10-13:** `getRake()` sets `hand.rake` to the sum of all five fields so
`totalProfit` stays correct, asserts the invariant above per hand (and that the site's `Total pot`
equals fpdb's own `hand.totalpot`, which is what catches unparsed money), and writes the breakdown
machine-readably into `Hands.comment` as `rake=$a jackpot=$b bingo=$c fortune=$d tax=$e`. Measured in
the imported DB: `sum(HandsPlayers.winnings)` = **$143.67**, `sum(HandsPlayers.rake)` = **$4.57**,
against `sum(Hands.street …)`-equivalent pots of **$148.24** — exactly the corpus totals.

### 3.4 Only Hero's cards are dealt

`Dealt to <alias>` exists for every seat (2070 lines) but carries cards on exactly 345 of them, all
Hero (2 cards in Hold'em, 4 in Omaha). Opponent ranges come solely from `shows [...]`: **62 hands**
contain such lines (135 lines; 8 hands reveal before the flop marker, 43 after the river marker, 11 in
between). Any stat that assumes known opponent cards is invalid.

Measured in the imported DB, and the trap that goes with it: `card1 > 0` is true on 452 of the 2070
`HandsPlayers` rows — 345 Hero (of which 178 also have `card4 > 0`, the Omaha hands) and 107 opponent
rows from those `shows` lines. fpdb stores an unknown card as **`0`, not NULL**, so `card1 IS NOT NULL`
selects every row and looks like the parser leaked opponents' hole cards. Query `> 0`.

### 3.5 Files are newest-first and the header clock is not the filename clock

Measured: every file is strictly **descending** by header timestamp and by hand id, so `sort by hand
id` rebuilds chronology — never trust file order. Header format is `YYYY/MM/DD HH:MM:SS` (solidus;
verified byte-wise — a `\d{4}-\d{2}-\d{2}` regex matches nothing in this corpus). Filename clock =
oldest hand's header clock + **6 h** (measured 359.1–360.0 min across the 10 files; the 0.9 min
shortfall is the filename's truncation to the minute). So header times are one fixed zone and the
filename is local UTC+6. **Import the header time and treat the offset as data, not as a bug**;
`HandHistoryConverter.changeTimezone` is the hook if the user wants local display.

### 3.6 Cards are shown before the board exists, and streets can be empty `[IMPLEMENTED]`

Measured over the 345-hand corpus: **62 hands contain `shows` lines, 135 lines in total**, and they
are not confined to one region — **8 hands show every card before `*** FLOP ***`** (all-in preflop:
reveal, then the board lines follow with no action between), **43 hands show only after the river
marker**, and **11 show in between**; by line count that is 20 / 24 / 91. No hand has a `shows` line
in more than one region. A parser that only reads `shows` inside `*** SHOWDOWN ***` would lose those
8 hands' card data entirely, so `GGPokerToFpdb.readShowdownActions` scans `hand.handText`.

`Uncalled bet ($N) returned to X` sits after the last betting line and before the `shows` lines; it
is present in **294 hands and absent in 51**. It needs no parsing: `Pot.end()` already returns the
uncalled bet, and the §3.3 identity holds on all 345 hands including those 294.

TURN and RIVER markers always carry two brackets (`[board so far] [new card]`) — **122 TURN + 86
RIVER = 208 lines, 208/208 two-bracket** — which matches fpdb's own re-serialization in
`Hand.py:1233-1240`, so the parser calls `setCommunityCards(street, new_cards_only)`.

### 3.7 Run It Twice and EV Cashout are in the format and now in the database `[IMPLEMENTED]`

**2 hands are run twice**, and which street they were all-in on decides the shape:

- `RC900000003` — all-in on the **flop**: markers `FIRST FLOP/TURN/RIVER` **and** `SECOND TURN` +
  `SECOND RIVER`. The second run shares the flop and gets its own turn and river.
- `RC900000004` — all-in on the **turn**: markers `FIRST FLOP/TURN/RIVER` and **`SECOND RIVER` only**.
  Its `[board so far]` bracket repeats the first run's four cards.

So the two runs are not symmetric, and a run's cards appear twice — once on its street markers, once
in the summary: `FIRST Board [...]` is always 5 cards, while `SECOND Board [...]` carries only that
run's *new* cards (2 in the first hand, **1** in the second).
`GGPokerToFpdb.readRunItTwice` reconstructs
run 2 as *the shared prefix from the first `SECOND` street marker's bracket* plus those new cards,
writes `hand.board['FLOP1'..'RIVER1']` / `['FLOP2'..'RIVER2']` and sets `hand.runItTimes = 2`, which
is what `DerivedStats.py:177-189` needs: it builds `hands['boards']` from `street + str(i+1)` keys,
and `Hand.py:295` inserts them through `Database.storeBoards`. Measured in the imported database:
**2 hands with `runItTwice = 1` and 4 rows in `Boards`**, run 1 = `Tc Js Ts 6s 3c` / run 2 =
`Tc Js Ts 6s As` for `RC900000004`. `Hands.boardcard1..5` can only hold one board, so it holds
run 1's; run 2 lives in `Boards` and nowhere else.

No parser before GG set `runItTimes`, so this path was unexercised; the fixture sidecars do not
cover the cards either (`TestHandsPlayers.py:175` deletes `boards` from the compared dict). Both
boards are therefore verified at the database level: `TestGgImport.py` re-derives each run's five
cards from that run's deepest `*** FIRST|SECOND <street> *** [prefix] [new]` line and compares them
to the stored `Boards` row — a different derivation from the parser's, so a pass means the summary
`Board` lines and the street markers say the same cards. §6 records the sabotage that proves it.

**3 hands use EV Cashout** (`RC900000002`, `RC900000005`, `RC900000006`): the non-betting verbs
`Chooses to EV Cashout` (4 lines across those 3 hands), `Pays Cashout Risk ($N)` (3),
`Receives Cashout ($N)` (1), plus summary suffixes `, Cashout Risk ($N)` (3) and
`, EV Cashout ($N)` (1). They change no wager total but the player stops acting afterwards, so the
parser keeps them out of `HandsActions` and writes them into `Hands.comment`. Cross-check: per
player, summary winnings == body `collected` in **344/345** hands; the one exception is an
EV-cashout hand whose summary says `won ($0)` while the body says `collected $2.13` — so trust the
body lines for money and keep the cashout amounts as commentary.


---

## 4. Removal of Linux/macOS `[DONE 2026-10-12]`

Deleted: `pyfpdb/XTables.py` (wnck/gtk X11 window finder), `pyfpdb/OSXTables.py` (Quartz),
`pyfpdb/xlib_tester.py`, `pyfpdb/Tables_Demo.py`, `Makefile` (tarball snapshots),
`files/fpdb.desktop`, `packaging/debian/`, `packaging/gentoo/`, `utils/create-release.sh`,
`pyfpdb/locale/localise.sh`, `pyfpdb/locale/test-all-langs.sh`,
`pyfpdb/test-old-config-files/test-old-configs.sh`. 17 files, −1,293 lines.

Edited (11 files): `HUD_main.pyw` now does an unconditional `import WinTables as Tables`
(was a 3-way `os_family` branch); `Configuration.py` collapsed `OS_FAMILY` to `'Win7'`, deleted
`POSIX`, the `~/.fpdb` and `/usr/share/python-fpdb` example-copy path, and the `os.uname()` guard;
`DetectInstalledSites.py` lost its four `~/.wine` branches and its `winpaths` dependency
(`%APPDATA%`/`%PROGRAMFILES%`/`%LOCALAPPDATA%` env vars now); `Importer.py`, `GuiTourneyImport.py`,
`GuiAutoImport.py`, `GuiBulkImport.py`, `fpdb.pyw`, `Database.py`, `fpdb_prerun.py`, `run_fpdb.py`
lost their posix/Linux/mac branches.

Verification: `grep -rn "os.name\|sys.platform\|/usr/share\|\.wine\|winpaths\|XTables" pyfpdb/*.py
pyfpdb/*.pyw` returns only `windows_make_bats.py` (a Windows-only helper, by design), an
`interlocks.py` doctest, and usage strings in `RushNotesMerge.py` (a dead Full-Tilt side tool).

**Not verified by execution.** Nothing in this repo runs yet — it is still Python 2 (§5). Treat
these edits as reviewed-by-reading until the port proves them.

---

## 5. Python 2 → 3: why the GUI must be replaced `[ENGINE PORTED 2026-10-13]`

Two independent walls:

1. **Syntax/runtime.** The code is Python 2 only.
2. **GUI toolkit.** The entire GUI and HUD import `pygtk` + `gtk` (`import pygtk;
   pygtk.require('2.0')`) across **38 files**, plus `gobject`, `pango`, `cairo`. PyGTK was never
   ported to Python 3 and there is no GTK2 binding that installs on modern Python. "Modernise the
   GUI" therefore means "rewrite the GUI", which is why PySide6 was chosen.

Census of Python-2 idioms, measured over the **engine closure only** (the 15 modules an import-only
pipeline actually loads — `Card, Charset, Configuration, Database, DerivedStats, Exceptions, Hand,
HandHistory, HandHistoryConverter, IdentifySite, Importer, L10n, Options, SQL, decimal_wrapper`):

| Idiom | Occurrences | Files |
|---|---|---|
| `print` statement | 208 | 8 |
| `print >> stream` | 85 | 1 |
| `.iteritems/.itervalues/.iterkeys` | 28 | 7 |
| `.has_key()` | 15 | 2 |
| `unicode()` / `basestring` | 14 | 5 |
| `except Exception, e` | 8 | 4 |
| `xrange` | 4 | 3 |
| `import Queue` | 2 | 2 |
| `<>` operator | 1 | 1 |
| `map(...)` without `list()` | 2 | 2 |

Whole-repo, 78 files contain `print "` statements and 45 contain `has_key/xrange/iteritems/
except-,e/urllib2/ConfigParser`.

Size by layer (measured):

| Layer | Files | Lines | Plan |
|---|---|---|---|
| Engine closure | 15 | 17,615 | port now — this is what makes importing work |
| Site parsers | 19 | 9,566 | port after the engine, site by site, gated by regression tests |
| `Gui*` + HUD (PyGTK) | 29 | ~6,838+ | **do not port**; replace with PySide6 |

Other dead dependencies removed or to remove: `winpaths` (gone), `win32console` (no longer ships in
pywin32 — used by `GuiAutoImport.py`), `distutils` (`setup.py` — removed from the stdlib in 3.12,
and it also generates `pyfpdb/card_path.py`, which `Deck.py` needs, so replacing it is mandatory,
not cosmetic), `pylab`/`matplotlib` (`fpdb_prerun.py` dependency probe), `ConfigParser`, `Tkinter`.

### 5.1 Port order

1. Create venv on Python 3.12; `pyproject.toml` replaces `setup.py`; SQLite only.
2. Port the engine closure until `TestHandsPlayers.py` can construct a database and import one
   PokerStars cash hand. That harness — not the GUI — is the port's verification gate.
3. Add `docs/gg-hand-history-format.md`-driven `GGPokerToFpdb.py`, register it (§2.2), import the
   10 corpus files, and assert measured row counts (§6).
4. Port remaining site parsers, one per regression run.
5. Build the PySide6 GUI over the same `Database`/`DerivedStats` API.

### 5.2 Port status `[DONE 2026-10-13, re-measured after the GGPoker work]`

The 15-module engine closure and all 19 site converters import and run under Python 3.12 with zero
`SyntaxWarning`s, and `TestHandsPlayers.py` now walks the whole corpus without crashing. Measured by
a full `TestHandsPlayers.py` run (no `-s`), **GGPoker included and at 0 errors**; the two columns are
files that produced at least one report and the number of reports:

| Site | files | errors | | Site | files | errors |
|---|---|---|---|---|---|---|
| Full Tilt Poker | 17 | 66 | | Merge | 12 | 55 |
| PokerStars | 8 | 43 | | Boss | 4 | 37 |
| PartyPoker | 23 | 23 | | Winamax | 4 | 31 |
| Cake | 4 | 23 | | Everest | 2 | 29 |
| OnGame | 3 | 24 | | iPoker | 2 | 17 |
| Everleaf | 2 | 11 | | Microgaming | 6 | 11 |
| PKR | 3 | 9 | | BetOnline | 5 | 5 |
| PacificPoker | 3 | 4 | | Absolute | 1 | 3 |
| **GGPoker** | **0** | **0** | | | | |

**Total 389 errors over 98 files, of which 49 are `Parse`** (a hand raised
`FpdbHandPartial`/`FpdbParseError`); the rest are derived-stat mismatches. Three families in there
are *not* port regressions:

- `allInEV`, `street0CalledRaiseChance`, `street0CalledRaiseDone` — 36 of PokerStars' 43. At
  `TestHandsPlayers.py:144` the shipped `ignorelist` is immediately followed by a comment naming
  exactly those three stats: upstream had stopped asserting them before the repo died.
- `Parse` on the two fixtures literally named `...Hand-cancelled.txt`: `readHandInfo` raises
  `FpdbHandPartial` for a cancelled hand (correct), and the harness scores `partial > 0` as an error.
  Neither fixture ships a `.hp`/`.hands`/`.gt` expectation, so there is nothing to compare.
- `KeyError: 'rush'` / `'comment'` — **these were mine** (78 of 467, over 39 files) and are fixed in
  §2.3. Everything else in the table was already disagreeing before the revival touched the file.

`TestHandsPlayers.py:184-197` also used to exempt `runItTwice` from comparison. Measured with a
read-only probe over **every `.hands` sidecar in the tree** — all 53 of them, 39 legacy + 14 GGPoker,
one hand each, and all 53 imported with 0 errors and 0 partials, so none was skipped:
**no hand disagrees with its own expected `runItTwice`, and no sidecar lacks the key** — so the
exemption was hiding only the one parser that writes it. It is removed, the full run is unchanged at
389, and mutating a GGPoker sidecar's `'runItTwice': True` to `False` now reports
`Total Errors: 1` naming that file (`Stat: runItTwice`, `Hand: 925265777`, `Got: True / Expected:
False`) where before the same mutation reported 0.

### 5.3 Port traps that changed behaviour silently `[measured]`

Every one of these compiled and ran; only the corpus numbers exposed them. This is why the gate is a
regression run and not `python -m compileall`.

1. **A currency-character class must keep its empty alternative.** `PokerStarsToFpdb`'s
   `substitutions['CUR']` is `(\$|€|£|)` — the trailing empty branch is what lets a chip-denominated
   tournament line (`Player2: calls 60`) match. Drop it and every bet/call/raise stops being read:
   street pots, `playersVpi` and `streetXRaises` go to 0 with no exception anywhere.
2. **`for key in info` loops became order-sensitive.** `readHandInfo` merges two `groupdict()`s and
   branches on the key. Python 3 iterates in regex-group order, so `TABLE` was handled before
   `TOURNO` was known, and the tournament id got prefixed twice (`492967531 492967531 1`). Fixed by
   consulting `info['TOURNO']` rather than depending on `hand.tourNo` already being set.
3. **Ordering a `str` against an `int` now raises instead of silently comparing by type name.**
   Found in `HandHistoryConverter.maxOccSeat`, `PartyPokerToFpdb.readPlayerStacks` and
   `WinamaxToFpdb.readCollectPot`; all three normalise at the boundary now.
4. **`Configuration.get_config` aborted on a missing `.example` even when the real config was
   found.** Its search order is `FPDB_ROOT_PATH/pyfpdb/<file>` then `%APPDATA%\fpdb/<file>`, but the
   `.example` fallback is looked for relative to the *current directory*, so launching the HUD from a
   Desktop shortcut (arbitrary CWD) exited with "No HUD_config.xml found, cannot fall back" despite a
   valid config in `CONFIG_PATH`. It is now fatal only when no config was found at all — which the
   double-clickable launcher (§7) depends on.

Also removed during the port, because a headless importer cannot depend on them: `import gtk` and the
GTK-pumping `progressNotify()` in `Importer.py` (replaced by a `ProgressBar` value object plus
`setProgressCallback`), `str(_(… ), "utf8")` decode hacks, `string.upper`, `time.clock`, and
`import exceptions`/`import thread`. `Configuration.set_logfile` now forces every stderr handler to
UTF-8 with `backslashreplace`, because hand histories legitimately contain Cyrillic table names and
the Windows console default (cp1252) raised `UnicodeEncodeError` *inside* `logging`.

---

## 6. Testing and regeneration `[GG green 2026-10-13; legacy suite at 389 known errors]`

- Fixture tree **tracked by git** (measured with `git ls-files pyfpdb/regression-test-files`):
  **405 `.txt` + 15 `.xml`**, 8.6 MB — `cash/` 253 txt + 12 xml, `tour/` 102 + 3, `summaries/` 42,
  `unsupported-sites/` 8.
- **GGPoker's fixtures are deliberately not committed.** A hand history is private play data: it
  names the opponents at his tables, their stack sizes and his own line on every street. The 14
  single-hand fixtures with their `.gt`/`.hands`/`.hp` sidecars and the 10 original session files
  (345 hands) live only under `pyfpdb/regression-test-files/cash/GGPoker/`, which `.gitignore`
  excludes, plus the untouched corpus at `%USERPROFILE%\Documents\Projects\Poker\history\games`.
  Anyone wanting GG coverage copies their own histories into that directory; nothing in the repo
  can then leak a hand. The same applies to new fixtures: **never commit a hand history**, and
  never add one to a commit that has already been pushed without rewriting that commit — a
  follow-up delete leaves the blob reachable from the old commit.
- **Coverage is thinner than the file count suggests:** the tracked sidecars holding expected values
  are `.gt` **30**, `.hands` **39**, `.hp` **58** (each GG fixture adds one of every kind, so a
  local corpus of 14 hands lifts all three by 14), and they sit on only **60** distinct fixtures out
  of the 420 tracked `.txt`/`.xml` files, so the other ~360 still
  exercise the parser with no assertion about the rows it produces. `.hp` coverage per legacy site:
  FTP 18, Stars 8, Merge 7, Winamax 6, then ≤3. A site ported without a sidecar is only proven to
  not crash.
- Runner: `pyfpdb/TestHandsPlayers.py` (`-s <Sitename> [-f file]`), uses
  `pyfpdb/HUD_config.test.xml` + SQLite, and calls `db.recreate_tables()` — so **running it wipes
  the test database**. Never point it at a DB you care about. `TestSummaryImport.py`,
  `TestDetectInstalledSites.py`, `test_Database.py`, `test_PokerStars.py`, `test_FullTilt.py`,
  `test_Everleaf.py` are the other harnesses.
- Row comparison cannot see everything, so `pyfpdb/TestGgImport.py` checks the **database against the
  histories it came from** (read-only, SQLite-only, takes a corpus directory and `--config`). It
  re-derives each expectation from the raw text rather than from a sidecar. Eight checks, all
  passing on the 345-hand corpus, on the 14-fixture subset, and on
  `Documents/Projects/Poker/history/games`:
  every corpus hand present in `Hands`; the two money identities below, per hand; `rush` and
  `comment` non-null on every GG hand; **run-it-twice hands having `Boards` rows 1 and 2 whose five
  card ids equal the cards dealt on the history's own `*** FIRST/SECOND <street> ***` lines** (the
  deepest line of a run is that run's whole board); a Hero row with cards in every hand; and card
  visibility as an **exact set equality** —
  rows with `card1 > 0` == every Hero row ∪ every `(hand, player)` that a body `shows` or summary
  `showed` line reveals (452 == 452; 345 Hero + 107 opponents, and Hero itself is revealed 28 times).
  Its failure modes are proven twice: adding **one cent** to a summary `Total pot` in a scratch copy
  produced `FAIL per hand: sum(winnings) == ...` for those two hands and **exit code 1**, and
  swapping one card of a `*** SECOND RIVER ***` line (`[As]` → `[Kh]`) produced
  `FAIL run-it-twice ... got=['Tc','Js','Ts','6s','As'] want=[...,'Kh']`, also exit 1.
- Fixture names must describe measured structure, not a guess. The run-it-twice pair was renamed
  (`RunItTwice-both-runs-from-flop` → `RunItTwice-allin-on-turn`, `RunItTwice-second-run-from-turn`
  → `RunItTwice-allin-on-flop`) after §3.7 showed the first hand was all-in on the turn. The 56
  fixture and sidecar files were snapshotted before, regenerated after, and are **byte-identical**
  — which is also the evidence that the walk order (and therefore the `fileId`/`gametypeId` baked
  into the sidecars) survived the rename.
- **Assertion strength is measured, not assumed.** Each sidecar was sabotaged and the harness re-run:
  `gametype word flip` → 1 error, `winnings +1` in a `.hp` → 1 error, `rushTable=` mangled in a
  `.hands` comment → 1 error, `'runItTwice': True` → `False` → **0 errors**, which is how
  the exemption list in `compare_hands_file` was found to exempt that stat. After removing the exemption (§5.2) the
  same mutation reports `Stat: runItTwice, Hand: 925265777, Got: True / Expected: False`, and every
  sidecar was restored byte-exact from the snapshot.
- **Known gaps, so nobody reads the green as more than it is.**
  - *The 14 GG sidecars are self-blessed.* `TestHandsPlayers.py` generated them from this parser, so
    they pin today's behaviour against future edits; they cannot prove it matches GG. The external
    evidence is `TestGgImport.py` (money, visibility and both run boards re-derived from the raw
    text) plus `getRake`, which raises `FpdbParseError` per hand unless the site's `Total pot` equals
    both `collected + deductions` **and** the pot fpdb calculated from the betting. "0 parse errors
    over 345 hands" is therefore an assertion result, not a hope.
  - *The row harness never sees boards.* `compare_hands_file` deletes `boards` before comparing
    (`TestHandsPlayers.py:175`), so run-1/run-2 card identities are asserted only by
    `TestGgImport.py`, and only for the **2** run-it-twice hands the corpus contains.
  - *Duplicate skipping is measured, not asserted.* All 14 named fixtures re-state hands that also
    live in `bulk-sessions/`, so one harness run feeds 359 hand texts through 24 files and the
    database ends at **345** `Hands` rows (345 distinct `siteHandNo`) — the dedup path does run, but
    no test checks it, and which file wins a hand depends on `walk_testfiles`' unsorted `os.listdir`
    order. That order is also why the `fileId`/`gametypeId` values baked into the sidecars are
    machine-specific (see the byte-identical regeneration evidence above).
- Policy for this revival (matches how the project should have been run): assertions must encode
  **what the code actually produced and what the hand history says**, not what a plausible poker
  premise implies. When a test fails because reality disagrees with the assumption, measure the
  whole corpus, then fix the test and record the finding here.
- GG acceptance numbers. `TestHandsPlayers.py -s GGPoker` → **Total Errors: 0**; `TestGgImport.py` →
  **0 FAILURES**, exit 0. Measured ground truth, raw text and database agreeing:
  hands **345** (all distinct `siteHandNo`, `2026-10-09 10:28:41`..`13:24:24`), `FpdbParseError`
  **0**, logical `tableName`s **2** (`RushAndCash-holdem-nl-0.01/0.02` 167,
  `RushAndCash-omahahi-pl-0.01/0.02` 178) from **345** raw `RushAndCash<id>` table ids, `rush = 1`
  on **345**, `comment` non-null on **345** (longest 194 chars), aliases **1103** + `Hero` =
  **1104** `Players`, `HandsPlayers` **2070**, `HandsActions` **3789** with **48** `allIn` rows
  (= the **48** `and is all-in` lines), hands with no board **161** (raw and DB), hands with >1
  `collected` line **8**, hands with non-zero Jackpot **44**, non-zero Bingo/Fortune/Tax **0**,
  run-it-twice hands **2** (+ **4** `Boards` rows, whose **10** card ids equal the cards on those
  hands' street markers), EV-cashout hands **3**, `shows`/`showed` lines
  **135** body + **135** summary.
  Money, per hand and therefore in total: `sum(winnings) == Total pot - (rake+jackpot+bingo+fortune
  +tax)` and `sum(totalProfit) == cashDrop - deductions`, **0 violations in 345**, summing to pot
  **$148.24** − deductions **$4.57** = winnings **$143.67**, and −$4.57 + $0.80 cash drop =
  **−$3.77** total profit across **4** cash-drop hands (§3.3).

---

## 7. GUI (PySide6) `[SCREENS 1-3 BUILT 2026-10-13; 4-5 PLANNED]`

One module, `pyfpdb/HudReplayer.py`, replaces `fpdb.pyw` + the 18 `Gui*` modules for the near-term
product. It runs the real pipeline (`Importer` → `GGPokerToFpdb` → `DerivedStats` → `Database`) on a
`QThread`; nothing in the window parses a hand history itself.

**Database separation.** The app writes to `%APPDATA%\fpdb\database\fpdb.db3`; §6's regression
harness recreates a database named `fpdb` in the same folder. Keeping them apart is what lets the
GUI import the corpus without ever disturbing the harness. Measured in `fpdb.db3` after importing
the 10 files under `Documents/Projects/Poker/history/games`: **345 stored, 0 partial, 0 errors**,
345 `Hands`, 2070 `HandsPlayers`, 3789 `HandsActions`, 4 `Boards` (§3.7), 452 rows with known cards
(§3.4), 167 Hold'em + 178 Omaha Hi. Re-running the same folder reports
`0 stored, 345 duplicate, 0 partial, 0 errors` — the toolbar shows the `runImport()` totals.

**Screen 1 — import.** "Import hand histories..." picks a folder and runs the thread. The per-file
error counts the original plan asked for are *not* available: `Files.file` stores only the file's
basename (`Importer.py:154`) and this code path leaves `Files.hands/stored/dups/partial/errs` at 0,
so only the run totals are honest to display. Because only a stem is stored, the raw-text pane needs
the folder it came from; that is remembered in `%APPDATA%\fpdb\HudReplayer.history_folder`.

**Screen 2 — hand list.** One query over `Hands` + `Gametypes`, newest first, with a substring filter
on time/game/stakes/table (measured: `omaha` → 178 rows, an unmatched string → 0 rows, cleared → 345)
and Hero's `totalProfit` colour-coded per hand.

**Screen 3 — the table replayer.** A PokerTracker-style felt, not a list of streets. Two modules:
`pyfpdb/HudReplayer.py` works out what one moment of a hand looks like and hands it over as plain
ints and lists; `pyfpdb/HudTable.py` decides where every box goes and paints it. Nothing in
`HudTable` reads a database, and nothing in `HudReplayer` draws.

Transport: `|< < Play > >|`, a frame counter, a 0.5x-4x speed combo, a `Show BB` switch for the
money unit, one button per street that exists in the hand, `run 1` / `run 2` for the hands that have
two boards, a slider over every action, and the action log itself as a clickable ruler. Keys:
Left/Right one action, Up/Down one street (down leads to later streets, the way the buttons and the
log are ordered), Home/End, Space to play. Those are handled in `keyPressEvent`, not as
`QShortcut`s, so they cannot fire while he is typing in the filter box or moving through the hand
list.

Everything about placement lives in `HudTable.layout(felt, state)`, which returns rectangles and
paints nothing — the seam that lets a test check the drawing without a screen. Its rules:

- The ring reads **BTN, CO, HJ, UTG, BB, SB** out from the hero, which is the direction play passes
  around a live table: whoever acts next is on the hero's right, the small blind on his left.
  Measured, GG numbers seats the other way round — `HandsPlayers.position` for hand 238 is
  seat 1 BTN, 2 SB, 3 BB, 4 UTG, 5 HJ, 6 CO — so `_seat_rows` walks the seat numbers *down* from the
  hero. Getting that backwards mirrors the entire table, and `scratch_table_check.py` asserts the
  ring for all 345 hands rather than trusting a screenshot of one.
- A seat is a stack from the rail inwards: name plate, then cards along the radius, then the bet.
  Sizes come from `sizes_for(felt)` and are proportional to the felt with caps; the caps are the
  largest values the walk below passes at, not taste.
- `ALL-IN` and `WINS $x` are text *inside* the plate, and the seat to act gets a gold border. Both
  used to be boxes on the plate's inner edge, which is exactly where that seat's own cards land.
  Stack and tag share the plate's second line, and the tag is the half that carries information the
  stack cannot, so when the two do not fit the *stack* is elided and the tag stays right-aligned.
- The board's five slots plus the pot pill are one keep-out rect, grown by a box's own half extents
  before its centre is tested — a centre can sit outside a rect the box is standing on.
- When the board stops a bet pill before it has cleared its own cards (a side seat on a short, wide
  table has a nearly flat radius), the pill slides round the cards' inner side instead.
- The board run is named in the counter label, never on the felt: appending it to the pot pill
  widened that pill and pushed the side seats' bets onto the board.
- A folded opponent shows no cards; the hero's own stay visible after a fold.

**Money rules that came from measurement rather than assumption:**

- Opponents' cards appear only on the showdown street (or for Hero), since `card1 > 0` rows come from
  `shows` lines that reveal them late (§3.4, §3.6).
- `Hands.street1Pot/2/3` are the pots at flop/turn/river (`Hand.py:1067-1078`); `street4Pot` and
  `showdownPot` are 0 in all 345 rows, so showdown reuses the river column, and the preflop/posts
  totals are summed from `HandsActions` instead. fpdb's raise rows store
  `(amount = raise-by, raiseTo, amountCalled)` and the pot grows by `amount + amountCalled`
  (`Hand.py:615-745`).
- Those pot columns still count an **uncalled bet the bettor gets straight back**, so the pot and the
  winner's payout disagree on screen unless it is subtracted: measured on this corpus, **294 of 345
  hands** have one, always exactly **one** returning player, **$49.18** in total, largest **$3.63**.
  The database gives the amount exactly — a player's money at risk is `winnings - totalProfit`, so
  `returned = wagered - (winnings - totalProfit)` — and it never went negative on any of the 345
  hands. With it subtracted, `last-street pot - returned == sum(winnings) + sum(rake)` holds on
  **184/184** hands that have a street-pot value, which is what the driver asserts for every hand.
- `Show BB` redraws the felt in big blinds: `ReplayWidget.fmt()` divides by the hand's own
  `Gametypes.bigBlind`, which is an **integer in cents** (measured: one distinct value across the
  corpus, `2`, i.e. the $0.01/$0.02 stakes in the title). `bb_text` keeps at most two decimals and
  drops trailing zeros, so 583 cents reads `291.5 BB` and not `291.50 BB`. Only the felt converts:
  the action log and the title quote the site's own dollars, and the blinds in the title are the
  divisor the numbers on the felt were divided by, so leaving them in dollars is what makes the
  conversion checkable. A hand with no known blind falls back to dollars rather than dividing by
  zero. BB strings are wider than the dollars they replace, which is why the geometry walk below
  runs every frame in both units.

**Verified how.** Two drivers in the workspace, both on the native `windows` Qt platform — the
offscreen one has no font database and renders every glyph as tofu. Neither shows a window on the
desktop: `Qt.WA_DontShowOnScreen` plus `show()` is what makes that work, because a hidden top-level
never activates its layouts, so `grab()` renders at the widget's minimum size and any geometry
measured from it is fiction.

- `scratch_table_check.py` walks **every frame of all 345 imported hands** at five felts — the
  widget's own minimum (760x430 → 688x358 felt) up through 1393x760 — **and in both money units**,
  asserting no two drawn boxes overlap: **52,750 frame/felt/unit combinations, clean**. The states
  it lays out are built by the same `_seat_rows()`/`fmt()` the window uses, so the walk cannot drift
  away from what is drawn. Heads-up is not in that walk because it is not in the data
  (`select seats, count(*) from Hands` → `[(6, 345)]`), so one heads-up frame with four cards, an
  all-in, a bet and a payout is built by hand, labelled synthetic, in dollars and in big blinds, and
  put through the same assertion. It also writes the PNGs, which are then looked at.
- `scratch_controls_check.py` presses every control with Qt's own `QTest`/`sendEvent` (never the OS
  input layer) and reads back what the felt was handed, so a button that moves a hidden index and
  leaves the drawing untouched fails. **47 checks, 0 failures.** It caught two real bugs at first:
  `>|` and End called `seek(-1)`, which clamps to 0, so they jumped to the *start* of the hand; and
  `seek()` called `stop_play()`, so playback died after a single frame —
  `seek(index, keep_playing=...)` is now the timer's path. For `Show BB` it compares the two
  renderings of the same frame number by number: every felt figure must lose its `$`, and
  `bb_value * Gametypes.bigBlind` must still equal the cents the dollars said.

**Still planned.** Screen 4 (player stats from `DerivedStats`) and screen 5 (EV graph), plus a
delivered launcher: a named, double-clickable Desktop + Start-menu entry, not "run this script from a
path". Locally compiled unsigned `.exe` files are blocked on this machine by Smart App Control, so
the launcher hosts the app behind a signed process instead of shipping a self-built binary.

---

## 8. Risks and open questions

- **License.** Code is AGPL-3.0. Fine for personal use; if this ever ships publicly the source must
  stay AGPL-compatible and the docs need the license preserved.
- **Timezone.** Resolved by measurement, see §3.5: header times use one fixed clock, the filename
  clock is that + 6 h, and files are newest-first. What is still unmeasured is what the *user's*
  local zone is relative to the header clock — ask (or compare against a session the user remembers)
  before bucketing sessions by day. `[PARTIALLY RESOLVED]`
- **Identity.** §3.2 is the biggest product risk: a HUD whose opponent stats reset every session is
  of limited use. Decide early whether that is acceptable.
- **Heads-up and tournaments** appear in the old regression corpus but not in the GG corpus; the GG
  importer should fail loudly on formats we have no samples for rather than parse them wrongly.
- **Dead upstream.** No issue tracker, no CI, no releases since 2012. Everything we assert must be
  reproducible locally.
