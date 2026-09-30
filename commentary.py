#!/usr/bin/env python3
"""AI Hindi cricket commentator for the live scoreboard stream.

Polls Cricbuzz via scoreboard_v2.fetch_rich, detects newly bowled balls
from recent_overs, and synthesizes Hindi commentary with edge-tts
(voice hi-IN-MadhurNeural). Runs in a background thread and exposes a
thread-safe queue (q) of stereo s16le/44100 PCM byte chunks.

If TTS is unavailable for any reason, commentary degrades to silence and
the stream continues video-only.
"""
import os
import queue
import random
import re
import subprocess
import tempfile
import threading
import time

from scoreboard_v2 import fetch_rich

VOICE = "hi-IN-MadhurNeural"
POLL_SEC = 15
SAMPLE_RATE = 44100
MAX_PENDING = 6          # drop new clips beyond this (avoid stale backlog)
SILENCE_PAD = 0.35       # seconds of silence around each clip

_TOK_RE = re.compile(r"^(?:[0-6]|w|wd|nb|b|lb|by)$", re.I)

LINES = {
    "4": [
        "Chauka! {bat} ne {bowl} ki gend ko seema rekha ke paar bhej diya! Chaar run!",
        "Kya khoobsurat shot hai! {bat} ke balle se nikla ye chauka!",
        "Chaaron khane chitt! {bat} ka shandaar chauka!",
    ],
    "6": [
        "Chhakka! {bat} ka vishaal chhakka! Gend seedhi darshakon mein ja giri!",
        "Kya shot hai! {bat} ne {bowl} ki gend ko stand ke paar pahunchaya! Chhe run!",
        "Lamba chhakka! {bat} ne dikhaya apna dam!",
    ],
    "w": [
        "Wicket! {bat} out ho gaye! {bowl} ko mili ye keemti wicket!",
        "Badi safalta! {bat} ko pavilion ka rasta dikhaya {bowl} ne!",
    ],
    "0": [
        "Koi run nahi. {bowl} ki kasi hui gend.",
        "Behtareen gend {bowl} ki, {bat} beaten hue.",
        "Dot ball! Dabav badhta hua ballebazon par.",
    ],
    "1": [
        "Ek run, {bat} ne halke haathon se khela.",
        "Ek run ka izafa score mein.",
    ],
    "2": [
        "Do run! Wicketon ke beech tez daud.",
        "Achhi running, do run jod liye {bat} ne.",
    ],
    "3": [
        "Teen run! Gend seema rekha se just pehle ruk gayi.",
    ],
    "wd": [
        "Wide gend, atirikt run milega.",
        "Line se bhatke {bowl}, wide ka ishara umpire ka.",
    ],
    "nb": [
        "No ball! Atirikt run, aur agli gend free hit hogi!",
        "Umpire ka ishara, ye no ball hai!",
    ],
    "b": [
        "Bye ke roop mein run mil gaya.",
        "Gend balle ko chhue bina nikal gayi.",
    ],
    "lb": [
        "Leg bye ka run.",
        "Pad se takrakar gend nikli, leg bye ka ishara.",
    ],
    "by": [
        "Bye ka run.",
    ],
}


def _norm_token(tok):
    t = tok.strip().lower()
    return t if _TOK_RE.match(t) else None


def _flatten(recent_overs):
    """recent_overs -> ordered list of (event_id, token)."""
    evs = []
    for over_no, toks in recent_overs or []:
        for idx, tok in enumerate(toks):
            evs.append((f"{over_no}.{idx}:{tok}", tok))
    return evs


def _tts_pcm(text):
    """Hindi text -> stereo s16le 44100 PCM bytes, or None on any failure."""
    mp3 = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            mp3 = f.name
        cp = subprocess.run(
            ["edge-tts", "--voice", VOICE, "--text", text, "--write-media", mp3],
            capture_output=True, timeout=40)
        if cp.returncode != 0 or not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
            return None
        cp2 = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", mp3,
             "-filter:a", "volume=1.5", "-f", "s16le",
             "-ar", str(SAMPLE_RATE), "-ac", "2", "-"],
            capture_output=True, timeout=40)
        return cp2.stdout or None
    except Exception:
        return None
    finally:
        if mp3:
            try:
                os.unlink(mp3)
            except Exception:
                pass


def _silence(seconds):
    n = int(SAMPLE_RATE * seconds)
    return b"\x00" * (n * 4)  # stereo s16le


class Commentator(threading.Thread):
    """Background Hindi commentator. q holds PCM byte chunks."""

    def __init__(self, cricbuzz_url):
        super().__init__(daemon=True)
        self.url = cricbuzz_url
        self.q = queue.Queue()
        self._seen = set()
        self._max_over = -1
        self._sum_over = -1
        self._team = ""
        self._welcomed = False
        self._end_announced = False

    # -- helpers ------------------------------------------------------
    def _say(self, text):
        if self.q.qsize() >= MAX_PENDING:
            return  # avoid stale backlog
        pcm = _tts_pcm(text)
        if pcm:
            self.q.put(_silence(SILENCE_PAD) + pcm + _silence(SILENCE_PAD))
            print(f"commentary: {text[:80]}", flush=True)

    def _names(self, st):
        batters = st.get("batters") or []
        bowlers = st.get("bowlers") or []
        bat = next((b["name"] for b in batters if b.get("striker")),
                   batters[0]["name"] if batters else "ballebaz")
        bowl = next((b["name"] for b in bowlers if b.get("cur")),
                    bowlers[0]["name"] if bowlers else "gendbaaz")
        return bat, bowl

    # -- main loop ----------------------------------------------------
    def run(self):
        while True:
            try:
                self._poll()
            except Exception as e:
                print(f"commentator poll failed: {e}", flush=True)
            time.sleep(POLL_SEC)

    def _poll(self):
        st = fetch_rich(self.url)
        if not st.get("runs"):
            return
        team, runs, wkts, overs = st["team"], st["runs"], st["wkts"], st["overs"]
        recent = st.get("recent_overs", [])
        try:
            cur_max = max(o for o, _ in recent)
        except ValueError:
            cur_max = -1

        if not self._welcomed:
            self._welcomed = True
            self._team = team
            self._max_over = cur_max
            self._sum_over = cur_max
            self._seen = {eid for eid, _ in _flatten(recent)}
            title = st.get("match_title") or "is mukable"
            self._say(f"Namaskar doston! {title} mein AI Hindi commentary ke saath "
                      f"aapka swagat hai. Taaza score hai {team}, {runs} par {wkts}, "
                      f"{overs} over mein.")
            return

        # new innings?
        if team != self._team or (self._max_over > 0 and cur_max < self._max_over - 3):
            self._team = team
            self._max_over = cur_max
            self._sum_over = cur_max
            self._seen = {eid for eid, _ in _flatten(recent)}
            self._end_announced = False
            self._say(f"Nayi pari ka aaghaaz! {team} ki ballebazi shuru ho chuki hai.")
            return
        self._max_over = max(self._max_over, cur_max)

        # match end?
        status = (st.get("status") or "").lower()
        if not self._end_announced and any(k in status for k in ("won by", "tied", "drawn")):
            self._end_announced = True
            self._say(f"Match samapt! {st.get('status')}. "
                      f"AI Hindi commentary mein judne ke liye dhanyavaad!")
            return

        # new balls -> ball-by-ball commentary
        bat, bowl = self._names(st)
        for eid, tok in _flatten(recent):
            if eid in self._seen:
                continue
            self._seen.add(eid)
            norm = _norm_token(tok)
            if not norm:
                continue
            line = random.choice(LINES[norm]).format(bat=bat, bowl=bowl)
            self._say(line)
        if len(self._seen) > 400:
            self._seen = set(list(self._seen)[-200:])

        # over summary when a fresh over number appears
        if cur_max > self._sum_over:
            self._sum_over = cur_max
            self._say(f"{cur_max - 1} over ki samapti. Score hai {team}, "
                      f"{runs} par {wkts}.")
