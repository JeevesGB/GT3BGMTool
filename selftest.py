"""Checks the tool against data it builds itself - no game files needed.

Run it after cloning, or before trusting a change:

    python selftest.py                 # everything that needs no outside data
    python selftest.py song.aup3       # also parse a real Audacity project

This proves the code paths are sound. It does not replace testing against PD's own files, which is what
actually settles whether a format reading is right.
"""

from __future__ import annotations
import math
import os
import struct
import sys
import tempfile

from gt3bgm import __version__, psadpcm as ps, verify as vfy
from gt3bgm.adsinf import AdsInf, ENTRY_SIZE, RACE_GROUP
from gt3bgm.markers import MarkerSet, NUM_CHANNELS

GROUPS = 17
passed = failed = 0


def check(ok: bool, what: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {what}" + (f" ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {what}" + (f" ({detail})" if detail else ""))


def tone(seconds: float, rate: int = 44100) -> list[list[int]]:
    """Something with real transients in it - a pure sine flatters an ADPCM encoder."""
    n = int(seconds * rate)
    left, right = [], []
    for i in range(n):
        t = i / rate
        env = 1.0 if (i // (rate // 4)) % 2 == 0 else 0.25          # a step every quarter second
        v = math.sin(2 * math.pi * 440 * t) * 0.6 + math.sin(2 * math.pi * 1970 * t) * 0.3
        left.append(max(-32768, min(32767, int(v * env * 30000))))
        right.append(max(-32768, min(32767, int(v * env * 22000))))
    return [left, right]


def build_inf(songs) -> bytes:
    """A minimal MADS index, written by hand so the reader is tested against something it did not produce.
    songs: [(group, name, title, artist, MarkerSet)]"""
    ordered = sorted(songs, key=lambda s: s[0])
    entries_at = 0x10 + GROUPS * 8
    pos = entries_at + len(ordered) * ENTRY_SIZE
    table_at = []
    for s in ordered:
        table_at.append(pos)
        pos += s[4].size
    strings_at, blob, offs = pos, bytearray(), []
    for group, name, title, artist, _ in ordered:
        row = []
        for text in (name, name + ".ads", title, artist):
            row.append(strings_at + len(blob))
            blob += text.encode("latin-1") + b"\0"
        offs.append(row)

    out = bytearray(b"MADS" + struct.pack("<III", 1, 0, len(ordered)))
    run = 0
    for g in range(GROUPS):
        count = sum(1 for s in ordered if s[0] == g)
        out += struct.pack("<II", entries_at + run * ENTRY_SIZE, count)   # empty groups keep the running offset
        run += count
    for i, row in enumerate(offs):
        out += struct.pack("<4I", *row) + struct.pack("<I", table_at[i])
    for s in ordered:
        out += s[4].write()
    return bytes(out + blob)


print(f"GT3BGMTool {__version__} self-test - Python {sys.version.split()[0]}\n")

print("PS-ADPCM and .ads")
pcm = tone(1.5)
ads = ps.write_ads(44100, pcm)
check(ads[:4] == b"SShd" and ads[0x28 - 8:0x28 - 4] == b"SSbd", "container has both chunk headers")
rate, back = ps.read_ads(ads)
check(rate == 44100 and len(back) == 2, "decodes back as 44100 Hz stereo")
db = vfy.snr(pcm, ads)
check(db > 30, "round-trip quality", f"{db:.1f} dB")
check(ps.samples_per_channel(ads) >= len(pcm[0]), "length rule covers the whole song",
      f"{ps.samples_per_channel(ads)} >= {len(pcm[0])}")
wav = os.path.join(tempfile.mkdtemp(), "t.wav")
ps.write_wav(wav, 44100, pcm)
r2, p2 = ps.read_wav(wav)
check(r2 == 44100 and p2 == pcm, "WAV survives a write and read unchanged")
try:
    ps.read_wav(__file__)
    check(False, "a non-WAV is rejected")
except Exception:
    check(True, "a non-WAV is rejected")

print("\nMarker tables")
m = MarkerSet(44100, 44100 * 10)
m.grid(120.0, 0.25, 4, 2)
check(len(m.ch[1]) == 20, "a 120 BPM grid over 10 s gives 20 beats", f"{len(m.ch[1])}")
check(m.ch[4] == m.ch[3] and len(m.ch[3]) == 3, "cuts land on ch3 with its ch4 copy", m.summary())
check(not m.problems(), "no markers out of range")
round_trip = MarkerSet.read(m.write(), 0)
check([list(c) for c in round_trip.ch] == [list(c) for c in m.ch] and round_trip.length == m.length,
      "table survives write and read")
check(m.size == 0x10C + 4 * m.total, "size matches the header plus the markers")
m.ch[9] = [m.length + 1]
check(bool(m.problems()), "a marker past the end is reported")

print("\nads.inf")
shared = "One Artist"                                   # PD shares strings; exercise the dedupe
m_a = MarkerSet(44100, 44100 * 60)
m_a.grid(100.0, 0.0)
base = build_inf([(0, "menu01", "Menu", shared, MarkerSet(44100, 44100)),
                  (RACE_GROUP, "race_a", "First", shared, m_a),
                  (RACE_GROUP, "race_b", "Second", "Other", MarkerSet(44100, 44100 * 90))])
inf = AdsInf.read(base)
check(len(inf.songs) == 3 and inf.groups == GROUPS, "reads three songs across 17 groups")
check(inf.write() == base, "rewrite is byte-identical")
check(inf.name(inf.songs[0]) == "menu01" and inf.file_name(inf.songs[1]) == "race_a.ads",
      "names and file fields read back")

inf = AdsInf.read(base)
table = MarkerSet(44100, ps.samples_per_channel(ads))
table.set_cuts([0.5, 1.0])
inf.add_song(RACE_GROUP, "mine", "My Song", "Me", table)
out = inf.write()
again = AdsInf.read(out)
check(len(again.songs) == 4, "added song is there after a read back")
added = next(s for s in again.songs if again.name(s) == "mine")
check(again.text_of(added, 2) == "My Song" and added.group == RACE_GROUP, "its text and group survive")
check(added.table.ch[3] == table.ch[3], "its markers survive")
res = vfy.verify(base, out, {"mine": ads})
check(res.ok, "verification passes on the result", res.report().splitlines()[0])
try:
    inf.add_song(RACE_GROUP, "mine", "Dupe", "", MarkerSet(44100, 1000))
    check(False, "a duplicate name is refused")
except ValueError:
    check(True, "a duplicate name is refused")

inf = AdsInf.read(base)
inf.rename(inf.songs[0], "Renamed", "Someone")
res = vfy.verify(base, inf.write(), {})
check(res.ok and any("retitled" in t for _, t in res.checks), "a retitle is reported, not called damage")

inf = AdsInf.read(base)
victim = next(s for s in inf.songs if inf.name(s) == "race_b")
inf.remove_song(victim)
out = inf.write()
after = AdsInf.read(out)
check(len(after.songs) == 2 and "race_b" not in [after.name(s) for s in after.songs], "removal takes the entry out")
check(after.write() == out, "the compacted file still rewrites identically")
res = vfy.verify(base, out, {}, removed={"race_b"})
check(res.ok, "verification passes on a removal", res.report().splitlines()[0])
res = vfy.verify(base, out, {})
check(not res.ok, "an UNEXPECTED removal is caught")

if len(sys.argv) > 1:
    print("\nAudacity project")
    from gt3bgm.audacity import Project
    try:
        pr = Project(sys.argv[1])
        check(pr.rate > 0, "project parses", pr.summary())
        cuts = pr.cut_times()
        check(cuts == sorted(cuts) and all(c >= 0 for c in cuts), "cut times are sorted and non-negative",
              f"{len(cuts)} of them")
    except Exception as e:
        check(False, "project parses", str(e))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
