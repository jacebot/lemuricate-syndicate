#!/usr/bin/env python3
"""Syndicate -> Lemur bridge.

Sits between Syndicate and the Lemur template on your iPad:

  * Syndicate sends its OSC feedback here (Settings > OSC: feedback host 127.0.0.1, port 9096).
  * Every message is passed on to the iPad unchanged, so meters, titles, BPM and so on still work.
  * When a deck loads a track, the bridge finds the audio file in Syndicate's library, builds an
    overview waveform with ffmpeg, and sends it to the iPad as /Syndicate/Deck/<n>/Wave.
  * At startup it asks Syndicate to resend everything, so decks that are already loaded show up.

Run:
    python3 syndicate_lemur_bridge.py --ipad <your iPad's IP>

Needs Python 3 (standard library only) and ffmpeg (brew install ffmpeg).
"""
import argparse
import os
import socket
import sqlite3
import struct
import subprocess
import threading
import time

# How many bars the waveform has. The Lemur canvas draws one bar per value.
WAVE_POINTS = 128

# Syndicate's library database. Opened read-only, only to find a track's file on disk.
LIBRARY_DB = os.path.expanduser(
    '~/Library/Application Support/UntitledAudioSyndicate/Syndicate/Database/Syndicate.db')


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


# --------------------------------------------------------------------------------------------
# Just enough OSC to read Syndicate's messages and write our own.
# OSC packs everything into 4-byte blocks: strings are null-terminated and padded to 4 bytes.
# --------------------------------------------------------------------------------------------

def _read_string(buf, i):
    """Read a padded OSC string starting at i. Returns (text, index after the padding)."""
    end = buf.index(b'\0', i)
    return buf[i:end].decode('utf-8', 'replace'), (end + 4) & ~3


def parse_osc(buf, out):
    """Append (address, [args]) for every message in buf to out. Handles bundles too."""
    if buf.startswith(b'#bundle'):
        i = 16                                   # skip '#bundle' + 8-byte timetag
        while i + 4 <= len(buf):
            size = struct.unpack('>i', buf[i:i + 4])[0]
            parse_osc(buf[i + 4:i + 4 + size], out)
            i += 4 + size
        return

    address, i = _read_string(buf, 0)
    if i >= len(buf):                            # message with no arguments
        out.append((address, []))
        return

    tags, i = _read_string(buf, i)               # e.g. ',sssffiis'
    args = []
    for tag in tags[1:]:
        if tag == 'i':
            args.append(struct.unpack('>i', buf[i:i + 4])[0]); i += 4
        elif tag == 'f':
            args.append(struct.unpack('>f', buf[i:i + 4])[0]); i += 4
        elif tag == 's':
            text, i = _read_string(buf, i); args.append(text)
        elif tag in 'dh':                        # 64-bit values: not used by Syndicate, skip
            i += 8
        elif tag == 'b':                         # blob: skip
            size = struct.unpack('>i', buf[i:i + 4])[0]
            i += 4 + ((size + 3) & ~3)
    out.append((address, args))


def _osc_string(text):
    raw = text.encode() + b'\0'
    return raw + b'\0' * (-len(raw) % 4)


def osc_message(address, *values):
    """Build an OSC message. Ints become 'i', floats become 'f'."""
    tags = ',' + ''.join('i' if isinstance(v, int) else 'f' for v in values)
    body = b''.join(struct.pack('>i' if isinstance(v, int) else '>f', v) for v in values)
    return _osc_string(address) + _osc_string(tags) + body


# --------------------------------------------------------------------------------------------
# Finding the audio file and building the waveform
# --------------------------------------------------------------------------------------------

def find_audio_file(title, artist, duration):
    """Look the track up in Syndicate's library. TrackInfo gives title, artist and length,
    so match on title, prefer the same artist, then pick the closest length."""
    try:
        db = sqlite3.connect(f'file:{LIBRARY_DB}?mode=ro', uri=True, timeout=2)
        rows = db.execute(
            'SELECT FileLocation, COALESCE(Artist, ""), COALESCE(DurationSeconds, 0) '
            'FROM Tracks WHERE Title = ?', (title,)).fetchall()
        db.close()
    except Exception as e:
        log(f'library lookup failed: {e}')
        return None

    if artist:
        rows = [r for r in rows if r[1] == artist] or rows
    rows.sort(key=lambda r: abs(r[2] - duration))
    return rows[0][0] if rows else None


_waveform_cache = {}


def build_waveform(path):
    """Decode the file with ffmpeg and squeeze it into WAVE_POINTS bar heights between 0 and 1."""
    if path in _waveform_cache:
        return _waveform_cache[path]

    # Mono at 2 kHz is plenty for an overview, and decoding takes well under a second.
    sample_rate = 2000
    result = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', str(sample_rate), '-f', 's16le', '-'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode or not result.stdout:
        log(f'ffmpeg failed for {path}: {result.stderr.decode(errors="replace")[:200]}')
        return None

    count = len(result.stdout) // 2
    samples = struct.unpack(f'<{count}h', result.stdout[:count * 2])
    chunk = max(1, count // WAVE_POINTS)

    heights = []
    for k in range(WAVE_POINTS):
        part = samples[k * chunk:(k + 1) * chunk]
        if not part:
            heights.append(0.0)
            continue
        # Half peak, half loudness (RMS). That looks closer to a DJ-app waveform than raw peaks.
        peak = max(abs(min(part)), max(part)) / 32768
        every4 = part[::4]
        rms = (sum(x * x for x in every4) / len(every4)) ** 0.5 / 32768
        heights.append(0.5 * peak + 0.5 * min(1.0, rms * 2.2))

    # Modern masters are loud almost everywhere, so stretch the contrast to show breakdowns
    # and drops. The small floor keeps quiet parts visible as a thin line.
    lo, hi = min(heights), max(heights)
    span = (hi - lo) or 1.0
    wave = [round(0.06 + 0.94 * ((h - lo) / span) ** 1.8, 3) for h in heights]

    _waveform_cache[path] = wave
    return wave


# --------------------------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ipad', required=True, help="your iPad's IP address (where Lemur runs)")
    ap.add_argument('--ipad-port', type=int, default=8000, help="Lemur's OSC port (default 8000)")
    ap.add_argument('--listen', type=int, default=9096,
                    help="port Syndicate sends its feedback to (default 9096)")
    ap.add_argument('--syndicate-port', type=int, default=9090,
                    help="Syndicate's OSC input port, used for the resend request (default 9090)")
    ap.add_argument('--no-resend', action='store_true',
                    help="don't ask Syndicate to resend its state at startup")
    ap.add_argument('--log', metavar='TEXT',
                    help='print every Syndicate message whose address contains TEXT (for debugging)')
    args = ap.parse_args()

    inbox = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    inbox.bind(('127.0.0.1', args.listen))
    outbox = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ipad = (args.ipad, args.ipad_port)
    log(f'listening on 127.0.0.1:{args.listen}, forwarding to {ipad[0]}:{ipad[1]}')

    # Syndicate only sends values when they change. Asking for a resend means decks that are
    # already loaded get their title, BPM, cues and (through us) waveform right away.
    if not args.no_resend:
        outbox.sendto(osc_message('/Syndicate/Feedback/Resend', 1), ('127.0.0.1', args.syndicate_port))
        log(f'asked Syndicate (127.0.0.1:{args.syndicate_port}) to resend its state')

    def send_waveform(deck, title, artist, duration, kind):
        # Runs in its own thread so a slow decode never holds up the meters.
        address = f'/Syndicate/Deck/{deck}/Wave'
        if kind == 'empty' or not title:
            outbox.sendto(osc_message(address, 0.0), ipad)      # clears the deck's waveform
            return
        path = find_audio_file(title, artist, duration)
        if not path or not os.path.exists(path):
            log(f'deck {deck}: no audio file found for "{title}" ({path})')
            return
        started = time.time()
        wave = build_waveform(path)
        if wave:
            outbox.sendto(osc_message(address, *wave), ipad)
            log(f'deck {deck}: waveform sent for "{title}" ({time.time() - started:.1f}s)')

    while True:
        packet, _ = inbox.recvfrom(65535)
        outbox.sendto(packet, ipad)                  # pass everything through untouched

        messages = []
        try:
            parse_osc(packet, messages)
        except Exception:
            continue

        for address, values in messages:
            if args.log and args.log in address:
                log(f'{address} {values}')

            # /Syndicate/Deck/<n>/TrackInfo  ,sssffiis
            #   title, artist, key, bpm, length in seconds, bars, beats, 'track' or 'empty'
            parts = address.split('/')
            if len(parts) == 5 and parts[2] == 'Deck' and parts[4] == 'TrackInfo' and len(values) >= 8:
                deck = parts[3]
                threading.Thread(target=send_waveform, daemon=True,
                                 args=(deck, values[0], values[1], float(values[4]), values[7])).start()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
