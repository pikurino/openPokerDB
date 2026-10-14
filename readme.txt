OpenPokerDB
===========

A revival of fpdb ("Free Poker Database"), forked in 2026 and rebuilt for Windows and Python 3.12: a
GGPoker hand-history importer, a SQLite statistics database, and a PokerTracker-style table replayer
with a PySide6 GUI.

Run it
------
    .venv\Scripts\python.exe pyfpdb\HudReplayer.py

The window imports a folder of GG hand histories into %APPDATA%\fpdb\database\fpdb.db3 and replays
any stored hand one action at a time: |< < Play > >|, a slider over every action, one button per
street, and the action log as a clickable ruler. Left/Right step an action, Up/Down step a street,
Home/End jump, Space plays, and 'Show BB' redraws every number on the felt in big blinds.
Options: --db <sqlite file>, --history <folder>, --hands <N>.

Hand histories are private
--------------------------
No hand history is committed to this repository, and none should be: a history names the opponents
at a table, their stacks, and every decision in the hand. The GG fixtures the regression harness
asserts on are therefore not shipped. To run GG tests, copy your own histories into the folder the
test reads:

    pyfpdb\TestHandsPlayers.py -s GGPoker     imports cash/GGPoker/ (skips if you have no files)
    pyfpdb\TestGgImport.py <corpus_dir>       checks the database against the histories it came from

See DESIGN.md section 6.

Read these
----------
    DESIGN.md                        what the project is, the measured facts about the data,
                                     what is done and what is planned
    docs\gg-hand-history-format.md   the GGPoker history format, line by line
    contributors.txt                 who made this fork

Names that still say fpdb
-------------------------
The package (pyfpdb/), the site converters (*ToFpdb.py), the exception classes (FpdbParseError), the
config file (HUD_config.xml) and the database (fpdb.db3) keep their upstream names on purpose: they
are what existing databases, config files and the regression harness point at. Only user-visible text
was rebranded.

License
-------
AGPL-3.0, as upstream. See agpl-3.0.txt; source file copyright headers are unchanged.
