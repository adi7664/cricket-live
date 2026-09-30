#!/usr/bin/env python3
"""AI Hindi cricket commentator for the live scoreboard stream.

Polls Cricbuzz via scoreboard_v2.fetch_rich every POLL_SEC, detects newly
bowled balls from recent_overs, and speaks ball-by-ball Hindi commentary
plus filler lines (score, partnership, bowler figures, win predictor) so
the audio keeps running continuously like a real commentator.

Architecture (decoupled, non-blocking):
  - Commentator thread (poll): only enqueues TEXT into text_q — never
    blocks on TTS, so no ball is ever missed.
  - synth worker thread: takes text, runs edge-tts (hi-IN-MadhurNeural)
    with one retry, validates the PCM length (truncated clips are
    discarded), and puts PCM chunks into audio_q for the streamer's
    audio pump.
  - If TTS is unavailable, commentary degrades to silence and the stream
    continues video-only.
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
FILLER_GAP = 20          # speak a filler if quiet longer than this (seconds)
MIN_PCM_SEC = 0.8        # discard decoded clips shorter than this (truncated)

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
    "by": [
        "Bye ka run.",
    ],
}

FILLER_GENERIC = [
    "Doston, match ka romanch apne urooj par hai. Jude rahiye hamare saath.",
    "Gendbaaz line-length par mehnat kar rahe hain, ballebaaz sambhal kar khel rahe hain.",
    "Stadium mein zabardast mahaul hai, darshakon ka josh dekhne layak hai.",
    "Agli gend ka intezaar hai, dekhte hain ballebaaz is baar kya karte hain.",
    "Fielding team ke kaptan ne field mein thodi tabdeeli ki hai.",
]


def _parse_balls(overs_str):
    """'19.1' -> 115 (total balls bowled); '19' -> 114; None on garbage."""
    try:
        parts = str(overs_str).split(".")
        o = int(parts[0])
        b = int(parts[1]) if len(parts) > 1 else 0
        return o * 6 + b
    except (TypeError, ValueError, IndexError):
        return None


def _latest_token(recent_overs):
    """Newest ball token from the strip — a hint only, never the event source."""
    try:
        return (recent_overs[-1][1][-1] or "").strip().lower()
    except (IndexError, TypeError):
        return ""


def _tts_pcm(text):
    """Hindi text -> stereo s16le 44100 PCM bytes, or None on any failure.

    Retries once; discards truncated decodes (shorter than MIN_PCM_SEC).
    """
    min_bytes = int(SAMPLE_RATE * 4 * MIN_PCM_SEC)
    for _ in range(2):
        mp3 = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                mp3 = f.name
            cp = subprocess.run(
                ["edge-tts", "--voice", VOICE, "--text", text, "--write-media", mp3],
                capture_output=True, timeout=40)
            if (cp.returncode != 0 or not os.path.exists(mp3)
                    or os.path.getsize(mp3) == 0):
                time.sleep(1)
                continue
            cp2 = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", mp3,
                 "-filter:a", "volume=1.5", "-f", "s16le",
                 "-ar", str(SAMPLE_RATE), "-ac", "2", "-"],
                capture_output=True, timeout=40)
            pcm = cp2.stdout or b""
            if len(pcm) >= min_bytes:
                return pcm
            print(f"commentary: truncated clip discarded ({len(pcm)} bytes)",
                  flush=True)
        except Exception as e:
            print(f"commentary: tts attempt failed: {e}", flush=True)
        finally:
            if mp3:
                try:
                    os.unlink(mp3)
                except Exception:
                    pass
        time.sleep(1)
    return None


def _silence(seconds):
    n = int(SAMPLE_RATE * seconds)
    return b"\x00" * (n * 4)  # stereo s16le


class Commentator(threading.Thread):
    """Background Hindi commentator.

    text_q  <- poll thread enqueues Hindi text (never blocks)
    audio_q -> synth worker puts PCM chunks here for the audio pump
    """

    def __init__(self, cricbuzz_url):
        super().__init__(daemon=True)
        self.url = cricbuzz_url
        self.text_q = queue.Queue()
        self.audio_q = queue.Queue()
        self._last = None  # (team, runs, wkts, balls) — score-delta event source
        self._welcomed = False
        self._end_announced = False
        self._filler_idx = 0
        self._last_queued = time.time()
        threading.Thread(target=self._synth_worker, daemon=True).start()

    # -- synthesis worker ----------------------------------------------
    def _synth_worker(self):
        while True:
            text = self.text_q.get()
            pcm = _tts_pcm(text)
            if pcm:
                if self.audio_q.qsize() < MAX_PENDING:
                    self.audio_q.put(_silence(SILENCE_PAD) + pcm
                                     + _silence(SILENCE_PAD))
                    self._last_queued = time.time()
                    print(f"commentary: {text[:80]}", flush=True)
                else:
                    print(f"commentary dropped (backlog): {text[:40]}",
                          flush=True)
            else:
                print(f"commentary TTS failed, skipped: {text[:60]}", flush=True)

    def _enqueue(self, text, is_filler=False):
        # Don't let fillers pile up if synthesis is slow/failing; ball lines
        # always go through.
        if is_filler and self.text_q.qsize() >= 4:
            return
        self.text_q.put(text)

    # -- helpers --------------------------------------------------------
    def _names(self, st):
        batters = st.get("batters") or []
        bowlers = st.get("bowlers") or []
        bat = next((b["name"] for b in batters if b.get("striker")),
                   batters[0]["name"] if batters else "ballebaz")
        bowl = next((b["name"] for b in bowlers if b.get("cur")),
                    bowlers[0]["name"] if bowlers else "gendbaaz")
        return bat, bowl

    def _filler(self, st):
        c = []
        team, runs, wkts = st["team"], st["runs"], st["wkts"]
        overs, crr = st.get("overs"), st.get("crr")
        if runs:
            c.append(f"Taaza score hai {team}, {runs} par {wkts}, {overs} over mein."
                     + (f" Run rate {crr} ka." if crr else ""))
        batters = st.get("batters") or []
        if len(batters) >= 2:
            c.append(f"{batters[0]['name']} aur {batters[1]['name']} "
                     f"crease par maujood hain.")
        try:
            top = max(batters, key=lambda x: int(x.get("r") or 0))
            c.append(f"{top['name']} {top['r']} run par khel rahe hain.")
        except Exception:
            pass
        cur = [b for b in (st.get("bowlers") or []) if b.get("cur")]
        if cur:
            bw = cur[0]
            c.append(f"{bw['name']} ka spell ab tak, {bw['o']} over mein "
                     f"{bw['r']} run dekar {bw['w']} wicket.")
        wa, wap = st.get("win_a"), st.get("win_a_pct")
        wb, wbp = st.get("win_b"), st.get("win_b_pct")
        if wa and wap:
            c.append(f"Jeet ke chances is waqt, {wa} {wap} feesad, {wb} {wbp} feesad.")
        c.extend(FILLER_GENERIC)
        line = c[self._filler_idx % len(c)]
        self._filler_idx += 1
        return line

    # -- main poll loop ---------------------------------------------------
    def run(self):
        while True:
            try:
                self._poll()
            except Exception as e:
                print(f"commentator poll failed: {e}", flush=True)
            time.sleep(POLL_SEC)

    def _poll(self):
        # Event source = SCORE DELTAS (monotonic, reliable). The Cricbuzz
        # recent-overs strip reorders and replays old balls, so it is only
        # ever used as a disambiguation hint — never as the event source.
        st = fetch_rich(self.url)
        if not st.get("runs"):
            return
        team = st["team"]
        try:
            runs, wkts = int(st["runs"]), int(st["wkts"])
        except (TypeError, ValueError):
            return
        balls = _parse_balls(st.get("overs"))
        if balls is None:
            return

        if not self._welcomed:
            self._welcomed = True
            self._last = (team, runs, wkts, balls)
            title = st.get("match_title") or "is mukable"
            self._enqueue(f"Namaskar doston! {title} mein AI Hindi commentary ke saath "
                          f"aapka swagat hai. Taaza score hai {team}, {runs} par {wkts}, "
                          f"{st.get('overs')} over mein.")
            return

        lt, lr, lw, lb = self._last

        # new innings? (team changed, or ball count jumped way back)
        if team != lt or balls < lb - 6:
            self._last = (team, runs, wkts, balls)
            self._end_announced = False
            self._enqueue(f"Nayi pari ka aaghaaz! {team} ki ballebazi shuru ho chuki hai.")
            return

        # match end?
        status = (st.get("status") or "").lower()
        if not self._end_announced and any(k in status for k in ("won by", "tied", "drawn")):
            self._end_announced = True
            self._enqueue(f"Match samapt! {st.get('status')}. "
                          f"AI Hindi commentary mein judne ke liye dhanyavaad!")
            return

        db, dr, dw = balls - lb, runs - lr, wkts - lw

        # Garbage / out-of-order data (score went backwards, wild jump):
        # resync silently — NEVER replay stale balls.
        if db < 0 or dr < 0 or dw < 0 or dw > 2 or db > 6 or dr > 8:
            self._last = (team, runs, wkts, balls)
            return

        # No change -> filler keeps the commentary running like a real one.
        if db == 0 and dr == 0 and dw == 0:
            if time.time() - self._last_queued > FILLER_GAP:
                self._enqueue(self._filler(st), is_filler=True)
            return

        bat, bowl = self._names(st)
        hint = _latest_token(st.get("recent_overs"))
        if dw > 0:
            line = random.choice(LINES["w"]).format(bat=bat, bowl=bowl)
        elif db == 0:
            # extra without a legal ball: wide or no-ball
            key = "nb" if hint == "nb" else "wd"
            line = random.choice(LINES[key]).format(bat=bat, bowl=bowl)
        elif dr == 6:
            line = random.choice(LINES["6"]).format(bat=bat, bowl=bowl)
        elif dr == 4:
            line = random.choice(LINES["4"]).format(bat=bat, bowl=bowl)
        elif dr > 6:
            line = f"{dr} run! {bat} ki tez daud ka kamaal!"
        elif dr == 5:
            line = f"Paanch run! {bat} ne tez daud se paanch run jod liye!"
        elif dr >= 1:
            line = random.choice(LINES[str(dr)]).format(bat=bat, bowl=bowl)
        else:
            line = random.choice(LINES["0"]).format(bat=bat, bowl=bowl)
        self._enqueue(line)
        self._last = (team, runs, wkts, balls)

        # over summary when an over completes
        if balls // 6 > lb // 6:
            self._enqueue(f"{balls // 6} over ki samapti. Score hai {team}, "
                          f"{runs} par {wkts}.")
