
"""GT3 sequenced-music tool.

Works with the three-file set:
  music.inf  (MSEQ)  — song index
  music.seq  (SEQG)  — sequences
  music.ins  (INST)  — instrument bank (pass-through for now)

Commands
--------
  list                          list songs in music.inf
  info                          summary of all three files
  export-midi <seq_index> [out] extract one sequence to MIDI
  export-all-midi <outdir>      extract every sequence to MIDI
  replace-seq <seq_index> <mid> replace a sequence from a MIDI file
  save                          write modified files back (after replace-seq)

Usage examples
--------------
  python gt3seqtool.py -d path/to/music list
  python gt3seqtool.py -d path/to/music export-midi 0 main01.mid
  python gt3seqtool.py -d path/to/music replace-seq 0 new_song.mid
  python gt3seqtool.py -d path/to/music save
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running from the tool directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gt3bgm.mseq import Mseq
from gt3bgm.seqg import SeqG, sequence_to_midi, midi_to_sequence
from gt3bgm.inst import Inst


class MusicSet:
    def __init__(self, directory: str):
        self.directory = directory
        self.inf_path = os.path.join(directory, "music.inf")
        self.seq_path = os.path.join(directory, "music.seq")
        self.ins_path = os.path.join(directory, "music.ins")
        for p in (self.inf_path, self.seq_path, self.ins_path):
            if not os.path.isfile(p):
                raise FileNotFoundError(f"Missing {p}")
        self.mseq = Mseq.open(self.inf_path)
        self.seqg = SeqG.open(self.seq_path)
        self.inst = Inst.open(self.ins_path)
        self._dirty = False

    def list_songs(self):
        print(f"{'#':>3}  {'name':<14}  {'title':<12}  {'artist':<14}  seq  bpm")
        print("-" * 70)
        for i, s in enumerate(self.mseq.songs):
            bpm = ""
            if 0 <= s.seq_index < len(self.seqg.sequences):
                bpm = f"{self.seqg.sequences[s.seq_index].bpm:.0f}"
            print(
                f"{i:3d}  {s.name:<14}  {s.title:<12}  {s.artist:<14}  "
                f"{s.seq_index:3d}  {bpm}"
            )

    def info(self):
        print(f"Directory : {self.directory}")
        print(f"music.inf : {len(self.mseq.songs)} songs (MSEQ v{self.mseq.version})")
        print(f"music.seq : {self.seqg.sequence_count()} sequences (SEQG)")
        for i, seq in enumerate(self.seqg.sequences):
            active = sum(1 for t in seq.tracks if t.events)
            print(f"            [{i}] {seq.bpm:.1f} BPM  vol=0x{seq.master_volume:04X}  "
                  f"{active}/16 tracks with events")
        print(f"music.ins : {self.inst.size} bytes (INST, pass-through)")

    def export_midi(self, seq_index: int, out_path: str):
        if not 0 <= seq_index < len(self.seqg.sequences):
            raise IndexError(f"seq_index {seq_index} out of range "
                             f"(0..{len(self.seqg.sequences)-1})")
        sequence_to_midi(self.seqg.sequences[seq_index], out_path)
        print(f"Wrote {out_path}")

    def export_all_midi(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)
        for i, seq in enumerate(self.seqg.sequences):
            # Prefer a song name that points at this sequence
            name = f"seq{i:02d}"
            for s in self.mseq.songs:
                if s.seq_index == i:
                    name = s.name
                    break
            path = os.path.join(out_dir, f"{name}.mid")
            sequence_to_midi(seq, path)
            print(f"  [{i}] {path}")

    def replace_seq(self, seq_index: int, midi_path: str):
        if not 0 <= seq_index < len(self.seqg.sequences):
            raise IndexError(f"seq_index {seq_index} out of range")
        old = self.seqg.sequences[seq_index]
        new = midi_to_sequence(midi_path, master_volume=old.master_volume)
        # keep original tempo if the MIDI had no tempo meta
        if new.tempo_ms == 500_000 and old.tempo_ms:
            new.tempo_ms = old.tempo_ms
        self.seqg.sequences[seq_index] = new
        self._dirty = True
        print(f"Replaced sequence {seq_index} from {midi_path} "
              f"({new.bpm:.1f} BPM, "
              f"{sum(1 for t in new.tracks if t.events)} active tracks)")
        print("Call 'save' to write music.seq")

    def save(self):
        # Rebuild seq
        seq_bytes = self.seqg.write()
        with open(self.seq_path, "wb") as f:
            f.write(seq_bytes)
        print(f"Wrote {self.seq_path} ({len(seq_bytes)} bytes)")
        # inf is only rewritten if we ever mutate it; for now identity is fine
        # but rewrite anyway so offsets stay consistent with any future edits
        inf_bytes = self.mseq.write()
        with open(self.inf_path, "wb") as f:
            f.write(inf_bytes)
        print(f"Wrote {self.inf_path} ({len(inf_bytes)} bytes)")
        # inst identity
        with open(self.ins_path, "wb") as f:
            f.write(self.inst.write())
        print(f"Wrote {self.ins_path} ({self.inst.size} bytes) [unchanged]")
        self._dirty = False


def main(argv=None):
    p = argparse.ArgumentParser(description="GT3 sequenced music tool (Phases 1–3)")
    p.add_argument("-d", "--dir", default=".", help="directory containing music.inf/seq/ins")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list songs")
    sub.add_parser("info", help="show file summary")

    e = sub.add_parser("export-midi", help="export one sequence to MIDI")
    e.add_argument("seq_index", type=int)
    e.add_argument("output", nargs="?", default=None)

    ea = sub.add_parser("export-all-midi", help="export all sequences to MIDI")
    ea.add_argument("outdir", nargs="?", default="midi_out")

    r = sub.add_parser("replace-seq", help="replace a sequence from a MIDI file")
    r.add_argument("seq_index", type=int)
    r.add_argument("midi")

    sub.add_parser("save", help="write modified files back to disk")

    ei = sub.add_parser("extract-instruments", help="extract samples from .ins to WAV/VAG")
    ei.add_argument("outdir", nargs="?", default="instruments_out")

    sf = sub.add_parser("build-sf2", help="build SoundFont (.sf2) from .ins banks")
    sf.add_argument("outdir", nargs="?", default="sf2_out")

    args = p.parse_args(argv)

    if args.cmd == "extract-instruments":
        from gt3bgm.inst import Inst
        out = args.outdir
        d = args.dir
        ins_files = []
        pack = os.path.join(d, "music.ins")
        if os.path.isfile(pack):
            ins_files = [pack]
        else:
            ins_files = [
                os.path.join(d, f)
                for f in sorted(os.listdir(d))
                if f.lower().endswith(".ins") and os.path.isfile(os.path.join(d, f))
            ]
        if not ins_files:
            raise SystemExit(f"No .ins files found in {d}")
        for path in ins_files:
            inst = Inst.open(path)
            sub = os.path.join(out, os.path.splitext(os.path.basename(path))[0])
            written = inst.extract_all(sub, also_vag=True)
            print(f"{os.path.basename(path)}: {len(inst.samples)} samples → {sub}")
        return

    if args.cmd == "build-sf2":
        from gt3bgm.inst import Inst
        out = args.outdir
        os.makedirs(out, exist_ok=True)
        d = args.dir
        ins_files = []
        pack = os.path.join(d, "music.ins")
        if os.path.isfile(pack):
            ins_files = [pack]
        else:
            ins_files = [
                os.path.join(d, f)
                for f in sorted(os.listdir(d))
                if f.lower().endswith(".ins") and os.path.isfile(os.path.join(d, f))
            ]
        if not ins_files:
            raise SystemExit(f"No .ins files found in {d}")
        for path in ins_files:
            inst = Inst.open(path)
            sf2_path = os.path.join(out, os.path.splitext(os.path.basename(path))[0] + ".sf2")
            inst.extract_sf2(sf2_path)
            print(f"{os.path.basename(path)}: {len(inst.samples)} samples → {sf2_path}")
        return

    ms = MusicSet(args.dir)

    if args.cmd == "list":
        ms.list_songs()
    elif args.cmd == "info":
        ms.info()
    elif args.cmd == "export-midi":
        out = args.output or f"seq{args.seq_index:02d}.mid"
        ms.export_midi(args.seq_index, out)
    elif args.cmd == "export-all-midi":
        ms.export_all_midi(args.outdir)
    elif args.cmd == "replace-seq":
        ms.replace_seq(args.seq_index, args.midi)
        # auto-save for convenience
        ms.save()
    elif args.cmd == "save":
        ms.save()
    else:
        p.error(f"unknown command {args.cmd}")


if __name__ == "__main__":
    main()
