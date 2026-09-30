#!/usr/bin/env python3
"""Live cricket score streamer v2 — KM PUNK-style animated scoreboard.

Renders the rich scoreboard (scoreboard_v2) at 10fps with an animated
cartoon stadium background, re-fetching the live score from Cricbuzz
every REFRESH seconds, and streams to YouTube via RTMP.

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cricbuzz-url", required=True)
    ap.add_argument("--rtmp", default="")
    ap.add_argument("--test-out", default="")
    ap.add_argument("--duration", type=int, default=20700,
                    help="max stream seconds (default 5h45m, under the 6h Actions limit)")
    args = ap.parse_args()

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

    if args.test_out:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-framerate", str(FPS), "-i", "-",
               "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-t", str(args.duration),
               "-y", args.test_out]
    else:
        if not args.rtmp:
            print("ERROR: --rtmp is required for live streaming", file=sys.stderr)
            sys.exit(2)
        # Two inputs: rawvideo frames on stdin (fd 0), commentary PCM on a
        # second pipe. Audio pump below writes realtime-paced s16le audio
        # (commentary clips, silence otherwise) so A/V stay in sync.
        ar, aw = os.pipe()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-framerate", str(FPS), "-i", "-",
               "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2",
               "-i", f"pipe:{ar}",
               "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
               "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k",
               "-pix_fmt", "yuv420p", "-g", "20",
               "-c:a", "aac", "-b:a", "128k", "-ar", str(SAMPLE_RATE),
               "-f", "flv", args.rtmp]

    print("starting ffmpeg...", flush=True)
    if args.test_out:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        audio_out = None
        comm = None
    else:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, pass_fds=(ar,))
        os.close(ar)
        audio_out = os.fdopen(aw, "wb")
        comm = Commentator(args.cricbuzz_url)
        comm.start()

        def _audio_pump():
            frame = (SAMPLE_RATE // 10) * 4  # 0.1s of stereo s16le
            pending = b""
            t0 = time.time()
            written = 0
            while True:
                if len(pending) < frame:
                    try:
                        pending += comm.q.get(timeout=0.05)
                    except queue.Empty:
                        pass
                elapsed = time.time() - t0
                target = int(elapsed * SAMPLE_RATE * 4)
                while written < target:
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
                    written += frame
                time.sleep(0.02)

        threading.Thread(target=_audio_pump, daemon=True).start()

    frame_dt = 1.0 / FPS
    start = time.time()
    last_fetch = 0.0
    frames = 0
    try:
        while True:
            now = time.time()
            if now - start >= args.duration:
                print("duration reached, stopping", flush=True)
                break
            if now - last_fetch >= REFRESH_SEC:
                last_fetch = now
                try:
                    upd = fetch_rich(args.cricbuzz_url)
                    if upd.get("runs"):
                        state = upd
                        print(f"score updated: {state['team']} {state['runs']}/"
                              f"{state['wkts']} ({state['overs']})", flush=True)
                except Exception as e:
                    print(f"fetch failed: {e}", flush=True)
            img = render_v2(state, F, frames)
            try:
                proc.stdin.write(img.tobytes())
            except BrokenPipeError:
                print("ffmpeg pipe closed", flush=True)
                break
            frames += 1
            target = start + frames * frame_dt
            delay = target - time.time()
            if delay > 0:
                time.sleep(delay)
            elif frames % 100 == 0:
                print(f"warning: render slower than realtime ({frames} frames)", flush=True)
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
        proc.wait(timeout=30)
    print(f"done, wrote {frames} frames", flush=True)


if __name__ == "__main__":
    main()
