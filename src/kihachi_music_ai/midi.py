from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .theory import key_signature_value

PPQ = 480


@dataclass(frozen=True)
class MidiNote:
    pitch: int
    start_beats: float
    duration_beats: float
    velocity: int
    channel: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.pitch <= 127:
            raise ValueError("MIDI pitch must be between 0 and 127")
        if self.start_beats < 0 or self.duration_beats <= 0:
            raise ValueError("MIDI timing must be non-negative with positive duration")
        if not 1 <= self.velocity <= 127:
            raise ValueError("MIDI velocity must be between 1 and 127")
        if not 0 <= self.channel <= 15:
            raise ValueError("MIDI channel must be between 0 and 15")


@dataclass(frozen=True)
class MidiFileInfo:
    format_type: int
    track_count: int
    ppq: int
    track_bytes: int


@dataclass(frozen=True)
class MidiReadResult:
    """What a format-0 file actually contains, decoded back to notes."""

    notes: tuple[MidiNote, ...]
    ppq: int
    track_name: str | None = None
    bpm: float | None = None


def _vlq(value: int) -> bytes:
    if value < 0:
        raise ValueError("VLQ cannot encode a negative value")
    buffer = value & 0x7F
    result = bytearray([buffer])
    while value >> 7:
        value >>= 7
        buffer = (value & 0x7F) | 0x80
        result.insert(0, buffer)
    return bytes(result)


def _meta(meta_type: int, payload: bytes) -> bytes:
    return b"\xFF" + bytes([meta_type]) + _vlq(len(payload)) + payload


def build_midi_bytes(
    notes: Iterable[MidiNote],
    *,
    track_name: str,
    bpm: float,
    key: str,
) -> bytes:
    if not 30.0 <= bpm <= 300.0:
        raise ValueError("bpm must be between 30 and 300")
    events: list[tuple[int, int, bytes]] = []
    name_payload = track_name.encode("utf-8")
    events.append((0, 0, _meta(0x03, name_payload)))
    tempo = int(round(60_000_000 / bpm))
    events.append((0, 0, _meta(0x51, tempo.to_bytes(3, "big"))))
    events.append((0, 0, _meta(0x58, bytes((4, 2, 24, 8)))))
    signature, is_minor = key_signature_value(key)
    events.append((0, 0, _meta(0x59, struct.pack("bb", signature, is_minor))))

    for note in notes:
        start = int(round(note.start_beats * PPQ))
        end = int(round((note.start_beats + note.duration_beats) * PPQ))
        end = max(start + 1, end)
        events.append((start, 2, bytes((0x90 | note.channel, note.pitch, note.velocity))))
        events.append((end, 1, bytes((0x80 | note.channel, note.pitch, 0))))

    events.sort(key=lambda item: (item[0], item[1], item[2]))
    track = bytearray()
    previous_tick = 0
    for tick, _priority, payload in events:
        track.extend(_vlq(tick - previous_tick))
        track.extend(payload)
        previous_tick = tick
    track.extend(b"\x00\xFF\x2F\x00")

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, PPQ)
    track_chunk = b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    return header + track_chunk


def write_midi(
    path: Path,
    notes: Iterable[MidiNote],
    *,
    track_name: str,
    bpm: float,
    key: str,
) -> None:
    path.write_bytes(build_midi_bytes(notes, track_name=track_name, bpm=bpm, key=key))


def read_midi(path: Path) -> MidiReadResult:
    """Decode a format-0 file back into notes.

    Reading the artifact rather than recomposing it from the SongSpec is what
    lets the MIDI review check what is actually on disk.
    """

    payload = Path(path).read_bytes()
    info = inspect_midi(Path(path))
    if info.format_type != 0:
        raise ValueError(f"only format 0 MIDI is supported, got format {info.format_type}")
    if info.ppq <= 0:
        raise ValueError("SMPTE time division is not supported")

    track = payload[22 : 22 + info.track_bytes]
    cursor = 0
    tick = 0
    status = 0
    track_name: str | None = None
    bpm: float | None = None
    open_notes: dict[tuple[int, int], list[tuple[int, int]]] = {}
    notes: list[MidiNote] = []

    while cursor < len(track):
        delta, cursor = _read_vlq(track, cursor)
        tick += delta
        if cursor >= len(track):
            raise ValueError("truncated MIDI event")
        byte = track[cursor]
        if byte == 0xFF:
            cursor += 1
            meta_type = track[cursor]
            cursor += 1
            length, cursor = _read_vlq(track, cursor)
            data = track[cursor : cursor + length]
            cursor += length
            if meta_type == 0x03:
                track_name = data.decode("utf-8", errors="replace")
            elif meta_type == 0x51 and length == 3:
                microseconds = int.from_bytes(data, "big")
                bpm = round(60_000_000 / microseconds, 6) if microseconds else None
            elif meta_type == 0x2F:
                break
            continue
        if byte in {0xF0, 0xF7}:
            cursor += 1
            length, cursor = _read_vlq(track, cursor)
            cursor += length
            continue
        if byte & 0x80:
            status = byte
            cursor += 1
        elif not status:
            raise ValueError("MIDI running status used before any status byte")
        command = status & 0xF0
        channel = status & 0x0F
        if command in {0xC0, 0xD0}:
            cursor += 1
            continue
        first = track[cursor]
        second = track[cursor + 1]
        cursor += 2
        if command == 0x90 and second > 0:
            open_notes.setdefault((channel, first), []).append((tick, second))
        elif command == 0x80 or (command == 0x90 and second == 0):
            pending = open_notes.get((channel, first))
            if pending:
                start_tick, velocity = pending.pop(0)
                notes.append(
                    MidiNote(
                        pitch=first,
                        start_beats=start_tick / info.ppq,
                        duration_beats=max(tick - start_tick, 1) / info.ppq,
                        velocity=velocity,
                        channel=channel,
                    )
                )
    if any(pending for pending in open_notes.values()):
        raise ValueError("MIDI track ended with a note still held")
    notes.sort(key=lambda note: (note.start_beats, note.pitch, note.channel))
    return MidiReadResult(tuple(notes), info.ppq, track_name, bpm)


def _read_vlq(payload: bytes, cursor: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if cursor >= len(payload):
            raise ValueError("truncated MIDI variable-length quantity")
        byte = payload[cursor]
        cursor += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, cursor
    raise ValueError("MIDI variable-length quantity is too long")


def inspect_midi(path: Path) -> MidiFileInfo:
    payload = path.read_bytes()
    if len(payload) < 22 or payload[:4] != b"MThd" or payload[14:18] != b"MTrk":
        raise ValueError(f"not a supported MIDI file: {path}")
    header_length, format_type, tracks, division = struct.unpack(">IHHH", payload[4:14])
    if header_length != 6:
        raise ValueError("unsupported MIDI header length")
    track_bytes = struct.unpack(">I", payload[18:22])[0]
    if len(payload) != 22 + track_bytes:
        raise ValueError("MIDI track length does not match file size")
    return MidiFileInfo(format_type, tracks, division, track_bytes)

