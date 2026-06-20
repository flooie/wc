"""Old-school arcade replay of a World Cup match, drawn on a <canvas>.

This is the browser cousin of an iOS SpriteKit scene: a top-down pixel
pitch with two teams of blocky players and a ball, driven by a
requestAnimationFrame loop — all in Python via PyScript.

The match timeline comes from ESPN's summary endpoint:
    .../fifa.world/summary?event=<id>   ->   data["keyEvents"]
Each key event (goal, card, substitution, ...) fires at its match
minute as a virtual clock advances.
"""

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

# Pitch inset (margin around the playing area).
M = 40
PITCH = (M, M, W - 2 * M, H - 2 * M)  # x, y, w, h

SPEEDS = [1, 2, 4, 8]

state = {
    "ready": False,
    "playing": False,
    "speed_idx": 0,
    "clock": 0.0,        # virtual match minutes
    "max_min": 90.0,
    "next_idx": 0,
    "events": [],        # parsed timeline
    "home": {"name": "HOME", "abbr": "HOM", "color": "#e23b3b"},
    "away": {"name": "AWAY", "abbr": "AWY", "color": "#3b6fe2"},
    "score": [0, 0],
    "ball": {"x": W / 2, "y": H / 2, "tx": W / 2, "ty": H / 2},
    "flash": None,       # {"text": str, "sub": str, "t": seconds_left, "big": bool}
    "shake": 0.0,
    "players": [],       # static formation positions
    "last_ts": None,
}

_frame_proxy = None


# ---------------------------------------------------------------------------
# Parsing the ESPN summary
# ---------------------------------------------------------------------------
def _q(sel):
    return document.querySelector(sel)


def _color(team):
    c = team.get("color") or ""
    c = c.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return f"#{c}"
    return ""


def _minute(clock_value):
    """'45'+2'' -> 47 ; '23'' -> 23 ; fallback 0."""
    if not clock_value:
        return 0
    nums = re.findall(r"\d+", str(clock_value))
    if not nums:
        return 0
    total = int(nums[0])
    if len(nums) > 1:
        total += int(nums[1])
    return total


def _icon(type_text):
    t = (type_text or "").lower()
    if "own goal" in t:
        return "⚽"
    if "penalty" in t and ("miss" in t or "saved" in t):
        return "❌"
    if "goal" in t:
        return "⚽"
    if "yellow" in t:
        return "🟨"
    if "red" in t:
        return "🟥"
    if "substitution" in t or t == "sub":
        return "🔄"
    return "•"


def parse_summary(summary):
    header = summary.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    comps = comp.get("competitors", []) or []

    side_by_id = {}
    for c in comps:
        team = c.get("team", {}) or {}
        info = {
            "name": (team.get("displayName") or "TBD").upper(),
            "abbr": (team.get("abbreviation") or team.get("displayName") or "")[:3].upper(),
            "color": _color(team) or ("#e23b3b" if c.get("homeAway") == "home" else "#3b6fe2"),
        }
        if c.get("homeAway") == "home":
            state["home"] = info
        elif c.get("homeAway") == "away":
            state["away"] = info
        side_by_id[str(team.get("id"))] = c.get("homeAway")

    events = []
    for ev in (summary.get("keyEvents") or []):
        etype = (ev.get("type") or {}).get("text", "") or ""
        team = ev.get("team") or {}
        side = side_by_id.get(str(team.get("id")), "")
        minute = _minute((ev.get("clock") or {}).get("displayValue"))
        players = ev.get("participants") or []
        who = next(
            (
                (p.get("athlete") or {}).get("displayName")
                for p in players
                if (p.get("athlete") or {}).get("displayName")
            ),
            "",
        )
        low = etype.lower()
        is_goal = "goal" in low and "missed" not in low and "no goal" not in low
        events.append(
            {
                "minute": minute,
                "type": etype,
                "icon": _icon(etype),
                "side": side if side in ("home", "away") else "",
                "who": who,
                "text": ev.get("text") or etype,
                "goal": is_goal,
            }
        )

    events.sort(key=lambda e: e["minute"])
    state["events"] = events
    last = events[-1]["minute"] if events else 90
    state["max_min"] = float(max(90, last + 3))

    # Build two simple 4-3-3-ish formations.
    state["players"] = _formation()
    state["ready"] = True
    _update_scoreboard()


def _formation():
    x, y, w, h = PITCH
    players = []
    # Home attacks left -> right; away attacks right -> left.
    home_cols = [0.07, 0.22, 0.36, 0.46]  # GK, DEF, MID, FWD bands
    away_cols = [0.93, 0.78, 0.64, 0.54]
    bands_home = [1, 4, 3, 3]
    bands_away = [1, 4, 3, 3]
    for cols, bands, side in ((home_cols, bands_home, "home"), (away_cols, bands_away, "away")):
        for ci, count in enumerate(bands):
            for r in range(count):
                px = x + cols[ci] * w
                py = y + h * (r + 1) / (count + 1)
                players.append({"x": px, "y": py, "bx": px, "by": py, "side": side})
    return players


# ---------------------------------------------------------------------------
# Event firing
# ---------------------------------------------------------------------------
def _goal_target(side):
    x, y, w, h = PITCH
    # Home scores in the right goal; away scores in the left goal.
    gx = x + w if side == "home" else x
    return gx, y + h / 2


def fire_event(ev):
    if ev["goal"] and ev["side"] in ("home", "away"):
        idx = 0 if ev["side"] == "home" else 1
        state["score"][idx] += 1
        gx, gy = _goal_target(ev["side"])
        state["ball"]["tx"], state["ball"]["ty"] = gx, gy
        team = state["home"] if ev["side"] == "home" else state["away"]
        state["flash"] = {"text": "GOAL!", "sub": f"{ev['minute']}'  {ev['who'] or team['name']}",
                          "t": 2.2, "big": True}
        state["shake"] = 0.6
        _update_scoreboard()
    else:
        label = ev["type"].upper()
        sub = f"{ev['minute']}'  {ev['who']}".strip()
        state["flash"] = {"text": f"{ev['icon']} {label}", "sub": sub, "t": 1.8, "big": False}
        # nudge the ball toward midfield for non-goal events
        x, y, w, h = PITCH
        state["ball"]["tx"] = x + w * (0.35 + 0.3 * (state["clock"] / state["max_min"]))
        state["ball"]["ty"] = y + h * (0.3 + 0.4 * ((ev["minute"] % 7) / 7))

    _q("#caption").innerText = state["flash"]["text"] + ("  " + state["flash"]["sub"]
                                                          if state["flash"]["sub"] else "")


# ---------------------------------------------------------------------------
# Update + draw
# ---------------------------------------------------------------------------
def update(ts):
    if state["last_ts"] is None:
        state["last_ts"] = ts
    dt = (ts - state["last_ts"]) / 1000.0
    state["last_ts"] = ts
    dt = min(dt, 0.1)

    if state["playing"]:
        minutes_per_sec = 4.0 * SPEEDS[state["speed_idx"]]
        state["clock"] += dt * minutes_per_sec

        while (state["next_idx"] < len(state["events"])
               and state["events"][state["next_idx"]]["minute"] <= state["clock"]):
            fire_event(state["events"][state["next_idx"]])
            state["next_idx"] += 1

        if state["clock"] >= state["max_min"]:
            state["clock"] = state["max_min"]
            state["playing"] = False
            _q("#play-btn").innerText = "▶"
            state["flash"] = {"text": "FULL TIME", "sub": "", "t": 3.0, "big": True}

    # Ball easing toward target; gentle wander when idle.
    b = state["ball"]
    if abs(b["tx"] - b["x"]) < 4 and abs(b["ty"] - b["y"]) < 4:
        x, y, w, h = PITCH
        t = state["clock"]
        b["tx"] = x + w * (0.5 + 0.25 * math.sin(t * 0.6))
        b["ty"] = y + h * (0.5 + 0.25 * math.cos(t * 0.9))
    b["x"] += (b["tx"] - b["x"]) * min(1.0, dt * 6)
    b["y"] += (b["ty"] - b["y"]) * min(1.0, dt * 6)

    # Players jitter around their base positions for a lively retro feel.
    for p in state["players"]:
        p["x"] = p["bx"] + math.sin(state["clock"] * 1.3 + p["by"]) * 4
        p["y"] = p["by"] + math.cos(state["clock"] * 1.1 + p["bx"]) * 4

    if state["flash"]:
        state["flash"]["t"] -= dt
        if state["flash"]["t"] <= 0:
            state["flash"] = None
    state["shake"] = max(0.0, state["shake"] - dt)

    _update_progress()


def _draw_pitch():
    x, y, w, h = PITCH
    # Striped grass
    stripes = 10
    for i in range(stripes):
        ctx.fillStyle = "#2e8b3d" if i % 2 == 0 else "#2a7e38"
        ctx.fillRect(x + i * w / stripes, y, w / stripes + 1, h)

    ctx.strokeStyle = "rgba(255,255,255,0.85)"
    ctx.lineWidth = 3
    ctx.strokeRect(x, y, w, h)
    # Halfway line + centre circle
    ctx.beginPath()
    ctx.moveTo(x + w / 2, y)
    ctx.lineTo(x + w / 2, y + h)
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(x + w / 2, y + h / 2, 54, 0, math.pi * 2)
    ctx.stroke()
    # Penalty boxes + goals
    bh = h * 0.5
    bw = w * 0.14
    ctx.strokeRect(x, y + (h - bh) / 2, bw, bh)
    ctx.strokeRect(x + w - bw, y + (h - bh) / 2, bw, bh)
    gh = h * 0.22
    ctx.fillStyle = "rgba(255,255,255,0.18)"
    ctx.fillRect(x - 8, y + (h - gh) / 2, 8, gh)
    ctx.fillRect(x + w, y + (h - gh) / 2, 8, gh)


def _draw_player(p):
    color = state["home"]["color"] if p["side"] == "home" else state["away"]["color"]
    ctx.fillStyle = color
    ctx.fillRect(p["x"] - 7, p["y"] - 7, 14, 14)
    ctx.fillStyle = "rgba(0,0,0,0.25)"
    ctx.fillRect(p["x"] - 7, p["y"] + 3, 14, 4)


def _draw_ball():
    b = state["ball"]
    ctx.fillStyle = "#ffffff"
    ctx.beginPath()
    ctx.arc(b["x"], b["y"], 7, 0, math.pi * 2)
    ctx.fill()
    ctx.fillStyle = "#111"
    ctx.fillRect(b["x"] - 2, b["y"] - 2, 4, 4)


def draw():
    ctx.save()
    if state["shake"] > 0:
        ctx.translate((math.sin(state["shake"] * 60)) * 6 * state["shake"],
                      (math.cos(state["shake"] * 55)) * 6 * state["shake"])

    ctx.fillStyle = "#0b1020"
    ctx.fillRect(-20, -20, W + 40, H + 40)

    if not state["ready"]:
        ctx.fillStyle = "#9fb"
        ctx.font = "16px 'Press Start 2P', monospace"
        ctx.textAlign = "center"
        ctx.fillText("LOADING…", W / 2, H / 2)
        ctx.restore()
        return

    _draw_pitch()
    for p in state["players"]:
        _draw_player(p)
    _draw_ball()

    flash = state["flash"]
    if flash:
        ctx.textAlign = "center"
        if flash["big"]:
            ctx.fillStyle = "#ffd43b"
            ctx.font = "48px 'Press Start 2P', monospace"
            ctx.fillText(flash["text"], W / 2, H / 2 - 6)
            if flash["sub"]:
                ctx.fillStyle = "#fff"
                ctx.font = "14px 'Press Start 2P', monospace"
                ctx.fillText(flash["sub"], W / 2, H / 2 + 30)
        else:
            ctx.fillStyle = "rgba(0,0,0,0.55)"
            ctx.fillRect(W / 2 - 220, 14, 440, 34)
            ctx.fillStyle = "#fff"
            ctx.font = "12px 'Press Start 2P', monospace"
            txt = flash["text"] + ("  " + flash["sub"] if flash["sub"] else "")
            ctx.fillText(txt[:46], W / 2, 36)

    ctx.restore()


def frame(ts):
    update(ts)
    draw()
    window.requestAnimationFrame(_frame_proxy)


# ---------------------------------------------------------------------------
# HUD
# ---------------------------------------------------------------------------
def _update_scoreboard():
    _q("#sb-home").innerText = state["home"]["abbr"]
    _q("#sb-away").innerText = state["away"]["abbr"]
    _q("#sb-score").innerText = f'{state["score"][0]} - {state["score"][1]}'


def _update_progress():
    _q("#sb-clock").innerText = f'{int(state["clock"])}\''
    pct = 100.0 * state["clock"] / state["max_min"] if state["max_min"] else 0
    _q("#progress-fill").style.width = f"{pct:.1f}%"


def _reset():
    state["clock"] = 0.0
    state["next_idx"] = 0
    state["score"] = [0, 0]
    state["flash"] = None
    state["ball"].update({"x": W / 2, "y": H / 2, "tx": W / 2, "ty": H / 2})
    _q("#caption").innerText = ""
    _update_scoreboard()


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
@when("click", "#play-btn")
def toggle_play(event=None):
    if not state["ready"]:
        return
    if state["clock"] >= state["max_min"]:
        _reset()
    state["playing"] = not state["playing"]
    _q("#play-btn").innerText = "⏸" if state["playing"] else "▶"


@when("click", "#restart-btn")
def restart(event=None):
    _reset()
    state["playing"] = True
    _q("#play-btn").innerText = "⏸"


@when("click", "#speed-btn")
def cycle_speed(event=None):
    state["speed_idx"] = (state["speed_idx"] + 1) % len(SPEEDS)
    _q("#speed-btn").innerText = f"{SPEEDS[state['speed_idx']]}×"


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
def _event_id():
    qs = window.location.search or ""
    for pair in qs.lstrip("?").split("&"):
        key, _, value = pair.partition("=")
        if key == "event" and value:
            return value
    return ""


async def boot():
    global _frame_proxy
    _frame_proxy = create_proxy(frame)
    window.requestAnimationFrame(_frame_proxy)  # start drawing (shows LOADING)

    event_id = _event_id()
    if not event_id:
        _q("#caption").innerText = "No match selected."
    else:
        try:
            resp = await fetch(f"{SUMMARY_URL}?event={event_id}")
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}")
            parse_summary(await resp.json())
            if not state["events"]:
                _q("#caption").innerText = "No timeline yet — kicks off when data appears."
        except Exception as exc:  # noqa: BLE001
            _q("#caption").innerText = f"Failed to load match: {exc}"

    splash = _q("#loading")
    if splash:
        splash.style.display = "none"


import asyncio  # noqa: E402  (kept near use for clarity)

asyncio.ensure_future(boot())
