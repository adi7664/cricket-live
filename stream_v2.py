#!/usr/bin/env python3
"""Live cricket score streamer v2 — KM PUNK-style animated scoreboard.

Renders the rich scoreboard (scoreboard_v2) at 10fps with an animated
cartoon stadium background, re-fetching the live score from Cricbuzz
every REFRESH seconds, and streams to YouTube via RTMP.

Audio: an AI Hindi commentator (commentary.py) speaks ball-by-ball via
edge-tts; the audio pump writes realtime-paced s16le PCM (commentary
clips, silence otherwise) so A/V stay in sync.

Resilience: if the ffmpeg/RTMP session dies, the supervisor loop
reconnects with backoff — the same YouTube broadcast resumes on the
same stream key after a short gap.

Usage (GitHub Actions):
    python stream_v2.py --cricbuzz-url "<url>" --rtmp "rtmp://a.rtmp.youtube.com/live2/KEY"

Local test (no RTMP):
    python stream_v2.py --cricbuzz-url "<url>" --test-out /tmp/test.mp4 --duration 30
"""
import argparse
import os
import queue
import subprocess
import sys
import threading
import time

from scoreboard_v2 import W, H, FPS, fetch_rich, load_fonts, render_v2
from commentary import Commentator, SAMPLE_RATE

REFRESH_SEC = 20


def _audio_pump(audio_out, comm):
    """Write sleep-paced stereo s16le audio: commentary clips, else silence.

    Exactly 0.1s of audio per 0.1s of wall time — no bursts, so ffmpeg's
    input queue never blocks and no audio is chopped.
    """
    frame = (SAMPLE_RATE // 10) * 4  # 0.1s of stereo s16le
    pending = b""
    next_t = time.time()
    while True:
        if len(pending) < frame:
            try:
                pending += comm.audio_q.get(timeout=0.05)
            except queue.Empty:
                pass
        if len(pending) >= frame:
            out, pending = pending[:frame], pending[frame:]
        else:
            out = pending + b"\x00" * (frame - len(pending))
            pending = b""
        try:
            audio_out.write(out)
            audio_out.flush()
        except (BrokenPipeError, ValueError, OSError):
            return
        next_t += 0.1
        delay = next_t - time.time()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.time()  # don't spiral after a stall


def _run_session(args, F, holder, comm, t_start):
    """Run one ffmpeg session. Returns 'done' (duration reached) or 'died'."""
    if args.test_out:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-framerate", str(FPS), "-i", "-",
               "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-t", str(args.duration),
               "-y", args.test_out]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        audio_out = None
    else:
        # Three inputs: rawvideo frames on stdin (fd 0), commentary PCM on a
        # second pipe, and a generated stadium-crowd ambience bed (lavfi) that
        # plays softly underneath everything — even during pauses.
        ar, aw = os.pipe()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-framerate", str(FPS), "-i", "-",
               "-thread_queue_size", "2048",
               "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2",
               "-i", f"pipe:{ar}",
               "-f", "lavfi",
               "-i", ("anoisesrc=color=pink:sample_rate=44100:duration=86400,"
                      "lowpass=f=1200,volume=0.30,tremolo=f=0.15:d=0.7"),
               "-filter_complex",
               ("[1:a][2:a]amix=inputs=2:duration=first:"
                "dropout_transition=0:normalize=0[aout]"),
               "-map", "0:v", "-map", "[aout]",
               "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
               "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k",
               "-pix_fmt", "yuv420p", "-g", "20",
               "-c:a", "aac", "-b:a", "128k", "-ar", str(SAMPLE_RATE),
               "-f", "flv", args.rtmp]
        print("starting ffmpeg...", flush=True)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, pass_fds=(ar,))
        os.close(ar)
        audio_out = os.fdopen(aw, "wb")
        threading.Thread(target=_audio_pump, args=(audio_out, comm),
                         daemon=True).start()

    frame_dt = 1.0 / FPS
    sess_frames = 0
    sess_t0 = time.time()
    try:
        while True:
            now = time.time()
            if now - t_start >= args.duration:
                return "done"
            if proc.poll() is not None:
                print(f"ffmpeg exited (code {proc.returncode})", flush=True)
                return "died"
            if now - holder["last_fetch"] >= REFRESH_SEC:
                holder["last_fetch"] = now
                try:
                    upd = fetch_rich(args.cricbuzz_url)
                    if upd.get("runs"):
                        holder["state"] = upd
                        st = holder["state"]
                        print(f"score updated: {st['team']} {st['runs']}/"
                              f"{st['wkts']} ({st['overs']})", flush=True)
                except Exception as e:
                    print(f"fetch failed: {e}", flush=True)
            img = render_v2(holder["state"], F, holder["frames"])
            try:
                proc.stdin.write(img.tobytes())
            except (BrokenPipeError, ValueError):
                print("ffmpeg pipe closed", flush=True)
                return "died"
            holder["frames"] += 1
            sess_frames += 1
            target = sess_t0 + sess_frames * frame_dt
            delay = target - time.time()
            if delay > 0:
                time.sleep(delay)
            elif sess_frames % 100 == 0:
                print(f"warning: render slower than realtime "
                      f"({holder['frames']} frames)", flush=True)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            if audio_out:
                audio_out.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return "died"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cricbuzz-url", required=True)
    ap.add_argument("--rtmp", default="")
    ap.add_argument("--test-out", default="")
    ap.add_argument("--duration", type=int, default=20700,
                    help="max stream seconds (default 5h45m, under the 6h Actions limit)")
    args = ap.parse_args()

    if not args.test_out and not args.rtmp:
        print("ERROR: --rtmp is required for live streaming", file=sys.stderr)
        sys.exit(2)

    F = load_fonts()
    try:
        state = fetch_rich(args.cricbuzz_url)
        print(f"initial score: {state['team']} {state['runs']}/{state['wkts']} ({state['overs']})",
              flush=True)
    except Exception as e:
        print(f"initial fetch failed: {e}", flush=True)
        state = {"match_title": "Live Cricket", "subtitle": "Connecting...",
                 "team": "", "runs": "", "wkts": "", "overs": "", "crr": "",
                 "pship_runs": "", "pship_balls": "", "win_a": "", "win_a_pct": "",
                 "win_b": "", "win_b_pct": "", "batters": [], "bowlers": [],
                 "recent_overs": [], "status": ""}

    holder = {"state": state, "frames": 0, "last_fetch": 0.0}
    comm = None
    if not args.test_out:
        comm = Commentator(args.cricbuzz_url)
        comm.start()

    t_start = time.time()
    quick_fail = 0
    while True:
        if time.time() - t_start >= args.duration:
            print("duration reached, stopping", flush=True)
            break
        sess_start = time.time()
        result = _run_session(args, F, holder, comm, t_start)
        if result == "done":
            break
        # ffmpeg session died -> reconnect with backoff
        if time.time() - sess_start < 60:
            quick_fail += 1
        else:
            quick_fail = 0
        wait = min(300, 5 * (2 ** quick_fail))
        print(f"session died, reconnecting in {wait}s", flush=True)
        time.sleep(wait)
    print(f"done, wrote {holder['frames']} frames", flush=True)


if __name__ == "__main__":
    main()
