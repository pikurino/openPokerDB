#!/usr/bin/env python3
"""Measure the GGPoker hand-history format over a corpus and print a spec-ready inventory.

Usage:  python tools/gg_format_probe.py <dir-with-GG-*.txt>

Everything printed here is measured from the files. Output is the source of
docs/gg-hand-history-format.md -- regenerate that doc from this script rather than editing numbers.
"""
import datetime
import glob
import os
import re
import sys
import time
from collections import Counter, defaultdict

MONEY = re.compile(r"\$[\d,]+(?:\.\d+)?")
# Aliases are 6-8 chars of [0-9a-f] and 29 of them are all-digits -- never require a letter.
PLAYER = re.compile(r"\b[0-9a-f]{6,8}\b")
HAND_HDR = re.compile(
    r"^Poker Hand #(?P<hid>\w+): (?P<game>.*?) \((?P<sb>\$[\d.]+)/(?P<bb>\$[\d.]+)\) - "
    r"(?P<date>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})"
)
TABLE = re.compile(r"^Table '(?P<t>[^']+)' (?P<seats>\d+)-max Seat #(?P<btn>\d+) is the button")
TABLE_M = re.compile(TABLE.pattern, re.M)
SEAT_STACK = re.compile(r"^Seat (?P<seat>\d+): (?P<name>\S+) \((?P<chips>\$[\d.]+) in chips\)")
DEALT = re.compile(r"^Dealt to (?P<name>\S+)(?: \[(?P<cards>[^\]]*)\])?\s*$")
HAND_RANK = re.compile(
    r"(\(| with )(?:a |an |the )?"
    r"(?:Royal Flush|Straight Flush|Four of a Kind|Full House|Three of a Kind|Two Pair|High Card|Flush|"
    r"Straight|Pair|\w+ high)"
    r"(?:, \w+ full of \w+|, \w+ to \w+|, \w+ and \w+| \w+ high| of \w+|, \w+)?",
    re.I,
)
ROLE = re.compile(r"^Seat \d+: \S+(?: \((?:button|small blind|big blind)\))? ")
BOARD = re.compile(r"^(?P<street>\*\*\* (?:FLOP|TURN|RIVER) \*\*\*)(?: (?P<prev>\[.*\]))?(?: (?P<new>\[.*\]))?\s*$")
COLLECT = re.compile(r"^(?P<name>\S+) collected (?P<amt>\$[\d.]+) from pot")

RAKE_PARTS = re.compile(
    r"^Total pot (?P<pot>\$[\d.]+) \| Rake (?P<rake>\$[\d.]+) \| Jackpot (?P<jackpot>\$[\d.]+)"
    r" \| Bingo (?P<bingo>\$[\d.]+) \| Fortune (?P<fortune>\$[\d.]+) \| Tax (?P<tax>\$[\d.]+)"
)


def shape(line):
    """Normalise a line into a comparable shape string."""
    s = line.strip()
    s = HAND_RANK.sub(lambda m: m.group(1) + "<HANDRANK>", s)
    s = re.sub(r"\$[\d,]+(?:\.\d+)?", r"$N", s)
    s = re.sub(r"\[(?:[^\[\]]{0,40})\]", "[CARDS]", s)
    s = re.sub(r"\b[0-9a-f]{6,8}\b", "PNAME", s)
    s = re.sub(r"#\w{6,}", "#HANDID", s)
    s = re.sub(r"\b\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\b", "DATETIME", s)
    s = re.sub(r"RushAndCash\d+", "RushAndCashN", s)
    s = re.sub(r"\bSeat \d+\b", "Seat N", s)
    s = re.sub(r"Seat #\d+", "Seat #N", s)
    s = re.sub(r"\b\d+(?:\.\d+)?\b", "N", s)
    return s


def f(s):
    return float(s.replace("$", "").replace(",", ""))


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    files = sorted(glob.glob(os.path.join(d, "*.txt")))
    if not files:
        sys.exit(f"no .txt files in {d}")

    shapes = Counter()
    example = {}
    hands = []
    verbs = Counter()
    shows_with_rank = Counter()
    summary_outcomes = Counter()
    per_file = {}
    rare = []

    for path in files:
        text = open(path, encoding="utf-8", errors="replace").read()
        blocks = re.split(r"(?=^Poker Hand #)", text, flags=re.M)
        blocks = [b for b in blocks if b.startswith("Poker Hand #")]
        first = HAND_HDR.search(blocks[0]) if blocks else None
        last = HAND_HDR.search(blocks[-1]) if blocks else None
        times = [m["date"] for m in (HAND_HDR.search(b) for b in blocks) if m]
        ids = [m["hid"] for m in (HAND_HDR.search(b) for b in blocks) if m]
        stamp = "%Y/%m/%d %H:%M:%S"
        fn = re.match(r"GG(\d{8})-(\d{4})", os.path.basename(path))
        offset_min = None
        if fn and times:
            fname_dt = datetime.datetime.strptime(fn.group(1) + fn.group(2), "%Y%m%d%H%M")
            offset_min = (fname_dt - datetime.datetime.strptime(times[-1], stamp)).total_seconds() / 60
        per_file[os.path.basename(path)] = {
            "hands": len(blocks),
            "first_hdr_time": first["date"] if first else None,
            "last_hdr_time": last["date"] if last else None,
            "descending_time": all(a > b for a, b in zip(times, times[1:])),
            "descending_id": all(a > b for a, b in zip(ids, ids[1:])),
            "fname_minus_oldest_min": offset_min,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
            "fname_table": (re.search(r"RushAndCash\d+", os.path.basename(path)) or [None])[0],
            "hdr_tables": len({m.group("t") for b in blocks for m in [TABLE_M.search(b)] if m}),
        }
        for b in blocks:
            lines = [ln for ln in b.splitlines() if ln.strip()]
            hdr = HAND_HDR.search(lines[0])
            tab = TABLE.search(lines[1]) if len(lines) > 1 else None
            rec = {
                "id": hdr["hid"] if hdr else None,
                "game": hdr["game"] if hdr else None,
                "time": hdr["date"] if hdr else None,
                "table": tab["t"] if tab else None,
                "seats": tab["seats"] if tab else None,
                "pot": None,
                "rake": {},
                "collects": [],
                "dealt": [],
                "summary_seats": [],
                "lines": lines,
            }
            for ln in lines:
                shapes[shape(ln)] += 1
                example.setdefault(shape(ln), ln.strip())
                m = RAKE_PARTS.match(ln)
                if m:
                    rec["pot"] = f(m["pot"])
                    rec["rake"] = {k: f(m[k]) for k in ("rake", "jackpot", "bingo", "fortune", "tax")}
                m = COLLECT.match(ln)
                if m:
                    rec["collects"].append((m["name"], f(m["amt"])))
                m = DEALT.match(ln)
                if m:
                    rec["dealt"].append((m["name"], m["cards"]))
                if re.match(r"^Seat \d+: \S+( \([^)]*\))? (folded|won|collected|showed|called|finished)", ln):
                    out = PLAYER.sub("PNAME", ROLE.sub("", ln))
                    out = re.sub(r"\$[\d.,]+", "$N", out)
                    summary_outcomes[out] += 1
                    rec["summary_seats"].append(ln)
                am = re.match(r"^(?P<name>\S+): (?P<verb>.*?)(?: \(\$?[\d.]+\))?(?: and is all-in)?$", ln)
                if am and not ln.startswith(("Seat ", "Dealt ", "Total pot", "Board")):
                    if am["verb"].startswith("shows ["):
                        shows_with_rank[bool(HAND_RANK.search(am["verb"]))] += 1
                        v = "shows [CARDS]" + (" (<HANDRANK>)" if HAND_RANK.search(am["verb"]) else "")
                    else:
                        v = HAND_RANK.sub(lambda m: m.group(1) + "<HANDRANK>", am["verb"])
                    v = re.sub(r"\$[\d.]+", "$N", v)
                    v = re.sub(r"(raises|calls|bets) \$N( to \$N)?", r"\1 $N\2", v)
                    verbs[v] += 1
            hands.append(rec)

    hands.sort(key=lambda h: (h["time"] or ""))

    print("# GGPoker corpus measurement")
    print(f"files={len(files)} hands={len(hands)} "
          f"games={dict(Counter(h['game'] for h in hands))}")

    print("\n## Distinct line shapes (count | shape | first example)")
    for sh, n in shapes.most_common():
        print(f"{n:6}  {sh[:96]:96}  :: {example[sh][:78]}")

    print("\n## Action verbs after 'Player: '")
    for v, n in verbs.most_common():
        print(f"{n:6}  {v}")

    print("\n## SUMMARY per-seat outcome phrases")
    for o, n in summary_outcomes.most_common():
        print(f"{n:6}  {o}")

    print("\n## Rare shapes (occur exactly once -- parser edge cases)")
    for sh, n in shapes.items():
        if n == 1:
            print(f"     {sh[:110]}  :: {example[sh][:100]}")

    print("\n## Multi-pot / rake-component measurements")
    multi = [h for h in hands if len(h["collects"]) > 1]
    print(f"hands with >1 'collected' line: {len(multi)}")
    for comp in ("rake", "jackpot", "bingo", "fortune", "tax"):
        vals = [h["rake"][comp] for h in hands if h["rake"]]
        nz = [v for v in vals if v > 0]
        print(f"  {comp:8} nonzero in {len(nz):4} hands   max ${max(vals):.2f}" if vals else f"  {comp}: none")
    bad = [h for h in hands if h["rake"] and abs(sum(h["rake"].values()) - h["rake"]["rake"]) > 0.005]
    print(f"  hands where the 5 components differ from Rake alone: {len(bad)}")
    tot_components = sum(sum(h['rake'].values()) for h in hands if h['rake'])
    print(f"  total of all 5 components: ${tot_components:.2f} | total pot: "
          f"${sum(h['pot'] or 0 for h in hands):.2f}")

    print("\n## Invariants (must be 0 violations for the importer to trust them)")
    viol = [h for h in hands if h["pot"] is not None
            and abs(h["pot"] - sum(a for _, a in h["collects"]) - sum(h["rake"].values())) > 0.005]
    print(f"  pot != sum(collected) + rake+jackpot+bingo+fortune+tax : {len(viol)} hands")
    for h in viol[:3]:
        print(f"     {h['id']} pot={h['pot']} collected={sum(a for _, a in h['collects'])} rake={h['rake']}")
    summ_won = re.compile(r"(?:won|collected) \(\$([\d.]+)\)")
    cross = []
    for h in hands:
        body = Counter()
        for n, a in h["collects"]:
            body[n] += a
        s = Counter()
        for ln in h["summary_seats"]:
            m = re.match(r"^Seat \d+: (\S+?)(?: \((?:button|small blind|big blind)\))? ", ln)
            if not m:
                continue
            for amt in summ_won.findall(ln):
                s[m.group(1)] += float(amt)
        if {k: round(v, 2) for k, v in body.items()} != {k: round(v, 2) for k, v in s.items()}:
            cross.append(h["id"])
    print(f"  per-player summary winnings != body 'collected' : {len(cross)} hands {cross}")
    print(f"  of those, hands containing an EV Cashout phrase : "
          f"{sum(1 for h in hands if h['id'] in cross and any('Cashout' in l for l in h['lines']))}")

    print("\n## Hero vs opponent hole cards")
    with_cards = [d for h in hands for d in h["dealt"] if d[1]]
    print(f"'Dealt to' lines total: {sum(len(h['dealt']) for h in hands)}; "
          f"lines carrying cards: {len(with_cards)}; "
          f"card-line owners: {dict(Counter(d[0] == 'Hero' and 'Hero' or 'OTHER' for d in with_cards))}")
    print(f"card counts seen in 'Dealt to': {dict(Counter(len(d[1].split()) for d in with_cards))}")

    print("\n## Where showdown cards and boards appear")
    def idx(lines, pred):
        return [i for i, ln in enumerate(lines) if pred(ln)]
    pre_flop_shows = after_river = no_board = 0
    for h in hands:
        L = h["lines"]
        s = idx(L, lambda l: ": shows [" in l)
        fl = idx(L, lambda l: l.startswith("*** FLOP") or l.startswith("*** FIRST FLOP"))
        ri = idx(L, lambda l: l.startswith("*** RIVER") or l.startswith("*** FIRST RIVER"))
        if s and fl and max(s) < min(fl):
            pre_flop_shows += 1
        if s and ri and max(s) > min(ri):
            after_river += 1
        if not fl:
            no_board += 1
    print(f"hands whose 'shows' lines are all before the flop: {pre_flop_shows}")
    print(f"hands with a 'shows' line after the river marker: {after_river}")
    print(f"hands with no board dealt at all: {no_board}")
    street = [l for h in hands for l in h["lines"] if l.startswith("*** ") and ("TURN" in l or "RIVER" in l)]
    print("TURN/RIVER street lines not shaped as '[board] [new]': "
          + str(sum(1 for l in street if l.count("[") != 2)) + " of " + str(len(street)))

    print("\n## Fast-fold table identity")
    prev = None
    changed = 0
    for h in hands:
        if prev is not None and h["table"] != prev:
            changed += 1
        prev = h["table"]
    print(f"consecutive hand pairs with a different table id: {changed} of {len(hands)-1}")
    print(f"unique table ids overall: {len({h['table'] for h in hands})} vs hands {len(hands)}")
    print(f"max seats declared: {dict(Counter(h['seats'] for h in hands))}")
    for name, info in sorted(per_file.items()):
        print(f"  {name[:44]:44} hands={info['hands']:3} distinct_tables={info['hdr_tables']:3} "
              f"fname_table={info['fname_table']} newest_first_by_time={info['descending_time']} "
              f"by_hand_id={info['descending_id']}")

    print("\n## Player aliases")
    def aliases(t):
        return set(re.findall(r"Seat \d+: (\S+) \(", t)) - {"Hero"}
    per_file_ids = {os.path.basename(p): aliases(open(p, encoding="utf-8", errors="replace").read())
                    for p in files}
    allc = Counter()
    for s in per_file_ids.values():
        allc.update(s)
    lens = Counter(len(a) for a in allc)
    print(f"unique aliases: {len(allc)} | appearing in >1 file: {sum(1 for v in allc.values() if v > 1)}"
          f" | max files for one alias: {max(allc.values())}")
    print(f"alias length distribution: {dict(sorted(lens.items()))}")
    print(f"all-digit aliases (breaks any regex needing a letter): "
          f"{sum(1 for a in allc if a.isdigit())} e.g. {sorted(a for a in allc if a.isdigit())[:4]}")
    print(f"non-[0-9a-f] aliases: {sorted(a for a in allc if not re.fullmatch(r'[0-9a-f]+', a))}")
    print(f"'Hero' appears as a seat name: "
          f"{sum(1 for h in hands for s in h['summary_seats'] if 'Hero' in s) > 0}")

    print("\n## Timezone evidence (filename clock vs header clock; header date format is YYYY/MM/DD)")
    for name, info in sorted(per_file.items()):
        off = info["fname_minus_oldest_min"]
        print(f"  {name[:40]:40} newest={info['first_hdr_time']} oldest={info['last_hdr_time']} "
              f"mtime={info['mtime']} filename_minus_oldest={off:.1f} min" if off is not None
              else f"  {name[:40]:40} (no filename timestamp)")
    offs = [i["fname_minus_oldest_min"] for i in per_file.values() if i["fname_minus_oldest_min"]]
    if offs:
        print(f"  filename clock = oldest header clock + {min(offs):.1f}..{max(offs):.1f} min "
              f"(filename is truncated to the minute)")

    print("\n## Currency formatting edge cases")
    amts = [a for h in hands for ln in h["lines"] for a in re.findall(r"\$([\d,]*(?:\.\d+)?)", ln)]
    print(f"money tokens: {len(amts)} | max ${max(float(a.replace(',','')) for a in amts):.2f} "
          f"| decimals: {dict(Counter(len(a.split('.')[1]) for a in amts if '.' in a))} "
          f"| integer-only: {sum(1 for a in amts if '.' not in a)}")
    sep = [h for h in hands for ln in h["lines"] if re.search(r"\$\d{1,3},\d{3}", ln)]
    print(f"lines with thousands separators: {len(sep)}")
    print(f"lines with 'and is all-in': {sum(1 for h in hands for ln in h['lines'] if 'and is all-in' in ln)}")
    print(f"lines with 'Mucked': {sum(1 for h in hands for ln in h['lines'] if 'Mucked' in ln)}")
    print(f"lines with ante/bring-in: {sum(1 for h in hands for ln in h['lines'] if re.search(r'ante|bring-in', ln, re.I))}")
    print(f"showdown 'shows' lines: {sum(1 for h in hands for ln in h['lines'] if ': shows [' in ln)}")
    rit = [h for h in hands if any(re.match(r"\*\*\* (FIRST|SECOND) ", l) for l in h["lines"])]
    plain = [h for h in hands if any(l.startswith("*** TURN ***") for l in h["lines"])]
    two_groups = [h for h in plain if any(l.startswith("*** TURN ***") and l.count("[") > 2 for l in h["lines"])]
    print(f"hands with a plain '*** TURN ***': {len(plain)}; of those carrying 2 card groups: {len(two_groups)}")
    print(f"hands run twice (FIRST/SECOND markers): {len(rit)}")
    for h in rit:
        print("   e.g. hand " + str(h["id"]) + " street markers: "
              + " | ".join(l for l in h["lines"] if l.startswith("***")))


if __name__ == "__main__":
    main()
