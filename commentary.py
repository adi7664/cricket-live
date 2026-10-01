#!/usr/bin/env python3
"""AI Hindi cricket commentator for the live scoreboard stream — CONTINUOUS talk.

Polls Cricbuzz via scoreboard_v2.fetch_rich every POLL_SEC, detects newly
bowled balls from SCORE DELTAS (monotonic, reliable — the recent-overs
strip reorders/replays old balls so it is only a hint), and speaks
ball-by-ball Hindi commentary plus rich filler combos so the audio runs
continuously like a real human commentator.

Every utterance is 2-4 sentences (~15-25s of speech); a filler starts if
the mic has been quiet for FILLER_GAP seconds, so there is almost no
silence.

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
import subprocess
import tempfile
import threading
import time

from scoreboard_v2 import fetch_rich, is_rain_delay

VOICE = "hi-IN-MadhurNeural"
POLL_SEC = 15
SAMPLE_RATE = 44100
MAX_PENDING = 3          # cap queued clips (keeps commentary fresh)
SILENCE_PAD = 0.3        # seconds of silence around each clip
FILLER_GAP = 5           # chain next clip before current ends -> seamless
CHECK_SEC = 2            # filler check cadence (fetch still every POLL_SEC)
MIN_PCM_SEC = 0.8        # discard decoded clips shorter than this (truncated)

# Long, chatty ball-by-ball lines (2-4 sentences each, ~15-25s of speech).
BALL_LINES = {
    "4": [
        "Chauka! {bat} ne {bowl} ki gend ko seema rekha ke paar bhej diya! "
        "Kya lajawaab timing thi is shot mein! Score pahunch gaya {team} ka "
        "{runs} par {wkts}.",
        "Chaaron khane chitt! {bat} ka shandaar chauka, darshakon mein khushi "
        "ki lehar daud gayi hai! {bat} ab {br} run par khel rahe hain, kya "
        "form mein hain!",
    ],
    "6": [
        "Chhakka! {bat} ka vishaal chhakka! Gend seedhi stand mein ja giri! "
        "Taqat aur timing ka behtareen namuna! {team} ka score ab {runs} par "
        "{wkts}.",
        "Kya shot hai! {bat} ne {bowl} ki gend ko aasman ki sair kara di! "
        "Chhe run! Stadium mein shor mach gaya hai, {bat} {br} run par "
        "pahunch gaye!",
    ],
    "w": [
        "Wicket! {bat} out ho gaye! {bowl} ko mili ye keemti safalta! "
        "Ballebaaz mayus hokar pavilion laut rahe hain. Score hai {team} ka "
        "{runs} par {wkts}.",
        "Badi safalta {bowl} ke liye! {bat} ko pavilion ka rasta dikhaya! "
        "Ye wicket match ka rukh badal sakti hai, dekhte hain naye ballebaaz "
        "kya karte hain.",
    ],
    "0": [
        "Koi run nahi. {bowl} ki kasi hui gend, {bat} poori tarah beaten hue. "
        "Behtareen line aur length ka muzahira. Dabav badhta hua ballebazon "
        "par.",
        "Dot ball! {bowl} ne {bat} ko khulne ka mauka nahi diya. Run rate "
        "{crr} ka hai, aur ye lagataar dabav wicket dila sakta hai.",
    ],
    "1": [
        "Ek run, {bat} ne halke haathon se khela. Score mein ek run ka izafa, "
        "{team} {runs} par {wkts}. {bat} {br} run par pahunch gaye hain.",
        "Ek run ka izafa score mein. {bat} ne samajhdaari se strike apne paas "
        "rakhi. Sajhedari dheere dheere aage badh rahi hai, {runs} par {wkts}.",
    ],
    "2": [
        "Do run! Wicketon ke beech tez daud ka behtareen namuna! {bat} ne do "
        "run jod liye. Fielders thode sust nazar aaye is baar.",
        "Achhi running! {bat} ne {bowl} ki gend par do run chura liye. Score "
        "{runs} par {wkts}, {overs} over ka khel ho chuka hai.",
    ],
    "3": [
        "Teen run! Gend seema rekha se just pehle ruk gayi! Fielder ne "
        "bahaduri se chauke ko roka. {bat} ki mehnat rang laayi, teen run "
        "mile. Score {runs} par {wkts}.",
    ],
    "wd": [
        "Wide gend! {bowl} line se bhatke, umpire ka ishara wide ka. Atirikt "
        "run milega aur gend dobara daalni hogi. Ballebazon ko muft ka run "
        "tohfe mein mila.",
        "Line se bhatke {bowl}! Wide ka ishara! Dabav mein gendbaaz ki ye "
        "ghalti, atirikt run ke saath score aage badhega.",
    ],
    "nb": [
        "No ball! {bowl} se badi ghalti ho gayi! Atirikt run ke saath saath "
        "agli gend free hit hogi. {bat} ke paas bada shot khelne ka sunahra "
        "mauka hai.",
        "Umpire ka ishara, ye no ball hai! {bowl} ne had paar kar di. "
        "Ballebaaz khush honge, free hit par bada shot lagbhag pakka hai.",
    ],
}

FILLER_GENERIC = [
    "Doston, match ka romanch apne urooj par hai. Stadium mein darshakon ka "
    "josh dekhne layak hai. Jude rahiye hamare saath, har gend ki taaza "
    "khabar yahin milegi.",
    "Gendbaaz line-length par mehnat kar rahe hain, ballebaaz sambhal kar "
    "khel rahe hain. Fielding team ke kaptan ne field mein thodi tabdeeli ki "
    "hai. Dekhte hain iska kya asar hota hai.",
    "Pitch se gendbaazon ko thodi madad mil rahi hai, isliye ballebazon ko "
    "sambhal kar khelna pad raha hai. Agle kuch over mein run rate par nazar "
    "rahegi.",
]

# Rain detection imported from scoreboard_v2 (fixed version).

# Rain-delay filler lines — interesting, accurate rain talk (2-4 sentences each).
RAIN_FILLERS = [
    "Doston, Korogi Sports Park mein barish ne khel rok diya hai. Pitch par "
    "bade covers bichha diye gaye hain, aur ground staff soppers aur sponges "
    "se outfield ka paani sukhaane mein juta hai. Umpire thodi der mein pitch "
    "ka muaina karenge.",
    "Yaad rahe doston, barish ke mausam mein DLS ka hisaab hamesha ahem rehta "
    "hai. Agar overs mein katauti hoti hai to chasing team ke saamne naya target "
    "aayega, aur run rate ka har decimal final tak ka raasta tay kar sakta hai.",

    "Agar barish rukti hai aur khel dobara shuru hota hai to kam overs par "
    "DLS ka hisaab lagoo hoga. Aise mein Bangladesh ke saamne naya target "
    "aayega aur har gend aur bhi keemti ho jayegi. Umpire lagataar mausam par "
    "nazar rakhe hue hain.",
    "Dono teamon ke khiladi dressing room mein barish ke thamne ka intezaar "
    "kar rahe hain, aur darshak bechaini se aasmaan ki taraf dekh rahe hain. "
    "Nisshin ka ye maidan ek saal ki mehnat se baseball ground se cricket "
    "stadium mein badla gaya tha.",
    "Ground staff poori jaan laga raha hai — bade soppers, squeegee aur "
    "sponges se outfield ka paani nikala ja raha hai. Umpire har kuch minute "
    "mein pitch ka muaina kar rahe hain. Jaise hi maidan khelne layak hoga, "
    "khiladi wapas aayenge.",
    "Jude rahiye hamare saath doston, barish se judi har taaza khabar sabse "
    "pehle yahin milegi. Covers kab hatenge, umpire kab muaina karenge, aur "
    "DLS ka kya hisaab banega — sab kuch aapko yahin sunne ko milega.",
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
                ["edge-tts", "--voice", VOICE, "--rate", "+20%", "--text", text, "--write-media", mp3],
                capture_output=True, timeout=60)
            if (cp.returncode != 0 or not os.path.exists(mp3)
                    or os.path.getsize(mp3) == 0):
                time.sleep(1)
                continue
            cp2 = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", mp3,
                 "-filter:a", "volume=2.0", "-f", "s16le",
                 "-ar", str(SAMPLE_RATE), "-ac", "2", "-"],
                capture_output=True, timeout=60)
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
        self._rain_announced = False
        self._rain_filler_idx = 0
        self._last_queued = time.time()
        self._cached_st = None
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

    def _ctx(self, st, bat, bowl):
        batters = st.get("batters") or []
        sb = next((b for b in batters if b.get("striker")),
                  batters[0] if batters else {})
        return {"bat": bat, "bowl": bowl,
                "team": st.get("team"), "runs": st.get("runs"),
                "wkts": st.get("wkts"), "overs": st.get("overs"),
                "crr": st.get("crr") or "",
                "br": sb.get("r", ""), "bb": sb.get("b", "")}

    def _filler(self, st):
        """3-sentence filler combos — score, players, analysis, atmosphere."""
        if is_rain_delay(st.get("status")):
            return self._rain_filler(st)
        team, runs, wkts = st.get("team"), st.get("runs"), st.get("wkts")
        overs, crr = st.get("overs"), st.get("crr") or ""
        c = []
        batters = st.get("batters") or []
        if len(batters) >= 2:
            c.append(
                f"Taaza score hai {team}, {runs} par {wkts}, {overs} over mein. "
                f"Run rate {crr} ka chal raha hai. {batters[0]['name']} aur "
                f"{batters[1]['name']} crease par datt kar khel rahe hain, "
                f"sajhedari aham hoti ja rahi hai.")
        try:
            top = max(batters, key=lambda x: int(x.get("r") or 0))
            cur = [b for b in (st.get("bowlers") or []) if b.get("cur")]
            if cur:
                bw = cur[0]
                c.append(
                    f"{top['name']} {top['r']} run par khel rahe hain, ye pari "
                    f"team ke liye bahut aham hai. {bw['name']} ka spell ab tak "
                    f"{bw['o']} over mein {bw['r']} run dekar {bw['w']} wicket ka "
                    f"raha hai. Gend thodi purani ho chuki hai, dekhte hain "
                    f"spinners ko kitni madad milti hai.")
        except Exception:
            pass
        wa, wap = st.get("win_a"), st.get("win_a_pct")
        wb, wbp = st.get("win_b"), st.get("win_b_pct")
        if wa and wap:
            c.append(
                f"Jeet ke chances is waqt {wa} {wap} feesad aur {wb} {wbp} feesad "
                f"hain. Match abhi poori tarah khula hua hai, aur agle kuch over "
                f"is mukable ka rukh tay karenge. Dono teamon ke khaimon mein "
                f"bechaini saaf dekhi ja sakti hai.")
        if runs:
            c.append(
                f"Score hai {team} ka {runs} par {wkts}, {overs} over ka khel ho "
                f"chuka hai. Ballebazon ki nazar ab bade shoton par hai, jabke "
                f"gendbaaz wicket ki talaash mein hain. Crowd ka josh urooj par "
                f"hai.")
        c.extend(FILLER_GENERIC)
        line = c[self._filler_idx % len(c)]
        self._filler_idx += 1
        return line

    def _rain_filler(self, st):
        """Rain-delay filler: covers, DLS, seeding stakes, ground staff."""
        team = st.get("team") or "Pakistan"
        runs, wkts = st.get("runs") or "--", st.get("wkts") or "--"
        overs = st.get("overs") or ""
        status = st.get("status") or ""
        batters = st.get("batters") or []
        crease = ""
        if len(batters) >= 2:
            crease = (f" {batters[0]['name']} aur {batters[1]['name']} crease par "
                      f"the jab khel roka gaya.")
        c = [
            f"Doston, {status}. {team} ka score hai {runs} par {wkts}, {overs} over mein.{crease} "
            f"Pitch par bade covers bichha diye gaye hain, aur ground staff soppers aur sponges "
            f"se outfield ka paani sukhaane mein juta hai.",
            "Yaad rahe doston, is tournament ke chaaron quarter final barish ki nazar ho chuke hain. "
            "Agar ye semi final bhi bina kisi nateeje ke dhul gaya to Pakistan higher seeding ki bina par "
            "final mein pahunch jayega, aur Bangladesh ka gold medal ka sapna toot jayega.",
            "Agar barish rukti hai aur khel dobara shuru hota hai to kam overs par DLS ka hisaab lagoo hoga. "
            "Aise mein Bangladesh ke saamne naya target aayega aur har gend aur bhi keemti ho jayegi. "
            "Umpire lagataar mausam par nazar rakhe hue hain.",
            "Dono teamon ke khiladi dressing room mein barish ke thamne ka intezaar kar rahe hain, "
            "aur darshak bechaini se aasmaan ki taraf dekh rahe hain. Nisshin ka ye maidan ek saal ki "
            "mehnat se baseball ground se cricket stadium mein badla gaya tha.",
            "Ground staff poori jaan laga raha hai — bade soppers, squeegee aur sponges se outfield ka "
            "paani nikala ja raha hai. Umpire har kuch minute mein pitch ka muaina kar rahe hain. "
            "Jaise hi maidan khelne layak hoga, khiladi wapas aayenge.",
            "Jude rahiye hamare saath doston, barish se judi har taaza khabar sabse pehle yahin milegi. "
            "Covers kab hatenge, umpire kab muaina karenge, aur DLS ka kya hisaab banega — sab kuch "
            "aapko yahin sunne ko milega.",
        ]
        line = c[self._rain_filler_idx % len(c)]
        self._rain_filler_idx += 1
        return line

    # -- main loop ------------------------------------------------------
    def run(self):
        # Ball events are fetched every POLL_SEC; the filler check runs every
        # CHECK_SEC so the next clip is synthesized BEFORE the current one
        # ends -> near-seamless continuous talk.
        last_fetch = 0.0
        while True:
            try:
                now = time.time()
                if now - last_fetch >= POLL_SEC:
                    last_fetch = now
                    self._poll()
                elif (self._welcomed and self._cached_st
                        and now - self._last_queued > FILLER_GAP
                        and self.audio_q.qsize() < 2):
                    self._enqueue(self._filler(self._cached_st), is_filler=True)
            except Exception as e:
                print(f"commentator loop failed: {e}", flush=True)
            time.sleep(CHECK_SEC)

    def _poll(self):
        # Event source = SCORE DELTAS (monotonic, reliable). The Cricbuzz
        # recent-overs strip reorders and replays old balls, so it is only
        # ever used as a disambiguation hint — never as the event source.
        st = fetch_rich(self.url)
        if not st.get("runs"):
            return
        self._cached_st = st
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
            self._enqueue(
                f"Namaskar doston! {title} mein AI Hindi commentary ke saath "
                f"aapka swagat hai. Taaza score hai {team}, {runs} par {wkts}, "
                f"{st.get('overs')} over mein. Main aapko har gend ki taaza "
                f"khabar dunga, jude rahiye hamare saath.")
            return

        lt, lr, lw, lb = self._last

        # new innings? (team changed, or ball count jumped way back)
        if team != lt or balls < lb - 6:
            self._last = (team, runs, wkts, balls)
            self._end_announced = False
            self._enqueue(f"Nayi pari ka aaghaaz! {team} ki ballebazi shuru ho "
                          f"chuki hai. Dekhte hain is pari mein kya hota hai.")
            return

        # match end?
        status = (st.get("status") or "").lower()
        if not self._end_announced and any(k in status for k in ("won by", "tied", "drawn")):
            self._end_announced = True
            self._enqueue(f"Match samapt! {st.get('status')}. "
                          f"AI Hindi commentary mein judne ke liye dhanyavaad!")
            return

        # rain delay? announce once, then let rain fillers do the talking
        if is_rain_delay(st.get("status")):
            if not self._rain_announced:
                self._rain_announced = True
                self._enqueue(
                    f"Doston, buri khabar! Barish ki wajah se khel rok diya gaya hai — "
                    f"{st.get('status')}. {team} ka score hai {runs} par {wkts}, "
                    f"{st.get('overs')} over mein. Pitch par covers bichha diye gaye hain. "
                    f"Jude rahiye, barish ki har taaza khabar yahin milegi.")
            return
        if self._rain_announced:
            # delay over — play resuming
            self._rain_announced = False
            self._enqueue(
                "Achhi khabar doston! Barish tham gayi hai, covers hataye ja rahe hain "
                "aur khel thodi der mein dobara shuru hoga!")
            return

        db, dr, dw = balls - lb, runs - lr, wkts - lw

        # Garbage / out-of-order data (score went backwards, wild jump):
        # resync silently — NEVER replay stale balls.
        if db < 0 or dr < 0 or dw < 0 or dw > 2 or db > 6 or dr > 8:
            self._last = (team, runs, wkts, balls)
            return

        # No change -> the run() loop's filler check keeps talk continuous.
        if db == 0 and dr == 0 and dw == 0:
            return

        bat, bowl = self._names(st)
        ctx = self._ctx(st, bat, bowl)
        hint = _latest_token(st.get("recent_overs"))
        if dw > 0:
            line = random.choice(BALL_LINES["w"]).format(**ctx)
        elif db == 0:
            # extra without a legal ball: wide or no-ball
            key = "nb" if hint == "nb" else "wd"
            line = random.choice(BALL_LINES[key]).format(**ctx)
        elif dr == 6:
            line = random.choice(BALL_LINES["6"]).format(**ctx)
        elif dr == 4:
            line = random.choice(BALL_LINES["4"]).format(**ctx)
        elif dr > 6:
            line = (f"{dr} run! {bat} ki tez daud ka kamaal! Fielders gend ke "
                    f"peeche bhaagte reh gaye. Score {runs} par {wkts}.")
        elif dr == 5:
            line = (f"Paanch run! {bat} ne tez daud se paanch run jod liye! "
                    f"Kya running hai wicketon ke beech!")
        elif dr >= 1:
            line = random.choice(BALL_LINES[str(dr)]).format(**ctx)
        else:
            line = random.choice(BALL_LINES["0"]).format(**ctx)
        self._enqueue(line)
        self._last = (team, runs, wkts, balls)

        # over summary when an over completes
        if balls // 6 > lb // 6:
            self._enqueue(f"{balls // 6} over ki samapti. Score hai {team}, "
                          f"{runs} par {wkts}. Agle over mein dekhte hain kya "
                          f"hota hai.")
