#!/usr/bin/env python3
"""KM PUNK-style rich live scoreboard renderer.

Fetches rich live data from Cricbuzz (score, overs, CRR, partnership,
win predictor, batters, bowlers, recent balls) and renders a broadcast-style
scoreboard with an animated cartoon stadium background.

Standalone preview:
    python3 scoreboard_v2.py --cricbuzz-url "<url>" --out /tmp/preview.png [--frame N]
"""
import argparse
import math
import random
import re
import sys

import requests
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 10

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

TEAM_NAMES = {
    "WI": "WEST INDIES", "IND": "INDIA", "PAK": "PAKISTAN", "AUS": "AUSTRALIA",
    "ENG": "ENGLAND", "NZ": "NEW ZEALAND", "SA": "SOUTH AFRICA", "RSA": "SOUTH AFRICA",
    "SL": "SRI LANKA", "BAN": "BANGLADESH", "AFG": "AFGHANISTAN", "ZIM": "ZIMBABWE",
    "IRE": "IRELAND", "WIW": "WEST INDIES W", "ZIMW": "ZIMBABWE W", "INDW": "INDIA W",
}


def load_fonts():
    def f(name, sz):
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}.ttf", sz)
    return {
        "huge": f("DejaVuSans-Bold", 100),
        "big": f("DejaVuSans-Bold", 72),
        "med": f("DejaVuSans-Bold", 44),
        "med2": f("DejaVuSans-Bold", 36),
        "sm": f("DejaVuSans-Bold", 30),
        "sm_r": f("DejaVuSans", 28),
        "tiny": f("DejaVuSans-Bold", 24),
        "tiny_r": f("DejaVuSans", 22),
    }


# ---------------- data ----------------

def _parts(html_seg):
    txt = re.sub(r"<[^>]+>", "|", html_seg)
    txt = re.sub(r"\|+", "|", txt)
    return [p.strip() for p in txt.split("|") if p.strip()]


def fetch_rich(url):
    """Scrape rich live data from a Cricbuzz live-scores page."""
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    html = r.text
    i = html.find('id="miniscore-branding-container"')
    seg = html[i:i + 30000] if i != -1 else html

    st = {"match_title": "", "subtitle": "", "team": "", "runs": "", "wkts": "",
          "overs": "", "crr": "", "pship_runs": "", "pship_balls": "",
          "win_a": "", "win_a_pct": "", "win_b": "", "win_b_pct": "",
          "batters": [], "bowlers": [], "recent_overs": [], "status": ""}

    m = re.search(r'<div class="mr-2">([A-Z]{2,5})</div><div><span class="mr-2">'
                  r'<span>(\d+)</span><span><span class="mx-\[3px\]">-</span>(\d+)'
                  r'</span></span><span class="mr-2">\(<!-- -->?([\d.]+)<!-- -->?\)</span>', seg)
    if m:
        st["team"], st["runs"], st["wkts"], st["overs"] = m.group(1), m.group(2), m.group(3), m.group(4)
    m = re.search(r'CRR:</span><span[^>]*>([\d.]+)</span>', seg)
    if m:
        st["crr"] = m.group(1)
    m = re.search(r'SHIP</span><span[^>]*>(\d+)<!-- -->\(<!-- -->(\d+)<!-- -->\)</span>', seg)
    if m:
        st["pship_runs"], st["pship_balls"] = m.group(1), m.group(2)
    wins = re.findall(r'title="([A-Z]{2,5})"><div class="font-normal text-gray-500 mb:text-xs">'
                      r'[A-Z]{2,5}</div><div class="font-semibold text-gray-900 mb:text-xs">'
                      r'(\d+)<!-- -->%</div>', seg)
    if len(wins) >= 2:
        (st["win_a"], st["win_a_pct"]), (st["win_b"], st["win_b_pct"]) = wins[0], wins[1]
    m = re.search(r'text-cbLive">([^<]+)<', seg)
    if m:
        st["status"] = m.group(1).strip()
    NAME2CODE = {"India": "IND", "West Indies": "WI", "Pakistan": "PAK",
                 "Australia": "AUS", "England": "ENG", "New Zealand": "NZ",
                 "South Africa": "SA", "Sri Lanka": "SL", "Bangladesh": "BAN",
                 "Afghanistan": "AFG", "Zimbabwe": "ZIM", "Ireland": "IRE",
                 "Netherlands": "NED", "Scotland": "SCO", "Namibia": "NAM",
                 "United States": "USA", "Italy": "ITA"}
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        t = m.group(1).strip()
        # e.g. "| India vs West Indies, 2nd ODI, West Indies tour of India, 2026 ..."
        g = re.search(r"\|\s*([A-Za-z ]+?)\s+vs\s+([A-Za-z ]+?)\s*,", t)
        if g:
            c1 = NAME2CODE.get(g.group(1).strip(), g.group(1).strip()[:3].upper())
            c2 = NAME2CODE.get(g.group(2).strip(), g.group(2).strip()[:3].upper())
            st["match_title"] = f"{c1} vs {c2}"
            st["subtitle"] = st["status"] or "Live"
        else:
            st["match_title"] = st["status"] or "Live Cricket"
            st["subtitle"] = ""

    parts = _parts(seg)
    # batters
    try:
        bi = parts.index("Batter")
        oi = parts.index("Bowler")
    except ValueError:
        bi = oi = -1
    if bi != -1 and oi != -1:
        j = bi + 1
        while j < oi and len(st["batters"]) < 4:
            # skip header tokens
            if parts[j] in ("R", "B", "4s", "6s", "SR"):
                j += 1
                continue
            name = parts[j]
            j += 1
            striker = False
            if j < oi and parts[j] == "*":
                striker = True
                j += 1
            nums = []
            while j < oi and len(nums) < 5:
                if re.fullmatch(r"[\d.]+", parts[j]):
                    nums.append(parts[j])
                elif parts[j] in ("View match performance", "View profile"):
                    pass
                else:
                    break
                j += 1
            if len(nums) == 5:
                st["batters"].append({"name": name, "striker": striker, "r": nums[0],
                                     "b": nums[1], "fours": nums[2], "sixes": nums[3],
                                     "sr": nums[4]})
            else:
                # resync: step one forward
                pass
    # bowlers
    try:
        ki = parts.index("Key Stats")
    except ValueError:
        ki = len(parts)
    if oi != -1:
        j = oi + 1
        while j < ki and len(st["bowlers"]) < 4:
            if parts[j] in ("O", "M", "R", "W", "ECO"):
                j += 1
                continue
            name = parts[j]
            j += 1
            cur = False
            if j < ki and parts[j] == "*":
                cur = True
                j += 1
            nums = []
            while j < ki and len(nums) < 5:
                if re.fullmatch(r"[\d.]+", parts[j]):
                    nums.append(parts[j])
                elif parts[j] in ("View match performance", "View profile"):
                    pass
                else:
                    break
                j += 1
            if len(nums) == 5:
                st["bowlers"].append({"name": name, "cur": cur, "o": nums[0],
                                     "m": nums[1], "r": nums[2], "w": nums[3],
                                     "eco": nums[4]})
    # recent balls: one <p> with overs separated by "|"
    # e.g. "... 0 0 0 1 4  | 4 0 0 1 0 0  | 4"
    m = re.search(r"Recent :<!-- -->\s*</p>\s*<p[^>]*>(.*?)</p>", seg, re.S)
    if m:
        raw = re.sub(r"<[^>]+>", "", m.group(1))
        groups = [g.strip() for g in raw.split("|")]
        try:
            cur_over_no = int(float(st["overs"])) + 1
        except Exception:
            cur_over_no = 0
        parsed = []
        for k, g in enumerate(groups):
            toks = [tok for tok in g.split()
                    if tok not in ("...", "…", "•")
                    and re.fullmatch(r"[0-9W]{1,2}|nb|wd|b|lb|by", tok)]
            if toks:
                parsed.append((cur_over_no - (len(groups) - 1 - k), toks))
        st["recent_overs"] = parsed[-2:]

    return st


# ---------------- drawing ----------------

RED = (178, 20, 20)
GOLD = (255, 210, 60)
NAVY = (16, 22, 38)
NAVY2 = (26, 34, 56)
BLUE = (0, 144, 255)
WIRED = (226, 60, 60)


def _rr(d, box, radius, fill, outline=None, width=1):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def base_scene():
    """Static cartoon stadium scene (cached, drawn once)."""
    img = Image.new("RGB", (W, H), (10, 14, 26))
    d = ImageDraw.Draw(img)
    for y in range(0, 470):
        k = y / 470
        d.line([(0, y), (W, y)], fill=(int(8 + 30 * k), int(14 + 40 * k), int(40 + 60 * k)))
    for x, y in [(120, 90), (520, 60), (900, 80), (1180, 100)]:
        d.rectangle([x - 4, y, x + 4, y + 120], fill=(60, 70, 90))
    d.rectangle([0, 200, W, 400], fill=(30, 36, 58))
    rnd2 = random.Random(42)
    for _ in range(900):
        x = rnd2.randrange(0, W)
        y = rnd2.randrange(205, 395)
        cols = [(200, 60, 60), (60, 120, 200), (230, 200, 80), (90, 180, 90), (220, 220, 230)]
        c = rnd2.choice(cols)
        if rnd2.random() < 0.06:
            c = (255, 255, 255)
        d.ellipse([x, y, x + 5, y + 5], fill=c)
    d.rectangle([0, 395, W, 410], fill=(120, 30, 30))
    for y in range(410, H):
        k = (y - 410) / (H - 410)
        d.line([(0, y), (W, y)], fill=(int(34 + 20 * k), int(110 + 30 * k), int(50 + 20 * k)))
    d.ellipse([W / 2 - 260, 430, W / 2 + 260, 700], outline=(255, 255, 255, 120), width=3)
    d.rectangle([W / 2 - 60, 470, W / 2 + 60, 700], fill=(196, 164, 110))
    for sx in (W / 2 - 14, W / 2, W / 2 + 14):
        d.rectangle([sx - 3, 560, sx + 3, 620], fill=(240, 220, 160))
    return img


_BASE = None


def draw_background(img, frame_no):
    """Animated layer over the cached static scene: floodlight pulse + flying ball."""
    global _BASE
    if _BASE is None:
        _BASE = base_scene()
    img.paste(_BASE)
    d = ImageDraw.Draw(img)
    t = frame_no / FPS
    for x, y in [(120, 90), (520, 60), (900, 80), (1180, 100)]:
        tw = 0.75 + 0.25 * math.sin(t * 2 + x)
        r = int(90 * tw)
        for rr in range(r, 0, -18):
            d.ellipse([x - rr, y - rr, x + rr, y + rr],
                      fill=(int(200 * rr / r), int(220 * rr / r), 255))
    bt = (t * 0.5) % 1.0
    bx = W / 2 - 320 + bt * 640
    by = 620 - math.sin(bt * math.pi) * 260
    d.ellipse([bx - 12, by - 12, bx + 12, by + 12], fill=(255, 255, 255),
              outline=(200, 40, 40), width=3)
    return img


def ball_chip(d, x, y, tok, F):
    s = 34
    fill = (52, 62, 88)
    fg = (235, 240, 248)
    if tok == "4":
        fill, fg = (30, 140, 60), (255, 255, 255)
    elif tok == "6":
        fill, fg = (150, 90, 10), (255, 255, 255)
    elif tok == "W":
        fill, fg = (178, 20, 20), (255, 255, 255)
    elif tok in ("nb", "wd"):
        fill, fg = (110, 70, 140), (255, 255, 255)
    _rr(d, [x, y, x + s, y + s], 8, fill)
    d.text((x + s / 2, y + s / 2), tok, font=F["tiny"], anchor="mm", fill=fg)
    return s + 8


def render_v2(st, F, frame_no=0):
    img = Image.new("RGB", (W, H), (10, 14, 26))
    draw_background(img, frame_no)
    d = ImageDraw.Draw(img)

    team_full = TEAM_NAMES.get(st["team"], st["team"])
    opp_full = ""
    mt = re.split(r"\s+vs\s+", st["match_title"], flags=re.I)
    if len(mt) == 2:
        opp_code = mt[1] if mt[0].upper() == st["team"] else mt[0]
        opp_full = TEAM_NAMES.get(opp_code.upper(), opp_code.upper())

    # ---- top scoreboard card ----
    _rr(d, [24, 56, W - 24, 300], 22, NAVY, outline=RED, width=3)
    # LIVE bug
    _rr(d, [40, 66, 150, 104], 10, RED)
    d.text((95, 85), "LIVE", font=F["sm"], anchor="mm", fill=(255, 255, 255))
    d.text((W - 40, 85), "WORLD CRICKET UPDATES", font=F["tiny_r"],
           anchor="rm", fill=(200, 208, 220))

    # left: batting team
    d.text((70, 130), team_full, font=F["sm"], anchor="lm", fill=(170, 180, 195))
    score_txt = f"{st['runs']}/{st['wkts']}" if st["runs"] else "--"
    d.text((70, 205), score_txt, font=F["huge"], anchor="lm", fill=(255, 255, 255))
    d.text((70, 274), f"OVERS: {st['overs']}   CRR: {st['crr']}",
           font=F["sm"], anchor="lm", fill=GOLD)
    # divider
    d.line([(470, 120), (470, 288)], fill=(70, 80, 100), width=2)
    # center: win predictor
    d.text((640, 130), "WIN PREDICTOR", font=F["tiny"], anchor="mm", fill=(170, 180, 195))
    try:
        pa, pb = int(st["win_a_pct"]), int(st["win_b_pct"])
    except Exception:
        pa, pb = 50, 50
    bx0, bx1, by = 480, 800, 168
    d.rectangle([bx0, by, bx1, by + 26], fill=(50, 58, 80))
    wa = (bx1 - bx0) * pa // 100
    d.rectangle([bx0, by, bx0 + wa, by + 26], fill=BLUE)
    d.rectangle([bx0 + wa + 4, by, bx1, by + 26], fill=WIRED)
    d.text((bx0, by + 52), f"{st['win_a']} {pa}%", font=F["sm"], anchor="lm", fill=(255, 255, 255))
    d.text((bx1, by + 52), f"{st['win_b']} {pb}%", font=F["sm"], anchor="rm", fill=(255, 255, 255))
    if st["pship_runs"]:
        d.text((640, 258), f"P'SHIP: {st['pship_runs']} ({st['pship_balls']})",
               font=F["sm"], anchor="mm", fill=GOLD)
    # divider
    d.line([(850, 120), (850, 288)], fill=(70, 80, 100), width=2)
    # right: bowling team
    d.text((1050, 150), opp_full or "BOWLING", font=F["med2"], anchor="mm", fill=(170, 180, 195))
    d.text((1050, 225), "BOWLING", font=F["big"], anchor="mm", fill=GOLD)

    # recent overs strip
    y0 = 300
    x = 60
    d.text((x, y0 + 22), "THIS OVER" if False else "", font=F["tiny"], anchor="lm", fill=(255, 255, 255))
    for num, grp in st["recent_overs"][-2:]:
        d.text((x, y0 + 22), f"OVER {num}", font=F["tiny"], anchor="lm", fill=(170, 180, 195))
        x += 130
        for tok in grp:
            x += ball_chip(d, x, y0 + 4, tok, F)
        x += 40
        if x > W - 120:
            break

    # ---- player cards ----
    cards = []
    for b in st["batters"][:2]:
        cards.append(("BATTER", b["name"] + ("*" if b["striker"] else ""),
                      f"{b['r']} ({b['b']})", f"4s: {b['fours']}  6s: {b['sixes']}  SR: {b['sr']}"))
    for bw in st["bowlers"][:1]:
        cards.append(("BOWLER", bw["name"] + ("*" if bw["cur"] else ""),
                      f"{bw['o']}-{bw['m']}-{bw['r']}-{bw['w']}",
                      f"Econ: {bw['eco']}"))
    cw = 384
    total = len(cards) * cw + (len(cards) - 1) * 24
    cx = (W - total) / 2
    cy = 392
    for role, name, line1, line2 in cards:
        _rr(d, [cx, cy, cx + cw, cy + 200], 18, NAVY2, outline=(70, 80, 110), width=2)
        # avatar
        ax, ay, ar = cx + 52, cy + 52, 34
        d.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=(60, 90, 150))
        initials = "".join(w[0] for w in name.replace("*", "").split()[:2]).upper()
        d.text((ax, ay), initials, font=F["sm"], anchor="mm", fill=(255, 255, 255))
        _rr(d, [cx + 100, cy + 18, cx + 230, cy + 52], 8, RED if role == "BATTER" else BLUE)
        d.text((cx + 165, cy + 35), role, font=F["tiny"], anchor="mm", fill=(255, 255, 255))
        nm = name.replace("*", "")
        if len(nm) > 18:
            nm = nm[:17] + "."
        d.text((cx + 100, cy + 78), nm.upper(), font=F["sm"], anchor="lm", fill=(255, 255, 255))
        d.text((cx + 100, cy + 128), line1, font=F["med2"], anchor="lm", fill=GOLD)
        d.text((cx + 24, cy + 172), line2, font=F["tiny_r"], anchor="lm", fill=(190, 198, 212))
        cx += cw + 24

    # ---- bottom ticker ----
    d.rectangle([0, H - 56, W, H], fill=RED)
    tick = f"  {st['match_title']}  •  {st['subtitle']}  •  Score auto-updates every few seconds  •  Subscribe for live cricket scores  • "
    tw = d.textlength(tick, font=F["sm_r"])
    off = (frame_no * 4) % max(1, int(tw))
    x = -off
    while x < W:
        d.text((x, H - 28), tick, font=F["sm_r"], anchor="lm", fill=(255, 255, 255))
        x += tw

    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cricbuzz-url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--frame", type=int, default=0)
    args = ap.parse_args()
    st = fetch_rich(args.cricbuzz_url)
    print("state:", {k: v for k, v in st.items() if k not in ("batters", "bowlers", "recent_overs")})
    print("batters:", st["batters"])
    print("bowlers:", st["bowlers"])
    print("recent:", st["recent_overs"])
    F = load_fonts()
    img = render_v2(st, F, args.frame)
    img.save(args.out)
    print("saved", args.out)


if __name__ == "__main__":
    main()
