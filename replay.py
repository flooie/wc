"""Old-school arcade replay of a World Cup match, drawn on a <canvas>.

The browser cousin of an iOS SpriteKit scene: a perspective top-down
pixel pitch with two teams of little numbered sprites and a ball,
driven by a requestAnimationFrame loop — all in Python via PyScript.

Playback steps through ESPN's play-by-play `commentary` feed (the
bottom ticker), with `keyEvents` (goals, cards, subs) interleaved so
the scoreboard and big captions fire at the right moment.

    .../fifa.world/summary?event=<id>  ->  data["commentary"], data["keyEvents"]
"""

import asyncio
import math
import re

from pyodide.ffi import create_proxy
from pyscript import document, fetch, when, window

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
SUMMARY_URL = f"{BASE}/summary"

canvas = document.querySelector("#game")
ctx = canvas.getContext("2d")
W = canvas.width
H = canvas.height

# Perspective trapezoid: far (top) edge narrow, near (bottom) edge wide.
X_FAR_L, X_FAR_R = W * 0.30, W * 0.70
X_NEAR_L, X_NEAR_R = W * 0.04, W * 0.96
Y_FAR, Y_NEAR = 64, H - 28

SPEEDS = [1, 2, 4, 8]
PACE = 0.55  # seconds per commentary entry at 1x

state = {
    "ready": False,
    "playing": False,
    "speed_idx": 0,
    "clock": 0.0,
    "max_min": 90.0,
    "timeline": [],       # merged, sorted: commentary + key events
    "idx": 0,
    "entry_timer": 0.0,
    "home": {"name": "HOME", "abbr": "HOM", "color": "#f4d300", "tokens": []},
    "away": {"name": "AWAY", "abbr": "AWY", "color": "#2f6bd6", "tokens": []},
    "score": [0, 0],
    "players": [],        # {fx,fy,bx,by,side,num,att}
    "ball": {"fx": 0.5, "fy": 0.5, "tx": 0.5, "ty": 0.5},
    "carrier": -1,
    "flash": None,        # {"text","sub","t","big"}
    "ticker": "",
    "last_ts": None,
}

_frame_proxy = None


# ---------------------------------------------------------------------------
def _qs(sel):
    return document.querySelector(sel)


def _to_screen(fx, fy):
    lx = X_FAR_L + (X_NEAR_L - X_FAR_L) * fy
    rx = X_FAR_R + (X_NEAR_R - X_FAR_R) * fy
    sx = lx + (rx - lx) * fx
    sy = Y_FAR + (Y_NEAR - Y_FAR) * fy
    return sx, sy


def _depth(fy):
    return 0.55 + 0.85 * fy  # sprites bigger when nearer (bottom)


def _color(team):
    c = (team.get("color") or "").strip().lstrip("#")
    return f"#{c}" if re.fullmatch(r"[0-9a-fA-F]{6}", c) else ""


def _minute(clock_value):
    nums = re.findall(r"\d+", str(clock_value or ""))
    if not nums:
        return 0
    return int(nums[0]) + (int(nums[1]) if len(nums) > 1 else 0)


def _tokens(name):
    return [w.lower() for w in re.split(r"\s+", name or "") if len(w) >= 4]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_summary(summary):
    header = summary.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    side_by_id = {}
    for c in comp.get("competitors", []) or []:
        team = c.get("team", {}) or {}
        name = team.get("displayName") or "TBD"
        info = {
            "name": name.upper(),
            "abbr": (team.get("abbreviation") or name)[:3].upper(),
            "color": _color(team) or ("#f4d300" if c.get("homeAway") == "home" else "#2f6bd6"),
            "tokens": _tokens(name) + [(team.get("abbreviation") or "").lower()],
        }
        if c.get("homeAway") == "home":
            state["home"] = info
        elif c.get("homeAway") == "away":
            state["away"] = info
        side_by_id[str(team.get("id"))] = c.get("homeAway")

    timeline = []

    for ev in (summary.get("keyEvents") or []):
        etype = (ev.get("type") or {}).get("text", "") or ""
        low = etype.lower()
        team = ev.get("team") or {}
        side = side_by_id.get(str(team.get("id")), "")
        who = next(
            ((p.get("athlete") or {}).get("displayName")
             for p in (ev.get("participants") or [])
             if (p.get("athlete") or {}).get("displayName")), "")
        timeline.append({
            "minute": _minute((ev.get("clock") or {}).get("displayValue")),
            "seq": (ev.get("sequence") or 0),
            "kind": "key",
            "side": side if side in ("home", "away") else "",
            "goal": ("goal" in low and "missed" not in low and "no goal" not in low),
            "type": etype,
            "who": who,
            "text": ev.get("text") or etype,
        })

    for cm in (summary.get("commentary") or []):
        text = cm.get("text") or ""
        timeline.append({
            "minute": _minute((cm.get("time") or {}).get("displayValue")),
            "seq": cm.get("sequence") or 0,
            "kind": "comm",
            "text": text,
        })

    # Commentary sequence numbers run high->low (newest first); sort ascending
    # by (minute, sequence) so playback is chronological.
    timeline.sort(key=lambda e: (e["minute"], e["seq"]))
    state["timeline"] = timeline
    last = timeline[-1]["minute"] if timeline else 90
    state["max_min"] = float(max(90, last))
    state["players"] = _formation()
    state["ready"] = True
    _reset()


def _formation():
    players = []
    layout_home = [(0.05, [0.5], 1), (0.20, [0.2, 0.4, 0.6, 0.8], 2),
                   (0.37, [0.3, 0.5, 0.7], 6), (0.47, [0.3, 0.5, 0.7], 9)]
    layout_away = [(0.95, [0.5], 1), (0.80, [0.2, 0.4, 0.6, 0.8], 2),
                   (0.63, [0.3, 0.5, 0.7], 6), (0.53, [0.3, 0.5, 0.7], 9)]
    for layout, side in ((layout_home, "home"), (layout_away, "away")):
        for fx, ys, base_num in layout:
            for i, fy in enumerate(ys):
                is_gk = base_num == 1
                players.append({
                    "fx": fx, "fy": fy, "bx": fx, "by": fy, "side": side,
                    "num": base_num + i, "att": 0.05 if is_gk else 0.34,
                })
    return players


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------
def _poss_side(text):
    low = text.lower()
    for side in ("home", "away"):
        for tok in state[side]["tokens"]:
            if tok and tok in low:
                return side
    return ""


def advance_entry():
    if state["idx"] >= len(state["timeline"]):
        state["playing"] = False
        _qs("#play-btn").innerText = "▶"
        state["flash"] = {"text": "FULL TIME", "sub": "", "t": 3.0, "big": True}
        return

    e = state["timeline"][state["idx"]]
    state["idx"] += 1
    state["clock"] = float(e["minute"])

    if e["kind"] == "key":
        if e["goal"] and e["side"]:
            idx = 0 if e["side"] == "home" else 1
            state["score"][idx] += 1
            _update_score()
            team = state["home"] if e["side"] == "home" else state["away"]
            state["flash"] = {"text": "GOAL!", "sub": f"{e['minute']}'  {e['who'] or team['name']}",
                              "t": 2.0, "big": True}
            # ball to the goal that was scored on
            state["ball"]["tx"] = 0.98 if e["side"] == "home" else 0.02
            state["ball"]["ty"] = 0.5
        else:
            icon = ""
            low = e["type"].lower()
            if "yellow" in low:
                icon = "🟨"
            elif "red" in low:
                icon = "🟥"
            elif "sub" in low:
                icon = "🔁"
            state["flash"] = {"text": f"{icon} {e['type'].upper()}".strip(),
                              "sub": f"{e['minute']}'  {e['who']}".strip(), "t": 1.6, "big": False}
        state["ticker"] = e["text"]
    else:
        state["ticker"] = e["text"]
        side = _poss_side(e["text"])
        # Possessing team pushes the ball toward the opponent's goal.
        if side == "home":
            state["ball"]["tx"] = 0.55 + 0.4 * ((e["seq"] % 5) / 5)
        elif side == "away":
            state["ball"]["tx"] = 0.45 - 0.4 * ((e["seq"] % 5) / 5)
        else:
            state["ball"]["tx"] = 0.3 + 0.4 * ((e["minute"] % 7) / 7)
        state["ball"]["ty"] = 0.2 + 0.6 * ((e["seq"] % 9) / 9)


def update(ts):
    if state["last_ts"] is None:
        state["last_ts"] = ts
    dt = min((ts - state["last_ts"]) / 1000.0, 0.1)
    state["last_ts"] = ts

    if state["playing"]:
        state["entry_timer"] += dt
        pace = PACE / SPEEDS[state["speed_idx"]]
        guard = 0
        while state["playing"] and state["entry_timer"] >= pace and guard < 20:
            state["entry_timer"] -= pace
            advance_entry()
            guard += 1

    # Ball easing (field coords).
    b = state["ball"]
    b["fx"] += (b["tx"] - b["fx"]) * min(1.0, dt * 4)
    b["fy"] += (b["ty"] - b["fy"]) * min(1.0, dt * 4)

    # Players swarm toward ball, weighted by their attraction; gentle jitter.
    nearest, nd = -1, 9e9
    for i, p in enumerate(state["players"]):
        tx = p["bx"] + (b["fx"] - p["bx"]) * p["att"]
        ty = p["by"] + (b["fy"] - p["by"]) * p["att"]
        p["fx"] += (tx - p["fx"]) * min(1.0, dt * 3)
        p["fy"] += (ty - p["fy"]) * min(1.0, dt * 3)
        p["fx"] += math.sin(state["clock"] * 1.7 + i) * 0.0015
        d = (p["fx"] - b["fx"]) ** 2 + (p["fy"] - b["fy"]) ** 2
        if d < nd:
            nd, nearest = d, i
    state["carrier"] = nearest

    if state["flash"]:
        state["flash"]["t"] -= dt
        if state["flash"]["t"] <= 0:
            state["flash"] = None

    _update_hud()


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _quad(p1, p2, p3, p4, fill):
    ctx.beginPath()
    ctx.moveTo(*p1)
    ctx.lineTo(*p2)
    ctx.lineTo(*p3)
    ctx.lineTo(*p4)
    ctx.closePath()
    ctx.fillStyle = fill
    ctx.fill()


def _line(f1, f2):
    a = _to_screen(*f1)
    bb = _to_screen(*f2)
    ctx.beginPath()
    ctx.moveTo(*a)
    ctx.lineTo(*bb)
    ctx.stroke()


def _draw_pitch():
    bands = 12
    for i in range(bands):
        fy0, fy1 = i / bands, (i + 1) / bands
        shade = "#2f8a3e" if i % 2 == 0 else "#2a7d39"
        _quad(_to_screen(0, fy0), _to_screen(1, fy0),
              _to_screen(1, fy1), _to_screen(0, fy1), shade)

    ctx.strokeStyle = "rgba(255,255,255,0.85)"
    ctx.lineWidth = 2
    # Outline
    ctx.beginPath()
    ctx.moveTo(*_to_screen(0, 0))
    for f in ((1, 0), (1, 1), (0, 1), (0, 0)):
        ctx.lineTo(*_to_screen(*f))
    ctx.stroke()
    # Halfway line
    _line((0.5, 0), (0.5, 1))
    # Penalty boxes
    for x0, x1 in ((0.0, 0.16), (0.84, 1.0)):
        ctx.beginPath()
        ctx.moveTo(*_to_screen(x0, 0.25))
        for f in ((x1, 0.25), (x1, 0.75), (x0, 0.75)):
            ctx.lineTo(*_to_screen(*f))
        ctx.stroke()
    # Centre circle (perspective ellipse)
    cx, cy = _to_screen(0.5, 0.5)
    w_mid = (X_NEAR_R - X_NEAR_L) * 0.5 + (X_FAR_R - X_FAR_L) * 0.5
    ctx.beginPath()
    ctx.ellipse(cx, cy, w_mid * 0.5 * 0.18, 16, 0, 0, math.pi * 2)
    ctx.stroke()


def _draw_sprite(p):
    sx, sy = _to_screen(p["fx"], max(0.0, min(1.0, p["fy"])))
    sc = _depth(p["fy"])
    color = state["home"]["color"] if p["side"] == "home" else state["away"]["color"]

    if state["carrier"] == state["players"].index(p):
        ctx.fillStyle = "rgba(255,212,59,0.35)"
        ctx.beginPath()
        ctx.ellipse(sx, sy + 2 * sc, 9 * sc, 5 * sc, 0, 0, math.pi * 2)
        ctx.fill()

    # shadow
    ctx.fillStyle = "rgba(0,0,0,0.28)"
    ctx.beginPath()
    ctx.ellipse(sx, sy + 2 * sc, 6 * sc, 2.4 * sc, 0, 0, math.pi * 2)
    ctx.fill()
    # torso
    ctx.fillStyle = color
    ctx.fillRect(sx - 3 * sc, sy - 9 * sc, 6 * sc, 9 * sc)
    # head
    ctx.fillStyle = "#e8c89a"
    ctx.beginPath()
    ctx.arc(sx, sy - 11 * sc, 2.6 * sc, 0, math.pi * 2)
    ctx.fill()
    # number
    ctx.fillStyle = "#ffffff"
    ctx.font = f"{max(7, int(7 * sc))}px monospace"
    ctx.textAlign = "center"
    ctx.fillText(str(p["num"]), sx, sy + 9 * sc)


def _draw_ball():
    b = state["ball"]
    sx, sy = _to_screen(b["fx"], b["fy"])
    sc = _depth(b["fy"])
    ctx.fillStyle = "rgba(0,0,0,0.3)"
    ctx.beginPath()
    ctx.ellipse(sx, sy + 2 * sc, 3.5 * sc, 1.6 * sc, 0, 0, math.pi * 2)
    ctx.fill()
    ctx.fillStyle = "#ffffff"
    ctx.beginPath()
    ctx.arc(sx, sy - 1 * sc, 3.2 * sc, 0, math.pi * 2)
    ctx.fill()
    ctx.fillStyle = "#111"
    ctx.fillRect(sx - 1.2 * sc, sy - 2.2 * sc, 2.4 * sc, 2.4 * sc)


def draw():
    ctx.fillStyle = "#0b1020"
    ctx.fillRect(0, 0, W, H)

    if not state["ready"]:
        ctx.fillStyle = "#9fb6c8"
        ctx.font = "16px 'Press Start 2P', monospace"
        ctx.textAlign = "center"
        ctx.fillText("LOADING…", W / 2, H / 2)
        return

    _draw_pitch()
    # Draw far players first for correct overlap.
    for p in sorted(state["players"], key=lambda q: q["fy"]):
        _draw_sprite(p)
    _draw_ball()

    # In-screen HUD bar (BRA 3 - 0 HAI  46')
    ctx.fillStyle = "rgba(3,6,15,0.82)"
    ctx.fillRect(W / 2 - 150, 8, 300, 26)
    ctx.fillStyle = "#fff"
    ctx.font = "12px 'Press Start 2P', monospace"
    ctx.textAlign = "center"
    ctx.fillText(
        f'{state["home"]["abbr"]} {state["score"][0]} - {state["score"][1]} {state["away"]["abbr"]}'
        f'   {int(state["clock"])}\'',
        W / 2, 26)

    flash = state["flash"]
    if flash and flash["big"]:
        ctx.fillStyle = "#ffd43b"
        ctx.font = "44px 'Press Start 2P', monospace"
        ctx.fillText(flash["text"], W / 2, H / 2 - 4)
        if flash["sub"]:
            ctx.fillStyle = "#fff"
            ctx.font = "13px 'Press Start 2P', monospace"
            ctx.fillText(flash["sub"], W / 2, H / 2 + 26)
    elif flash:
        ctx.fillStyle = "rgba(0,0,0,0.6)"
        ctx.fillRect(W / 2 - 210, 40, 420, 26)
        ctx.fillStyle = "#fff"
        ctx.font = "11px 'Press Start 2P', monospace"
        ctx.fillText((flash["text"] + "  " + flash["sub"])[:46], W / 2, 58)


def frame(ts):
    update(ts)
    draw()
    window.requestAnimationFrame(_frame_proxy)


# ---------------------------------------------------------------------------
# HUD / DOM
# ---------------------------------------------------------------------------
def _update_score():
    _qs("#sb-score").innerText = f'{state["score"][0]} - {state["score"][1]}'


def _update_hud():
    _qs("#sb-clock").innerText = f'{int(state["clock"])}\''
    n = len(state["timeline"]) or 1
    _qs("#progress-fill").style.width = f"{100.0 * state['idx'] / n:.1f}%"
    _qs("#caption").innerText = state["ticker"]


def _reset():
    state["clock"] = 0.0
    state["idx"] = 0
    state["entry_timer"] = 0.0
    state["score"] = [0, 0]
    state["flash"] = None
    state["ticker"] = ""
    state["ball"].update({"fx": 0.5, "fy": 0.5, "tx": 0.5, "ty": 0.5})
    for p in state["players"]:
        p["fx"], p["fy"] = p["bx"], p["by"]
    _qs("#sb-home").innerText = state["home"]["abbr"]
    _qs("#sb-away").innerText = state["away"]["abbr"]
    _update_score()


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
@when("click", "#play-btn")
def toggle_play(event=None):
    if not state["ready"]:
        return
    if state["idx"] >= len(state["timeline"]):
        _reset()
    state["playing"] = not state["playing"]
    _qs("#play-btn").innerText = "⏸" if state["playing"] else "▶"


@when("click", "#restart-btn")
def restart(event=None):
    _reset()
    state["playing"] = True
    _qs("#play-btn").innerText = "⏸"


@when("click", "#speed-btn")
def cycle_speed(event=None):
    state["speed_idx"] = (state["speed_idx"] + 1) % len(SPEEDS)
    _qs("#speed-btn").innerText = f"{SPEEDS[state['speed_idx']]}×"


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
def _event_id():
    qs = (window.location.search or "").lstrip("?")
    for pair in qs.split("&"):
        key, _, value = pair.partition("=")
        if key == "event" and value:
            return value
    return ""


async def boot():
    global _frame_proxy
    _frame_proxy = create_proxy(frame)
    window.requestAnimationFrame(_frame_proxy)

    event_id = _event_id()
    if not event_id:
        _qs("#caption").innerText = "No match selected."
    else:
        try:
            resp = await fetch(f"{SUMMARY_URL}?event={event_id}")
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}")
            parse_summary(await resp.json())
            if not state["timeline"]:
                _qs("#caption").innerText = "No play-by-play yet for this match."
            else:
                state["playing"] = True
                _qs("#play-btn").innerText = "⏸"
        except Exception as exc:  # noqa: BLE001
            _qs("#caption").innerText = f"Failed to load match: {exc}"

    splash = _qs("#loading")
    if splash:
        splash.style.display = "none"


asyncio.ensure_future(boot())
