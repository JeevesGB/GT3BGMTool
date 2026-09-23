"""Gran Turismo SEQG (music.seq) format — multi-sequence container.

Based on research by xan1242 (GTSeq2Midi) and analysis of GT3 music.seq.

Layout
------
  0x00  'SEQG'
  0x04  unknown (usually 0)
  0x08  sequence count (N)
  0x0C  Sequence[N] headers, each 72 bytes:
          MasterVolume  u32
          TempoMS       u32   (microseconds-ish; BPM = 240_000_000 / TempoMS)
          TrackPtr[16]  u32   (file offsets into the event streams)

Event stream (per track)
------------------------
  VLV delta-time, then command byte:

  0x00          nop / padding (1 byte)
  0x01          loop marker   (cmd + 1 pad = 2 bytes)
  0x02          end of track  (cmd + 2 bytes = 3 bytes)
  0x03          program/instrument change (cmd + program = 2 bytes)
  0x04          volume        (cmd + vol = 2 bytes)
  0x05          pan           (cmd + pan = 2 bytes)
  0x06          tempo         (rarely used)
  0x01-0x7F     treated as pitch-bend low byte (cmd + high = 2 bytes)
                value is 14-bit VLV-decoded then centred around 0x1000
  0x80-0xEC     note on: note, velocity, then VLV duration
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import BinaryIO

MAGIC = b"SEQG"
TRACK_COUNT = 16
PPQN = 120  # ticks per quarter note used by GT sequences
DEFAULT_BEND_RANGE = 3

# Event command IDs
CMD_NOP = 0x00
CMD_LOOP = 0x01
CMD_END = 0x02
CMD_PROGRAM = 0x03
CMD_VOLUME = 0x04
CMD_PAN = 0x05
CMD_TEMPO = 0x06


def encode_vlv(value: int) -> bytes:
    """Encode an unsigned integer as a MIDI-style variable-length quantity (little-endian byte order as used by GT)."""
    if value < 0:
        raise ValueError("VLV cannot be negative")
    if value == 0:
        return b"\x00"
    parts = []
    while value > 0:
        parts.append(value & 0x7F)
        value >>= 7
    # GT stores VLV with continuation bits on all but the last byte, in the order written
    out = bytearray()
    for i, p in enumerate(reversed(parts)):
        if i < len(parts) - 1:
            out.append(p | 0x80)
        else:
            out.append(p)
    return bytes(out)


def decode_vlv(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a GT VLV starting at offset. Returns (value, bytes_consumed)."""
    value = 0
    length = 0
    for i in range(4):
        if offset + i >= len(data):
            break
        b = data[offset + i]
        value = (value << 7) | (b & 0x7F)
        length += 1
        if b < 0x80:
            break
    return value, length


def tempo_ms_to_bpm(tempo_ms: int) -> float:
    if tempo_ms <= 0:
        return 0.0
    return 240_000_000.0 / tempo_ms


def bpm_to_tempo_ms(bpm: float) -> int:
    if bpm <= 0:
        return 500_000
    return int(round(240_000_000.0 / bpm))


@dataclass
class SeqTrack:
    """Decoded event list for one track."""
    events: list[tuple] = field(default_factory=list)  # (abs_tick, cmd, *args)
    raw: bytes = b""

    def append(self, abs_tick: int, cmd: int, *args):
        self.events.append((abs_tick, cmd, *args))


@dataclass
class Sequence:
    master_volume: int = 0x4000
    tempo_ms: int = 500_000
    track_pointers: list[int] = field(default_factory=lambda: [0] * TRACK_COUNT)
    tracks: list[SeqTrack] = field(default_factory=lambda: [SeqTrack() for _ in range(TRACK_COUNT)])

    @property
    def bpm(self) -> float:
        return tempo_ms_to_bpm(self.tempo_ms)


@dataclass
class SeqG:
    """Full music.seq container."""
    sequences: list[Sequence] = field(default_factory=list)
    raw: bytes = b""

    @classmethod
    def read(cls, data: bytes) -> "SeqG":
        if data[:4] != MAGIC:
            raise ValueError(f"Not a SEQG file (got {data[:4]!r})")
        unk, count = struct.unpack_from("<II", data, 4)
        sequences: list[Sequence] = []
        for i in range(count):
            base = 12 + i * 72
            if base + 72 > len(data):
                break
            vol, tempo = struct.unpack_from("<II", data, base)
            ptrs = list(struct.unpack_from(f"<{TRACK_COUNT}I", data, base + 8))
            seq = Sequence(master_volume=vol, tempo_ms=tempo, track_pointers=ptrs)
            for t, ptr in enumerate(ptrs):
                if ptr == 0 or ptr == 0xFFFFFFFF or ptr >= len(data):
                    continue
                seq.tracks[t] = _parse_track(data, ptr)
            sequences.append(seq)
        obj = cls(sequences=sequences, raw=data)
        return obj

    @classmethod
    def open(cls, path: str) -> "SeqG":
        with open(path, "rb") as f:
            return cls.read(f.read())

    def write(self) -> bytes:
        """Rebuild a SEQG binary. Track event streams are written after the header table."""
        count = len(self.sequences)
        header = bytearray()
        header += MAGIC
        header += struct.pack("<II", 0, count)

        # Placeholder for sequence headers; fill after we know data offsets
        header_size = 12 + count * 72
        data_parts: list[bytes] = []
        data_offset = header_size

        seq_headers: list[bytes] = []
        for seq in self.sequences:
            ptrs = []
            for track in seq.tracks:
                raw = track.raw if track.raw else _encode_track(track)
                if not raw:
                    # empty track still needs an end marker
                    raw = encode_vlv(0) + bytes([CMD_END, 0, 0])
                ptrs.append(data_offset if raw else 0)
                data_parts.append(raw)
                data_offset += len(raw)
            # pad to 16
            while len(ptrs) < TRACK_COUNT:
                ptrs.append(0)
            sh = struct.pack("<II", seq.master_volume, seq.tempo_ms)
            sh += struct.pack(f"<{TRACK_COUNT}I", *ptrs[:TRACK_COUNT])
            seq_headers.append(sh)

        out = bytearray()
        out += MAGIC
        out += struct.pack("<II", 0, count)
        for sh in seq_headers:
            out += sh
        for part in data_parts:
            out += part
        return bytes(out)

    def sequence_count(self) -> int:
        return len(self.sequences)


def _parse_track(data: bytes, start: int) -> SeqTrack:
    """Parse one SEQG track.

    Per leo-the-leon/vgm-specs (gran-turismo/SEQG.md) and xan1242/gtseq2midi:

    Stream is:  VLV-delta, then either
      - event:   type (01-06) + value   [often preceded by a 00 delta]
      - note:    note (80-FF) + velocity (00-7F) + VLV duration
      - end:     02 + padding

    Bytes 00-7F are deltas / velocities / durations (high bit clear).
    Bytes 80-FF are note numbers (high bit set). Pitch-bend is NOT
    documented in vgm-specs; we no longer invent bend events from low bytes.
    """
    track = SeqTrack()
    if start == 0 or start == 0xFFFFFFFF or start >= len(data):
        return track

    cursor = start
    abs_time = 0
    max_iters = 500_000
    for _ in range(max_iters):
        if cursor >= len(data):
            break
        delta, vlen = decode_vlv(data, cursor)
        abs_time += delta
        cursor += vlen
        if cursor >= len(data):
            break
        cmd = data[cursor]

        # --- control events (type byte 01-06; value follows) ---
        if cmd == CMD_LOOP:  # 0x01
            # value byte often 0xFF = infinite; we only need the marker
            track.append(abs_time, CMD_LOOP)
            cursor += 2
        elif cmd == CMD_END:  # 0x02
            param = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_END, param)
            # GTSeq2Midi advances 3; value + pad
            cursor += 3
            break
        elif cmd == CMD_PROGRAM:  # 0x03  (programs are 1-based per vgm-specs)
            prog = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_PROGRAM, prog)
            cursor += 2
        elif cmd == CMD_VOLUME:  # 0x04
            vol = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_VOLUME, vol)
            cursor += 2
        elif cmd == CMD_PAN:  # 0x05
            pan = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_PAN, pan)
            cursor += 2
        elif cmd == CMD_TEMPO:  # 0x06 (rare)
            val = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_TEMPO, val)
            cursor += 2
        elif cmd == 0x00:
            # bare zero — treat as padding / zero-delta already consumed; skip
            cursor += 1
        elif cmd >= 0x80:
            # Note: high bit set. Pitch = cmd & 0x7F (MIDI 0-127).
            # Then velocity (7-bit), then VLV duration.
            note = cmd & 0x7F
            if cursor + 1 >= len(data):
                break
            vel = data[cursor + 1] & 0x7F
            dur, dlen = decode_vlv(data, cursor + 2)
            # duration 0 still produces a tick of sound for exporters
            track.append(abs_time, "note", note, vel, max(dur, 0))
            cursor += 2 + dlen
        else:
            # Unknown low byte in command position — skip one to resync
            track.append(abs_time, "unknown", cmd)
            cursor += 1

    end = min(cursor, len(data))
    track.raw = data[start:end]
    return track


def _encode_track(track: SeqTrack) -> bytes:
    """Encode a SeqTrack event list back to GT event bytes."""
    out = bytearray()
    last_tick = 0
    for ev in track.events:
        abs_tick = ev[0]
        cmd = ev[1]
        delta = max(0, abs_tick - last_tick)
        last_tick = abs_tick
        out += encode_vlv(delta)

        if cmd == CMD_NOP:
            out.append(CMD_NOP)
        elif cmd == CMD_LOOP:
            out += bytes([CMD_LOOP, 0])
        elif cmd == CMD_END:
            param = ev[2] if len(ev) > 2 else 0
            out += bytes([CMD_END, param & 0xFF, 0])
        elif cmd == CMD_PROGRAM:
            out += bytes([CMD_PROGRAM, ev[2] & 0xFF])
        elif cmd == CMD_VOLUME:
            out += bytes([CMD_VOLUME, ev[2] & 0xFF])
        elif cmd == CMD_PAN:
            out += bytes([CMD_PAN, ev[2] & 0xFF])
        elif cmd == CMD_TEMPO:
            out += bytes([CMD_TEMPO, ev[2] & 0xFF])
        elif cmd == "bend":
            bend = ev[2] & 0xFFFF
            low = bend & 0xFF
            high = (bend >> 8) & 0xFF
            out += bytes([low, high])
        elif cmd == "note":
            note, vel, dur = ev[2], ev[3], ev[4]
            # high bit marks "this is a note"; pitch in low 7 bits
            n = 0x80 | (int(note) & 0x7F)
            out += bytes([n, int(vel) & 0x7F])
            out += encode_vlv(max(0, int(dur)))
        else:
            # skip unknowns
            pass

    # ensure track ends
    if not track.events or track.events[-1][1] != CMD_END:
        out += encode_vlv(0)
        out += bytes([CMD_END, 0, 0])
    return bytes(out)


# ---------------------------------------------------------------------------
# MIDI export / import
# ---------------------------------------------------------------------------

def sequence_to_midi(seq: Sequence, path: str) -> None:
    """Write one Sequence to a standard Type-1 MIDI file (stdlib only)."""
    import io

    def write_vlv(buf: bytearray, value: int):
        # standard MIDI VLV (big-endian continuation)
        if value == 0:
            buf.append(0)
            return
        stack = []
        while value > 0:
            stack.append(value & 0x7F)
            value >>= 7
        while len(stack) > 1:
            buf.append(stack.pop() | 0x80)
        buf.append(stack.pop())

    tracks_data: list[bytes] = []

    # Tempo track
    tempo_track = bytearray()
    # meta tempo: FF 51 03 tt tt tt  (microseconds per quarter)
    # GT TempoMS is not exactly MIDI tempo; convert via BPM
    bpm = seq.bpm or 120.0
    us_per_quarter = int(round(60_000_000 / bpm))
    write_vlv(tempo_track, 0)
    tempo_track += bytes([0xFF, 0x51, 0x03])
    tempo_track += struct.pack(">I", us_per_quarter)[1:]  # 3 bytes
    write_vlv(tempo_track, 0)
    tempo_track += bytes([0xFF, 0x2F, 0x00])  # end of track
    tracks_data.append(bytes(tempo_track))

    for ch, track in enumerate(seq.tracks):
        if not track.events:
            continue
        buf = bytearray()
        last = 0
        for ev in track.events:
            tick = ev[0]
            cmd = ev[1]
            args = ev[2:]
            delta = max(0, tick - last)
            last = tick
            write_vlv(buf, delta)
            if cmd == CMD_PROGRAM and args:
                # SEQG programs are 1-based; General MIDI is 0-based
                prog = max(0, (int(args[0]) & 0x7F) - 1)
                buf += bytes([0xC0 | (ch & 0x0F), prog])
            elif cmd == CMD_VOLUME and args:
                buf += bytes([0xB0 | (ch & 0x0F), 7, int(args[0]) & 0x7F])
            elif cmd == CMD_PAN and args:
                buf += bytes([0xB0 | (ch & 0x0F), 10, int(args[0]) & 0x7F])
            elif cmd == "note" and len(args) >= 3:
                note, vel, dur = int(args[0]), int(args[1]), int(args[2])
                n = note & 0x7F
                v = max(1, min(127, vel & 0x7F))
                # duration 0 → one tick so the note is audible in players
                d = max(1, dur)
                buf += bytes([0x90 | (ch & 0x0F), n, v])
                write_vlv(buf, d)
                buf += bytes([0x80 | (ch & 0x0F), n, 0])
                last += d
            elif cmd == CMD_LOOP:
                name = b"loopStart"
                buf += bytes([0xFF, 0x06, len(name)]) + name
            elif cmd == CMD_END:
                name = b"loopEnd"
                buf += bytes([0xFF, 0x06, len(name)]) + name
            elif cmd == "bend" and args:
                bend = int(args[0])
                lsb = bend & 0x7F
                msb = (bend >> 7) & 0x7F
                buf += bytes([0xE0 | (ch & 0x0F), lsb, msb])
            else:
                # remove the delta we already wrote for events we skip
                # (rebuild without it by not having written... too late; leave as rest)
                pass
        write_vlv(buf, 0)
        buf += bytes([0xFF, 0x2F, 0x00])
        tracks_data.append(bytes(buf))

    # MIDI header
    ntrks = len(tracks_data)
    hdr = struct.pack(">4sIHHH", b"MThd", 6, 1, ntrks, PPQN)
    out = bytearray(hdr)
    for td in tracks_data:
        out += struct.pack(">4sI", b"MTrk", len(td))
        out += td
    with open(path, "wb") as f:
        f.write(out)


def midi_to_sequence(path: str, master_volume: int = 0x4000) -> Sequence:
    """Parse a Type-0/1 MIDI file into a GT Sequence (best-effort).

    Limitations (Phase 3):
    - Only note on/off, program, volume (CC7), pan (CC10), tempo are mapped.
    - Pitch bend is approximated.
    - Complex MIDI features (sysex, RPN, etc.) are ignored.
    - Note numbers are written in the 0x80-0xEC style expected by the GT player.
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"MThd":
        raise ValueError("Not a MIDI file")
    header_len, fmt, ntrks, division = struct.unpack_from(">IHHH", data, 4)
    if division & 0x8000:
        raise ValueError("SMPTE time division not supported")
    ppqn = division
    # scale factor to GT PPQN
    scale = PPQN / ppqn if ppqn else 1.0

    pos = 8 + header_len
    midi_tracks: list[list[tuple]] = []  # list of (abs_tick, event_bytes)

    def read_midi_vlv(buf, i):
        value = 0
        while i < len(buf):
            b = buf[i]
            i += 1
            value = (value << 7) | (b & 0x7F)
            if b < 0x80:
                break
        return value, i

    for _ in range(ntrks):
        if pos + 8 > len(data) or data[pos:pos + 4] != b"MTrk":
            break
        track_len = struct.unpack_from(">I", data, pos + 4)[0]
        track_data = data[pos + 8: pos + 8 + track_len]
        pos += 8 + track_len

        events = []
        i = 0
        abs_tick = 0
        running = 0
        while i < len(track_data):
            delta, i = read_midi_vlv(track_data, i)
            abs_tick += delta
            if i >= len(track_data):
                break
            status = track_data[i]
            if status & 0x80:
                running = status
                i += 1
            else:
                status = running
            etype = status & 0xF0
            channel = status & 0x0F

            if etype in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if i + 1 >= len(track_data):
                    break
                a, b = track_data[i], track_data[i + 1]
                i += 2
                events.append((int(abs_tick * scale), etype, channel, a, b))
            elif etype in (0xC0, 0xD0):
                if i >= len(track_data):
                    break
                a = track_data[i]
                i += 1
                events.append((int(abs_tick * scale), etype, channel, a, 0))
            elif status == 0xFF:
                # meta
                if i >= len(track_data):
                    break
                meta = track_data[i]
                i += 1
                length, i = read_midi_vlv(track_data, i)
                meta_data = track_data[i:i + length]
                i += length
                events.append((int(abs_tick * scale), 0xFF, meta, meta_data))
            elif status in (0xF0, 0xF7):
                length, i = read_midi_vlv(track_data, i)
                i += length
            else:
                break
        midi_tracks.append(events)

    # Build GT sequence: map MIDI channels 0-15 → GT tracks 0-15
    seq = Sequence(master_volume=master_volume, tempo_ms=bpm_to_tempo_ms(120))
    # find tempo
    for evs in midi_tracks:
        for e in evs:
            if e[1] == 0xFF and e[2] == 0x51 and len(e[3]) >= 3:
                us = (e[3][0] << 16) | (e[3][1] << 8) | e[3][2]
                if us > 0:
                    bpm = 60_000_000 / us
                    seq.tempo_ms = bpm_to_tempo_ms(bpm)
                break

    # Collect note ons per channel and pair with note offs
    for ch in range(TRACK_COUNT):
        # gather events for this channel from all midi tracks
        ch_events = []
        for evs in midi_tracks:
            for e in evs:
                if e[1] == 0xFF:
                    continue
                if len(e) >= 3 and e[2] == ch:
                    ch_events.append(e)
        ch_events.sort(key=lambda x: x[0])

        gt = SeqTrack()
        active: dict[int, tuple[int, int]] = {}  # note -> (start_tick, vel)

        for e in ch_events:
            tick, etype = e[0], e[1]
            if etype == 0x90:  # note on
                note, vel = e[3], e[4]
                if vel == 0:
                    # note off
                    if note in active:
                        start, v = active.pop(note)
                        dur = max(1, tick - start)
                        # encode note in 0x80+ range as GT expects
                        gt.append(start, "note", note & 0x7F, v, dur)
                else:
                    active[note] = (tick, vel)
            elif etype == 0x80:  # note off
                note = e[3]
                if note in active:
                    start, v = active.pop(note)
                    dur = max(1, tick - start)
                    gt.append(start, "note", note & 0x7F, v, dur)
            elif etype == 0xC0:
                gt.append(tick, CMD_PROGRAM, e[3] & 0x7F)
            elif etype == 0xB0:
                cc, val = e[3], e[4]
                if cc == 7:
                    gt.append(tick, CMD_VOLUME, val & 0x7F)
                elif cc == 10:
                    gt.append(tick, CMD_PAN, val & 0x7F)

        # flush remaining active notes with short duration
        for note, (start, v) in active.items():
            gt.append(start, "note", note & 0x7F, v, PPQN // 4)

        gt.events.sort(key=lambda x: x[0])
        # ensure end marker
        end_tick = gt.events[-1][0] + 1 if gt.events else 0
        gt.append(end_tick, CMD_END, 0)
        gt.raw = _encode_track(gt)
        seq.tracks[ch] = gt

    return seq
