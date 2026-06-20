"""Old-school arcade replay of a World Cup match, drawn on a <canvas>.

A flat top-down landscape pitch with a small soccer-simulation engine:
two teams in formation, possession, passing between teammates, pressing
defenders, and scripted shots / goals / kickoffs. The narrative beat
(possession side, ticker text, goals, cards) is driven by ESPN's
play-by-play feed; the engine animates plausible movement in between.

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
W = canvas.width    # 960
H = canvas.height   # 540

# Flat pitch rectangle (HUD bar floats over the top strip).
PX, PY = 26, 46
PW, PH = W - 52, H - 72

SPEEDS = [1, 2, 4, 8]
BEAT = 0.85         # seconds per narrative beat (commentary entry) at 1x
PASS_EVERY = 0.95   # seconds between engine pass/dribble decisions at 1x

# Engine tuning (field units per second; field is 0..1 in each axis).
SPD = 0.16
SPRINT = 0.30
BALL_SPD = 1.3
SHOT_SPD = 2.1       # shots travel faster than passes
SHOT_RANGE = 0.26    # shoot once within this much of the goal (attack axis)
SHOT_CHANCE = 0.18   # per-decision chance to shoot when in range

state = {
    "ready": False,
    "playing": False,
    "speed_idx": 0,
    "clock": 0.0,
    "max_min": 90.0,
    "timeline": [],
    "idx": 0,
    "beat_timer": 0.0,
    "pass_timer": 0.0,
    "reset_timer": 0.0,
    "home": {"name": "HOME", "abbr": "HOM", "color": "#f4d300", "tokens": []},
    "away": {"name": "AWAY", "abbr": "AWY", "color": "#2f6bd6", "tokens": []},
    "score": [0, 0],
    "shots": [0, 0],
    "poss_time": [0.0, 0.0],
    "poss": "home",
    "players": [],
    "ball": {"x": 0.5, "y": 0.5, "tx": 0.5, "ty": 0.5, "owner": -1,
             "flight": False, "pending": -1, "shot": None, "speed": BALL_SPD},
    "trail": [],
    "flash": None,
    "ticker": "",
    "last_ts": None,
    "seed": 0x2545F491,
}

_frame_proxy = None


# ---------------------------------------------------------------------------
def _qs(sel):
    return document.querySelector(sel)


def _rnd():
    state["seed"] = (1103515245 * state["seed"] + 12345) & 0x7FFFFFFF
    return state["seed"] / 0x7FFFFFFF


def _to_screen(fx, fy):
    return PX + fx * PW, PY + fy * PH


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _color(team):
    c = (team.get("color") or "").strip().lstrip("#")
    return f"#{c}" if re.fullmatch(r"[0-9a-fA-F]{6}", c) else ""


def _minute(clock_value):
    nums = re.findall(r"\d+", str(clock_value or ""))
    return int(nums[0]) + (int(nums[1]) if len(nums) > 1 else 0) if nums else 0


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
        who = next(((p.get("athlete") or {}).get("displayName")
                    for p in (ev.get("participants") or [])
                    if (p.get("athlete") or {}).get("displayName")), "")
        timeline.append({
            "minute": _minute((ev.get("clock") or {}).get("displayValue")),
            "seq": ev.get("sequence") or 0, "kind": "key",
            "side": side if side in ("home", "away") else "",
            "goal": ("goal" in low and "missed" not in low and "no goal" not in low),
            "type": etype, "who": who, "text": ev.get("text") or etype,
        })
    for cm in (summary.get("commentary") or []):
        timeline.append({
            "minute": _minute((cm.get("time") or {}).get("displayValue")),
            "seq": cm.get("sequence") or 0, "kind": "comm", "text": cm.get("text") or "",
        })

    timeline.sort(key=lambda e: (e["minute"], e["seq"]))
    state["timeline"] = timeline
    state["max_min"] = float(max(90, timeline[-1]["minute"] if timeline else 90))
    _build_formation()
    state["ready"] = True
    _reset()


def _build_formation():
    """4-3-3 for both sides in field coords (home attacks +x)."""
    players = []
    # (x, [y...], role) — role 'gk' stays home.
    home = [(0.06, [0.5], "gk"), (0.22, [0.2, 0.4, 0.6, 0.8], "def"),
            (0.40, [0.3, 0.5, 0.7], "mid"), (0.50, [0.25, 0.5, 0.75], "fwd")]
    away = [(0.94, [0.5], "gk"), (0.78, [0.2, 0.4, 0.6, 0.8], "def"),
            (0.60, [0.3, 0.5, 0.7], "mid"), (0.50, [0.25, 0.5, 0.75], "fwd")]
    num = {"home": 1, "away": 1}
    for layout, side in ((home, "home"), (away, "away")):
        for fx, ys, role in layout:
            for fy in ys:
                players.append({
                    "x": fx, "y": fy, "bx": fx, "by": fy, "tx": fx, "ty": fy,
                    "side": side, "role": role, "num": num[side],
                })
                num[side] += 1
    state["players"] = players


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def _attack_dir(side):
    return 1.0 if side == "home" else -1.0


def _goal_x(side):
    return 1.0 if side == "home" else 0.0


def _teammates(side):
    return [p for p in state["players"] if p["side"] == side]


def _ball_owner():
    o = state["ball"]["owner"]
    return state["players"][o] if 0 <= o < len(state["players"]) else None


def set_targets():
    b = state["ball"]
    poss = state["poss"]
    adv = b["x"] - 0.5
    owner = _ball_owner()

    # find nearest defender to press the ball
    presser = None
    best = 9e9
    for p in state["players"]:
        if p["side"] != poss and p["role"] != "gk":
            d = (p["x"] - b["x"]) ** 2 + (p["y"] - b["y"]) ** 2
            if d < best:
                best, presser = d, p

    shot_side = b["shot"] if b["flight"] else None
    for p in state["players"]:
        atk = _attack_dir(p["side"])
        if p["role"] == "gk":
            p["tx"] = _clamp(0.04 if p["side"] == "home" else 0.96)
            if shot_side and shot_side != p["side"]:
                p["ty"] = _clamp(b["ty"], 0.30, 0.70)  # dive across the goal
            else:
                p["ty"] = _clamp(0.5 + (b["y"] - 0.5) * 0.4, 0.3, 0.7)
            continue
        if p["side"] == poss:
            if owner is not None and p is owner:
                continue  # owner target handled by decide()
            p["tx"] = _clamp(p["bx"] + atk * 0.14 + adv * 0.18)
            p["ty"] = _clamp(p["by"] + (b["y"] - 0.5) * 0.18)
        else:
            if p is presser:
                p["tx"], p["ty"] = _clamp(b["x"]), _clamp(b["y"])
            else:
                p["tx"] = _clamp(p["bx"] - atk * 0.05 + adv * 0.16)
                p["ty"] = _clamp(p["by"] + (b["y"] - 0.5) * 0.28)


def decide():
    """Ball owner passes or dribbles toward goal."""
    if state["reset_timer"] > 0:
        return
    b = state["ball"]
    owner = _ball_owner()
    if owner is None or b["flight"]:
        return
    gx = _goal_x(owner["side"])
    atk = _attack_dir(owner["side"])
    mates = [p for p in _teammates(owner["side"]) if p is not owner and p["role"] != "gk"]

    # Shoot when advanced into the final third with a plausible chance.
    if (gx - owner["x"]) * atk < SHOT_RANGE and owner["role"] != "gk" and _rnd() < SHOT_CHANCE:
        _shoot(owner)
        return

    if _rnd() < 0.62 and mates:
        # pass: prefer a teammate further toward goal, fairly close
        atk = _attack_dir(owner["side"])
        def score(m):
            ahead = (m["x"] - owner["x"]) * atk
            dist = math.hypot(m["x"] - owner["x"], m["y"] - owner["y"])
            return ahead * 1.5 - dist + _rnd() * 0.3
        target = max(mates, key=score)
        b["flight"] = True
        b["owner"] = -1
        b["pending"] = state["players"].index(target)
        b["tx"], b["ty"] = target["x"], target["y"]
    else:
        # dribble toward goal
        owner["tx"] = _clamp(owner["x"] + _attack_dir(owner["side"]) * 0.16)
        owner["ty"] = _clamp(owner["y"] + (_rnd() - 0.5) * 0.2)


def _move_toward(p, dt, speed):
    dx, dy = p["tx"] - p["x"], p["ty"] - p["y"]
    d = math.hypot(dx, dy)
    if d < 1e-4:
        return
    step = min(d, speed * dt)
    p["x"] += dx / d * step
    p["y"] += dy / d * step


def update(ts):
    if state["last_ts"] is None:
        state["last_ts"] = ts
    real_dt = min((ts - state["last_ts"]) / 1000.0, 0.1)
    state["last_ts"] = ts
    dt = real_dt * SPEEDS[state["speed_idx"]] if state["playing"] else 0.0

    if dt > 0:
        # Narrative beats
        state["beat_timer"] += dt
        guard = 0
        while state["beat_timer"] >= BEAT and guard < 12:
            state["beat_timer"] -= BEAT
            advance_beat()
            guard += 1
        # Engine decisions
        state["pass_timer"] += dt
        if state["pass_timer"] >= PASS_EVERY:
            state["pass_timer"] = 0.0
            decide()
        if state["reset_timer"] > 0:
            state["reset_timer"] = max(0.0, state["reset_timer"] - dt)
            if state["reset_timer"] == 0:
                _kickoff()
        # Possession clock (skip dead-ball celebration time).
        if state["reset_timer"] == 0:
            state["poss_time"][0 if state["poss"] == "home" else 1] += dt

    set_targets()
    b = state["ball"]

    # Players move; presser sprints.
    poss = state["poss"]
    for p in state["players"]:
        spd = SPRINT if (p["side"] != poss and (p["tx"] - b["x"]) ** 2 + (p["ty"] - b["y"]) ** 2 < 0.01) else SPD
        _move_toward(p, dt, spd)

    # Ball physics
    if b["flight"]:
        dx, dy = b["tx"] - b["x"], b["ty"] - b["y"]
        d = math.hypot(dx, dy)
        step = b.get("speed", BALL_SPD) * dt
        if d <= step or d < 1e-3:
            b["x"], b["y"] = b["tx"], b["ty"]
            b["flight"] = False
            b["speed"] = BALL_SPD
            if b["shot"]:
                shooter_side, b["shot"] = b["shot"], None
                _resolve_shot(shooter_side)
            elif b["pending"] >= 0:
                b["owner"] = b["pending"]
                b["pending"] = -1
        else:
            b["x"] += dx / d * step
            b["y"] += dy / d * step
    else:
        owner = _ball_owner()
        if owner is not None:
            atk = _attack_dir(owner["side"])
            b["x"] += (owner["x"] + atk * 0.02 - b["x"]) * min(1.0, dt * 12)
            b["y"] += (owner["y"] - b["y"]) * min(1.0, dt * 12)

    # Motion trail (kept short; fades behind the ball).
    if dt > 0:
        state["trail"].append((b["x"], b["y"]))
        if len(state["trail"]) > 12:
            state["trail"].pop(0)

    if state["flash"]:
        state["flash"]["t"] -= real_dt
        if state["flash"]["t"] <= 0:
            state["flash"] = None

    _update_hud()


# ---------------------------------------------------------------------------
# Narrative beats (from the ESPN timeline)
# ---------------------------------------------------------------------------
def _poss_from_text(text):
    low = text.lower()
    for side in ("home", "away"):
        for tok in state[side]["tokens"]:
            if tok and tok in low:
                return side
    return ""


def _give_ball_to(side):
    """Hand possession to the team's nearest outfield player to the ball."""
    state["poss"] = side
    b = state["ball"]
    best, who = 9e9, None
    for i, p in enumerate(state["players"]):
        if p["side"] == side and p["role"] != "gk":
            d = (p["x"] - b["x"]) ** 2 + (p["y"] - b["y"]) ** 2
            if d < best:
                best, who = d, i
    if who is not None:
        b["owner"], b["flight"], b["pending"] = who, False, -1


def _gk(side):
    """(index, player) of a side's goalkeeper, or (-1, None)."""
    for i, p in enumerate(state["players"]):
        if p["side"] == side and p["role"] == "gk":
            return i, p
    return -1, None


def _shoot(owner):
    """Launch a shot at the goal mouth. Shots never score (the timeline owns
    the scoreline); they resolve as a save or an off-target turnover."""
    b = state["ball"]
    side = owner["side"]
    state["shots"][0 if side == "home" else 1] += 1
    b.update({
        "flight": True, "owner": -1, "pending": -1, "shot": side,
        "speed": SHOT_SPD, "tx": _goal_x(side),
        "ty": _clamp(0.5 + (_rnd() - 0.5) * 0.34, 0.32, 0.68),
    })


def _resolve_shot(side):
    """A shot reached the goal line: keeper saves or it goes wide. Either way
    possession turns over to the defending keeper to restart play."""
    b = state["ball"]
    defend = "away" if side == "home" else "home"
    _, gk = _gk(defend)
    saved = gk is not None and (abs(gk["y"] - b["y"]) < 0.11 or _rnd() < 0.4)
    state["flash"] = {"text": "SAVE!" if saved else "OFF TARGET",
                      "sub": "", "t": 1.2, "big": False}
    _hold_with_gk(defend)


def _hold_with_gk(side):
    """Give the ball to a side's keeper and let them restart possession."""
    i, gk = _gk(side)
    state["poss"] = side
    b = state["ball"]
    if gk is not None:
        b.update({"x": gk["x"], "y": gk["y"], "tx": gk["x"], "ty": gk["y"],
                  "owner": i, "flight": False, "pending": -1, "shot": None})


def advance_beat():
    if state["idx"] >= len(state["timeline"]):
        state["playing"] = False
        _qs("#play-btn").innerText = "▶"
        state["flash"] = {"text": "FULL TIME", "sub": "", "t": 4.0, "big": True}
        return

    e = state["timeline"][state["idx"]]
    state["idx"] += 1
    state["clock"] = float(e["minute"])
    state["ticker"] = e.get("text", "")

    if e["kind"] == "key":
        if e["goal"] and e["side"]:
            _score_goal(e)
            return
        low = e["type"].lower()
        icon = "🟨" if "yellow" in low else "🟥" if "red" in low else "🔁" if "sub" in low else ""
        state["flash"] = {"text": f"{icon} {e['type'].upper()}".strip(),
                          "sub": f"{e['minute']}'  {e['who']}".strip(), "t": 1.6, "big": False}

    side = _poss_from_text(e.get("text", ""))
    if side and side != state["poss"] and state["reset_timer"] == 0:
        _give_ball_to(side)


def _score_goal(e):
    idx = 0 if e["side"] == "home" else 1
    state["score"][idx] += 1
    state["shots"][idx] += 1  # a goal is a shot on target
    _update_score()
    team = state["home"] if e["side"] == "home" else state["away"]
    state["flash"] = {"text": "GOAL!", "sub": f"{e['minute']}'  {e['who'] or team['name']}",
                      "t": 2.4, "big": True}
    b = state["ball"]
    b["flight"], b["owner"], b["pending"], b["shot"] = True, -1, -1, None
    b["speed"] = SHOT_SPD
    b["tx"], b["ty"] = _goal_x(e["side"]), 0.5
    state["reset_timer"] = 1.8
    state["_kickoff_to"] = "away" if e["side"] == "home" else "home"


def _kickoff():
    for p in state["players"]:
        p["x"], p["y"], p["tx"], p["ty"] = p["bx"], p["by"], p["bx"], p["by"]
    state["ball"].update({"x": 0.5, "y": 0.5, "tx": 0.5, "ty": 0.5,
                          "flight": False, "pending": -1, "shot": None, "speed": BALL_SPD})
    _give_ball_to(state.get("_kickoff_to", "home"))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _draw_pitch():
    stripes = 12
    for i in range(stripes):
        ctx.fillStyle = "#2f8a3e" if i % 2 == 0 else "#2a7d39"
        ctx.fillRect(PX + i * PW / stripes, PY, PW / stripes + 1, PH)

    ctx.strokeStyle = "rgba(255,255,255,0.85)"
    ctx.lineWidth = 2
    ctx.strokeRect(PX, PY, PW, PH)
    ctx.beginPath()
    ctx.moveTo(PX + PW / 2, PY)
    ctx.lineTo(PX + PW / 2, PY + PH)
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(PX + PW / 2, PY + PH / 2, PH * 0.13, 0, math.pi * 2)
    ctx.stroke()
    # penalty + goal boxes
    bh, bw = PH * 0.5, PW * 0.13
    sh, sw = PH * 0.24, PW * 0.05
    for left in (True, False):
        x = PX if left else PX + PW - bw
        ctx.strokeRect(x, PY + (PH - bh) / 2, bw, bh)
        xs = PX if left else PX + PW - sw
        ctx.strokeRect(xs, PY + (PH - sh) / 2, sw, sh)
    # goals
    gh = PH * 0.16
    ctx.fillStyle = "rgba(255,255,255,0.25)"
    ctx.fillRect(PX - 6, PY + (PH - gh) / 2, 6, gh)
    ctx.fillRect(PX + PW, PY + (PH - gh) / 2, 6, gh)


def _draw_player(p, highlight):
    sx, sy = _to_screen(p["x"], p["y"])
    color = state[p["side"]]["color"]
    if highlight:
        ctx.fillStyle = "rgba(255,212,59,0.40)"
        ctx.beginPath()
        ctx.arc(sx, sy, 13, 0, math.pi * 2)
        ctx.fill()
    ctx.fillStyle = "rgba(0,0,0,0.30)"
    ctx.beginPath()
    ctx.ellipse(sx, sy + 9, 7, 3, 0, 0, math.pi * 2)
    ctx.fill()
    ctx.fillStyle = color
    ctx.beginPath()
    ctx.arc(sx, sy, 8, 0, math.pi * 2)
    ctx.fill()
    ctx.strokeStyle = "rgba(0,0,0,0.5)"
    ctx.lineWidth = 1
    ctx.stroke()
    ctx.fillStyle = "#fff"
    ctx.font = "bold 10px monospace"
    ctx.textAlign = "center"
    ctx.textBaseline = "middle"
    ctx.fillText(str(p["num"]), sx, sy)


def _draw_trail():
    trail = state["trail"]
    n = len(trail)
    for i, (fx, fy) in enumerate(trail[:-1]):
        sx, sy = _to_screen(fx, fy)
        frac = (i + 1) / n
        ctx.fillStyle = f"rgba(255,255,255,{frac * 0.22:.3f})"
        ctx.beginPath()
        ctx.arc(sx, sy, 1 + frac * 3, 0, math.pi * 2)
        ctx.fill()


def _draw_ball():
    b = state["ball"]
    sx, sy = _to_screen(b["x"], b["y"])
    ctx.fillStyle = "rgba(0,0,0,0.3)"
    ctx.beginPath()
    ctx.ellipse(sx, sy + 5, 5, 2, 0, 0, math.pi * 2)
    ctx.fill()
    ctx.fillStyle = "#fff"
    ctx.beginPath()
    ctx.arc(sx, sy, 5, 0, math.pi * 2)
    ctx.fill()
    ctx.fillStyle = "#111"
    ctx.fillRect(sx - 1.5, sy - 1.5, 3, 3)


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
    _draw_trail()
    owner = state["ball"]["owner"]
    for i, p in enumerate(state["players"]):
        _draw_player(p, i == owner)
    _draw_ball()

    # HUD bar: score/clock on top, shots + possession below.
    ctx.textBaseline = "alphabetic"
    ctx.fillStyle = "rgba(3,6,15,0.82)"
    ctx.fillRect(W / 2 - 170, 8, 340, 46)
    ctx.fillStyle = "#fff"
    ctx.font = "13px 'Press Start 2P', monospace"
    ctx.textAlign = "center"
    ctx.fillText(
        f'{state["home"]["abbr"]} {state["score"][0]} - {state["score"][1]} {state["away"]["abbr"]}'
        f'   {int(state["clock"])}\'', W / 2, 27)
    tot = state["poss_time"][0] + state["poss_time"][1] or 1.0
    ph = round(100 * state["poss_time"][0] / tot)
    ctx.fillStyle = "#9fb6c8"
    ctx.font = "8px 'Press Start 2P', monospace"
    ctx.fillText(
        f'SH {state["shots"][0]}-{state["shots"][1]}    POS {ph}%-{100 - ph}%',
        W / 2, 47)

    flash = state["flash"]
    if flash and flash["big"]:
        ctx.fillStyle = "#ffd43b"
        ctx.font = "52px 'Press Start 2P', monospace"
        ctx.fillText(flash["text"], W / 2, H / 2 - 6)
        if flash["sub"]:
            ctx.fillStyle = "#fff"
            ctx.font = "14px 'Press Start 2P', monospace"
            ctx.fillText(flash["sub"], W / 2, H / 2 + 30)
    elif flash:
        ctx.fillStyle = "rgba(0,0,0,0.6)"
        ctx.fillRect(W / 2 - 240, 60, 480, 26)
        ctx.fillStyle = "#fff"
        ctx.font = "11px 'Press Start 2P', monospace"
        ctx.fillText((flash["text"] + "  " + flash["sub"])[:52], W / 2, 78)


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
    state["beat_timer"] = state["pass_timer"] = state["reset_timer"] = 0.0
    state["score"] = [0, 0]
    state["shots"] = [0, 0]
    state["poss_time"] = [0.0, 0.0]
    state["trail"] = []
    state["flash"] = None
    state["ticker"] = ""
    state["poss"] = "home"
    _build_formation()
    state["ball"].update({"x": 0.5, "y": 0.5, "tx": 0.5, "ty": 0.5, "owner": -1,
                          "flight": False, "pending": -1, "shot": None, "speed": BALL_SPD})
    _give_ball_to("home")
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
