"""Old-school arcade replay of a World Cup match, drawn on a <canvas>.

A 3/4-perspective stadium with a small soccer-simulation engine: two teams
in formation, possession, passing between teammates, pressing defenders, and
scripted shots / goals / kickoffs. The pitch is drawn in fake 3D and a camera
pans along the touchline to follow the ball; players are little sprites with
shadows and a crowd fills the stands. The narrative beat (possession side,
ticker text, goals, cards) is driven by ESPN's play-by-play feed; the engine
animates plausible movement in between.

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

# --- Perspective camera / projection ---------------------------------------
# The field is 0..1 in length (fx, home goal at 0) and width (fy, far
# touchline at 0). The camera shows a window VIEW_LEN long, panning along the
# length to follow the ball, and projects it onto a ground-plane trapezoid:
# the far touchline (fy=0) is higher and narrower, the near one (fy=1) lower
# and full-width, so the pitch recedes into the distance.
VIEW_LEN = 0.66          # fraction of pitch length visible in the viewport
FY_T, FY_B = 104, H - 34  # screen y of far / near touchlines
FAR_INSET = 0.16         # horizontal inset of the far edge (fraction of W)
CAM_LAG = 3.2            # camera follow smoothing (higher = snappier)
CAM_EDGE = 0.14         # how far past each goal the camera may travel
SC_FAR, SC_NEAR = 0.60, 1.20   # sprite/ball scale at far / near touchline
STAND_TOP = 14          # top of the far stand on screen

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
    "ticker_prev": None,
    "ticker_scroll": 0.0,
    "cam_x": 0.5,       # camera centre along the pitch length
    "anim_t": 0.0,      # wall-clock seconds, drives run cycle / flags / crowd
    "crowd": [],        # precomputed stand dots: (x, y, color, phase)
    "particles": [],    # screen-space confetti / dust
    "shake": 0.0,       # screen-shake magnitude (px), decays each frame
    "ball_air": 0.0,    # current visual hop height of the ball (px)
    "sound": True,      # mute toggle
    "audio": None,      # lazily created Web Audio context
    "last_ts": None,
    "seed": 0x2545F491,
}

_frame_proxy = None


# ---------------------------------------------------------------------------
def _qs(sel):
    return document.querySelector(sel)


def _set_text(sel, text):
    """Set an element's text if it exists (the canvas now owns most of the UI)."""
    el = _qs(sel)
    if el is not None:
        el.innerText = text


def _rnd():
    state["seed"] = (1103515245 * state["seed"] + 12345) & 0x7FFFFFFF
    return state["seed"] / 0x7FFFFFFF


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _depth(fy):
    """Foreshorten the width axis: far rows bunch toward the horizon."""
    d = _clamp(fy)
    return d * (0.80 + 0.20 * d)


def _project(fx, fy):
    """Field (fx, fy) -> (screen_x, screen_y, scale) on the perspective pitch."""
    d = _depth(fy)
    sy = FY_T + (FY_B - FY_T) * d
    inset = FAR_INSET * (1.0 - d)
    left = (0.012 + inset) * W
    right = (0.988 - inset) * W
    u = (fx - (state["cam_x"] - VIEW_LEN / 2)) / VIEW_LEN
    sx = left + (right - left) * u
    sc = SC_FAR + (SC_NEAR - SC_FAR) * d
    return sx, sy, sc


def _update_camera(dt_real):
    lo = VIEW_LEN / 2 - CAM_EDGE
    hi = 1.0 - VIEW_LEN / 2 + CAM_EDGE
    target = _clamp(state["ball"]["x"], lo, hi)
    state["cam_x"] += (target - state["cam_x"]) * min(1.0, dt_real * CAM_LAG)


def _darken(hexcolor, f):
    c = hexcolor.lstrip("#")
    if len(c) != 6:
        return hexcolor
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"


def _build_crowd():
    """Scatter colored dots across the far stand (computed once)."""
    palette = ["#e6edf3", "#ffd43b", "#ff7b72", "#5aa7ff", "#3fb950",
               "#d2a8ff", "#ffa657", "#f0f6fc"]
    crowd = []
    rows = 9
    for row in range(rows):
        y = STAND_TOP + row * (FY_T - STAND_TOP - 8) / rows
        step = 9 + row * 0.5  # nearer rows (lower) spaced a touch wider
        x = 4 + (row % 2) * step / 2
        while x < W - 4:
            crowd.append((x + (_rnd() - 0.5) * 4, y + (_rnd() - 0.5) * 3,
                          palette[int(_rnd() * len(palette))], _rnd() * 6.28))
            x += step
    state["crowd"] = crowd


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
                    "vx": 0.0, "vy": 0.0, "moving": False,
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
        _spawn_puff(owner["x"], owner["y"], 4)
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
        p["moving"] = False
        return
    step = min(d, speed * dt)
    p["x"] += dx / d * step
    p["y"] += dy / d * step
    p["vx"], p["vy"] = dx / d, dy / d
    p["moving"] = step > 1e-4 and dt > 0


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

    # Camera follows the ball; clock drives idle animation (run cycle, flags).
    _update_camera(real_dt)
    state["anim_t"] += real_dt

    # Ball hops slightly while in flight (a shot/pass arcs off the ground).
    target_air = 6.0 if b["flight"] else 0.0
    state["ball_air"] += (target_air - state["ball_air"]) * min(1.0, real_dt * 8)

    _update_particles(real_dt)
    if state["shake"] > 0:
        state["shake"] = max(0.0, state["shake"] - real_dt * 36.0)

    # Commentary ticker: restart the scroll whenever the line changes.
    if state["ticker"] != state["ticker_prev"]:
        state["ticker_prev"] = state["ticker"]
        state["ticker_scroll"] = 0.0
    state["ticker_scroll"] += real_dt * 70.0

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
    _spawn_puff(owner["x"], owner["y"], 8)
    _sfx("shot")
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
    _spawn_puff(b["x"], b["y"], 7)
    _sfx("save" if saved else "shot")
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
        _sfx("fulltime")
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
        _sfx("card")

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
    _spawn_confetti(team["color"])
    state["shake"] = 9.0
    _sfx("goal")
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
    _spawn_puff(0.5, 0.5, 6)
    _sfx("whistle")


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _proj_path(points, close):
    ctx.beginPath()
    for i, (fx, fy) in enumerate(points):
        sx, sy, _ = _project(fx, fy)
        if i == 0:
            ctx.moveTo(sx, sy)
        else:
            ctx.lineTo(sx, sy)
    if close:
        ctx.closePath()


def _fill_field_quad(corners, color):
    _proj_path(corners, True)
    ctx.fillStyle = color
    ctx.fill()


def _poly(points, close=True, color="rgba(255,255,255,0.85)", width=2.0):
    _proj_path(points, close)
    ctx.strokeStyle = color
    ctx.lineWidth = width
    ctx.stroke()


def _ellipse_field(cx, cy, rx, ry, color):
    pts = [(cx + rx * math.cos(i / 28 * math.tau), cy + ry * math.sin(i / 28 * math.tau))
           for i in range(28)]
    _poly(pts, close=True, color=color)


def _spot(fx, fy):
    sx, sy, sc = _project(fx, fy)
    ctx.fillStyle = "rgba(255,255,255,0.85)"
    ctx.beginPath()
    ctx.arc(sx, sy, max(1.0, 2.0 * sc), 0, math.tau)
    ctx.fill()


def _round_rect(x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    ctx.beginPath()
    ctx.moveTo(x + r, y)
    ctx.arcTo(x + w, y, x + w, y + h, r)
    ctx.arcTo(x + w, y + h, x, y + h, r)
    ctx.arcTo(x, y + h, x, y, r)
    ctx.arcTo(x, y, x + w, y, r)
    ctx.closePath()
    ctx.fill()


def _draw_goal(gx, behind):
    """A perspective goal frame + net at the goal line fx=gx."""
    mouth0, mouth1 = 0.42, 0.58
    f0, f1 = _project(gx, mouth0), _project(gx, mouth1)          # front posts
    b0, b1 = _project(gx + behind, mouth0), _project(gx + behind, mouth1)  # back
    h = 30 * (f0[2] + f1[2]) / 2
    hb = 30 * (b0[2] + b1[2]) / 2
    ft0, ft1 = (f0[0], f0[1] - h), (f1[0], f1[1] - h)
    bt0, bt1 = (b0[0], b0[1] - hb), (b1[0], b1[1] - hb)
    # Net grid on the back frame.
    ctx.strokeStyle = "rgba(255,255,255,0.20)"
    ctx.lineWidth = 1
    for k in range(1, 6):
        u = k / 6
        ax, ay = b0[0] + (b1[0] - b0[0]) * u, b0[1] + (b1[1] - b0[1]) * u
        ctx.beginPath()
        ctx.moveTo(ax, ay)
        ctx.lineTo(ax, ay - hb)
        ctx.stroke()
    for k in range(1, 4):
        v = k / 4
        ctx.beginPath()
        ctx.moveTo(b0[0], b0[1] - hb * v)
        ctx.lineTo(b1[0], b1[1] - hb * v)
        ctx.stroke()
    # Side netting (front -> back).
    ctx.strokeStyle = "rgba(255,255,255,0.16)"
    for fa, ba in ((ft0, bt0), (ft1, bt1)):
        ctx.beginPath()
        ctx.moveTo(*fa)
        ctx.lineTo(*ba)
        ctx.stroke()
    # Bright front frame (posts + crossbar).
    ctx.strokeStyle = "rgba(255,255,255,0.92)"
    ctx.lineWidth = 2.4
    for base, top in ((f0, ft0), (f1, ft1)):
        ctx.beginPath()
        ctx.moveTo(base[0], base[1])
        ctx.lineTo(*top)
        ctx.stroke()
    ctx.beginPath()
    ctx.moveTo(*ft0)
    ctx.lineTo(*ft1)
    ctx.stroke()


def _draw_stands():
    """Dark far stand filled with twinkling crowd dots, behind the pitch."""
    ctx.fillStyle = "#05070d"
    ctx.fillRect(0, 0, W, FY_T)
    t = state["anim_t"]
    for x, y, color, phase in state["crowd"]:
        ctx.globalAlpha = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 2.0 + phase))
        ctx.fillStyle = color
        ctx.fillRect(x, y, 2, 2)
    ctx.globalAlpha = 1.0
    # Barrier strip in front of the stand.
    ctx.fillStyle = "#0b1020"
    ctx.fillRect(0, FY_T - 5, W, 6)


def _draw_pitch():
    cam = state["cam_x"]
    lx0, lx1 = cam - VIEW_LEN / 2, cam + VIEW_LEN / 2
    bands = 11
    for i in range(bands):
        d0, d1 = i / bands, (i + 1) / bands
        shade = "#2f8a3e" if i % 2 == 0 else "#2a7d39"
        _fill_field_quad([(lx0, d0), (lx1, d0), (lx1, d1), (lx0, d1)], shade)

    line = "rgba(255,255,255,0.85)"
    _poly([(0, 0), (1, 0), (1, 1), (0, 1)], close=True, color=line)
    _poly([(0.5, 0), (0.5, 1)], close=False, color=line)
    _ellipse_field(0.5, 0.5, 0.085, 0.17, color=line)
    _spot(0.5, 0.5)
    for gx, s in ((0.0, 1.0), (1.0, -1.0)):
        _poly([(gx, 0.21), (gx + s * 0.16, 0.21),
               (gx + s * 0.16, 0.79), (gx, 0.79)], close=False, color=line)
        _poly([(gx, 0.37), (gx + s * 0.055, 0.37),
               (gx + s * 0.055, 0.63), (gx, 0.63)], close=False, color=line)
        _spot(gx + s * 0.11, 0.5)
    _draw_goal(0.0, -0.05)
    _draw_goal(1.0, 0.05)


def _draw_player(p, highlight):
    sx, sy, sc = _project(p["x"], p["y"])
    body = state[p["side"]]["color"]
    dark = _darken(body, 0.62)
    t = state["anim_t"]
    phase = math.sin(t * 13 + p["num"]) if p.get("moving") else 0.0
    bob = abs(phase) * 1.2 * sc
    face = 1.0 if p.get("vx", 0.0) >= 0 else -1.0

    if highlight:
        ctx.strokeStyle = "rgba(255,212,59,0.95)"
        ctx.lineWidth = 2
        ctx.beginPath()
        ctx.ellipse(sx, sy + 1, 10 * sc, 4.5 * sc, 0, 0, math.tau)
        ctx.stroke()

    ctx.fillStyle = "rgba(0,0,0,0.30)"
    ctx.beginPath()
    ctx.ellipse(sx, sy + 1, 6 * sc, 2.6 * sc, 0, 0, math.tau)
    ctx.fill()

    feet = sy - bob
    lh, lw = 6 * sc, 2.2 * sc
    swing = phase * 2.2 * sc
    ctx.fillStyle = dark
    ctx.fillRect(sx - 2.6 * sc + swing, feet - lh, lw, lh)
    ctx.fillRect(sx + 0.4 * sc - swing, feet - lh, lw, lh)

    th = 7.5 * sc
    ty = feet - lh - th
    ctx.fillStyle = body
    _round_rect(sx - 4 * sc, ty, 8 * sc, th, 1.6 * sc)
    ctx.fillStyle = "rgba(0,0,0,0.18)"
    ctx.fillRect(sx - 4 * sc, ty, 1.4 * sc, th)
    ctx.fillStyle = body
    ctx.fillRect(sx - 5.4 * sc, ty + 0.5 * sc, 1.6 * sc, th * 0.7)
    ctx.fillRect(sx + 3.8 * sc, ty + 0.5 * sc, 1.6 * sc, th * 0.7)

    hx, hy = sx + face * 0.4 * sc, ty - 3.0 * sc
    ctx.fillStyle = "#e8b48a"
    ctx.beginPath()
    ctx.arc(hx, hy, 3 * sc, 0, math.tau)
    ctx.fill()
    ctx.fillStyle = "#22150d"  # hair (top half)
    ctx.beginPath()
    ctx.arc(hx, hy, 3 * sc, math.pi, math.tau)
    ctx.fill()

    fs = max(6, round(7 * sc))
    ctx.fillStyle = "#fff"
    ctx.font = f"{fs}px 'Press Start 2P', monospace"
    ctx.textAlign = "center"
    ctx.textBaseline = "alphabetic"
    ctx.fillText(str(p["num"]), sx, sy + 11 * sc)


def _draw_trail():
    trail = state["trail"]
    n = len(trail)
    for i, (fx, fy) in enumerate(trail[:-1]):
        sx, sy, sc = _project(fx, fy)
        frac = (i + 1) / n
        ctx.fillStyle = f"rgba(255,255,255,{frac * 0.20:.3f})"
        ctx.beginPath()
        ctx.arc(sx, sy - state["ball_air"] * frac, (1 + frac * 3) * sc, 0, math.tau)
        ctx.fill()


def _draw_ball():
    b = state["ball"]
    sx, sy, sc = _project(b["x"], b["y"])
    air = state["ball_air"]
    ctx.fillStyle = "rgba(0,0,0,0.30)"
    ctx.beginPath()
    ctx.ellipse(sx, sy, 4.5 * sc, 1.8 * sc, 0, 0, math.tau)
    ctx.fill()
    by = sy - air
    r = 4.2 * sc
    ctx.fillStyle = "#fff"
    ctx.beginPath()
    ctx.arc(sx, by, r, 0, math.tau)
    ctx.fill()
    ctx.fillStyle = "#111"
    ctx.beginPath()
    ctx.arc(sx, by, r * 0.42, 0, math.tau)
    ctx.fill()
    ctx.strokeStyle = "rgba(0,0,0,0.4)"
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.arc(sx, by, r, 0, math.tau)
    ctx.stroke()


# ---------------------------------------------------------------------------
# Juice: particles + screen shake
# ---------------------------------------------------------------------------
def _spawn_confetti(color):
    for _ in range(70):
        state["particles"].append({
            "x": _rnd() * W, "y": -8 - _rnd() * 50,
            "vx": (_rnd() - 0.5) * 120, "vy": 60 + _rnd() * 150, "g": 90,
            "life": 1.6 + _rnd() * 1.4, "max": 3.0, "size": 2.5 + _rnd() * 3.5,
            "color": color if _rnd() < 0.55 else ("#fff" if _rnd() < 0.5 else "#ffd43b"),
        })


def _spawn_puff(fx, fy, n=6):
    sx, sy, sc = _project(fx, fy)
    for _ in range(n):
        a = _rnd() * math.tau
        v = (25 + _rnd() * 40) * sc
        state["particles"].append({
            "x": sx, "y": sy, "vx": math.cos(a) * v, "vy": math.sin(a) * v - 12,
            "g": 50, "life": 0.25 + _rnd() * 0.2, "max": 0.5,
            "size": (1.4 + _rnd() * 1.6) * sc, "color": "#eaf2ff",
        })


def _update_particles(rdt):
    parts = state["particles"]
    if not parts:
        return
    for p in parts:
        p["vy"] += p["g"] * rdt
        p["x"] += p["vx"] * rdt
        p["y"] += p["vy"] * rdt
        p["life"] -= rdt
    state["particles"] = [p for p in parts if p["life"] > 0 and p["y"] < H + 20]


def _draw_particles():
    for p in state["particles"]:
        ctx.globalAlpha = max(0.0, min(1.0, p["life"] / p["max"]))
        ctx.fillStyle = p["color"]
        ctx.fillRect(p["x"], p["y"], p["size"], p["size"])
    ctx.globalAlpha = 1.0


# ---------------------------------------------------------------------------
# Sound (Web Audio; all calls are no-ops when muted or unavailable)
# ---------------------------------------------------------------------------
def _audio_ctx():
    if state["audio"] is None and state["sound"]:
        try:
            AC = getattr(window, "AudioContext", None) or getattr(window, "webkitAudioContext", None)
            if AC is not None:
                state["audio"] = AC.new()
        except Exception:  # noqa: BLE001
            state["audio"] = None
    return state["audio"]


def _resume_audio():
    ac = _audio_ctx()
    try:
        if ac is not None and ac.state == "suspended":
            ac.resume()
    except Exception:  # noqa: BLE001
        pass


def _beep(freq, dur, kind="square", vol=0.18, slide=None, delay=0.0):
    if not state["sound"]:
        return
    ac = _audio_ctx()
    if ac is None:
        return
    try:
        t = ac.currentTime + delay
        osc = ac.createOscillator()
        gain = ac.createGain()
        osc.type = kind
        osc.frequency.setValueAtTime(freq, t)
        if slide:
            osc.frequency.exponentialRampToValueAtTime(max(40, slide), t + dur)
        gain.gain.setValueAtTime(vol, t)
        gain.gain.exponentialRampToValueAtTime(0.0008, t + dur)
        osc.connect(gain)
        gain.connect(ac.destination)
        osc.start(t)
        osc.stop(t + dur + 0.02)
    except Exception:  # noqa: BLE001
        pass


def _sfx(name):
    if name == "shot":
        _beep(320, 0.12, "sawtooth", 0.12, slide=120)
    elif name == "save":
        _beep(150, 0.14, "sawtooth", 0.16, slide=70)
    elif name == "card":
        _beep(180, 0.10, "square", 0.12)
    elif name == "whistle":
        _beep(1900, 0.10, "square", 0.10)
        _beep(2100, 0.10, "square", 0.10, delay=0.12)
    elif name == "fulltime":
        for i in range(3):
            _beep(1950, 0.14, "square", 0.11, delay=i * 0.18)
    elif name == "goal":
        for i, f in enumerate((523, 659, 784, 1047)):
            _beep(f, 0.18, "square", 0.16, delay=i * 0.12)


def _draw_ticker():
    """Scrolling commentary line + match progress along the bottom strip."""
    band_y = H - 40
    ctx.fillStyle = "rgba(3,6,15,0.88)"
    ctx.fillRect(0, band_y, W, 34)

    text = state["ticker"] or "PRESS PLAY"
    ctx.fillStyle = "#ffd43b"
    ctx.font = "10px 'Press Start 2P', monospace"
    ctx.textBaseline = "middle"
    cy = band_y + 13
    tw = ctx.measureText(text).width
    if tw <= W - 32:
        ctx.textAlign = "center"
        ctx.fillText(text, W / 2, cy)
    else:
        ctx.textAlign = "left"
        span = tw + 80
        off = state["ticker_scroll"] % span
        ctx.fillText(text, 16 - off, cy)
        ctx.fillText(text, 16 - off + span, cy)  # seamless wrap

    # progress bar
    n = len(state["timeline"]) or 1
    frac = min(1.0, state["idx"] / n)
    py = H - 5
    ctx.fillStyle = "rgba(255,255,255,0.14)"
    ctx.fillRect(0, py, W, 4)
    ctx.fillStyle = "#ffd43b"
    ctx.fillRect(0, py, W * frac, 4)


def _draw_hud():
    ctx.textBaseline = "alphabetic"
    bx, bw = W / 2 - 180, 360
    ctx.fillStyle = "rgba(3,6,15,0.82)"
    _round_rect(bx, 8, bw, 56, 8)
    ctx.fillStyle = "#fff"
    ctx.font = "14px 'Press Start 2P', monospace"
    ctx.textAlign = "center"
    ctx.fillText(
        f'{state["home"]["abbr"]} {state["score"][0]} - {state["score"][1]} {state["away"]["abbr"]}'
        f'   {int(state["clock"])}\'', W / 2, 30)
    ctx.fillStyle = "#9fb6c8"
    ctx.font = "8px 'Press Start 2P', monospace"
    ctx.fillText(f'SH {state["shots"][0]} - {state["shots"][1]}', W / 2, 45)
    # Possession bar, split in the two team colors.
    tot = state["poss_time"][0] + state["poss_time"][1] or 1.0
    hf = state["poss_time"][0] / tot
    pbw, pbx, pby, pbh = 240, W / 2 - 120, 52, 7
    ctx.fillStyle = state["home"]["color"]
    ctx.fillRect(pbx, pby, pbw * hf, pbh)
    ctx.fillStyle = state["away"]["color"]
    ctx.fillRect(pbx + pbw * hf, pby, pbw * (1 - hf), pbh)
    ctx.strokeStyle = "rgba(0,0,0,0.6)"
    ctx.lineWidth = 1
    ctx.strokeRect(pbx, pby, pbw, pbh)


def _draw_flash():
    flash = state["flash"]
    if not flash:
        return
    ctx.textAlign = "center"
    ctx.textBaseline = "alphabetic"
    if flash["big"]:
        ctx.fillStyle = "#ffd43b"
        ctx.font = "52px 'Press Start 2P', monospace"
        ctx.fillText(flash["text"], W / 2, H / 2 - 6)
        if flash["sub"]:
            ctx.fillStyle = "#fff"
            ctx.font = "14px 'Press Start 2P', monospace"
            ctx.fillText(flash["sub"], W / 2, H / 2 + 30)
    else:
        ctx.fillStyle = "rgba(0,0,0,0.6)"
        ctx.fillRect(W / 2 - 240, 70, 480, 26)
        ctx.fillStyle = "#fff"
        ctx.font = "11px 'Press Start 2P', monospace"
        ctx.fillText((flash["text"] + "  " + flash["sub"]).strip()[:52], W / 2, 88)


def draw():
    ctx.fillStyle = "#05070d"
    ctx.fillRect(0, 0, W, H)
    if not state["ready"]:
        ctx.fillStyle = "#9fb6c8"
        ctx.font = "16px 'Press Start 2P', monospace"
        ctx.textAlign = "center"
        ctx.textBaseline = "alphabetic"
        ctx.fillText("LOADING…", W / 2, H / 2)
        return

    # The world (stadium + pitch + sprites) shakes; the HUD overlay stays put.
    ctx.save()
    sh = state["shake"]
    if sh > 0.2:
        ctx.translate((_rnd() - 0.5) * sh, (_rnd() - 0.5) * sh)

    _draw_stands()
    _draw_pitch()
    _draw_trail()

    # Depth-sort players + ball so nearer sprites overlap farther ones.
    owner = state["ball"]["owner"]
    entities = [("p", i, p, p["y"]) for i, p in enumerate(state["players"])]
    entities.append(("b", -1, None, state["ball"]["y"]))
    entities.sort(key=lambda e: e[3])
    for kind, i, p, _y in entities:
        if kind == "p":
            _draw_player(p, i == owner)
        else:
            _draw_ball()
    _draw_particles()
    ctx.restore()

    _draw_hud()
    _draw_flash()
    _draw_ticker()


def frame(ts):
    update(ts)
    draw()
    window.requestAnimationFrame(_frame_proxy)


# ---------------------------------------------------------------------------
# HUD / DOM
# ---------------------------------------------------------------------------
def _update_score():
    # Score is rendered on the canvas HUD; keep optional DOM mirrors in sync.
    _set_text("#sb-score", f'{state["score"][0]} - {state["score"][1]}')


def _update_hud():
    # The clock, progress and commentary now live on the canvas; only mirror to
    # DOM if those (optional) elements are present.
    _set_text("#sb-clock", f'{int(state["clock"])}\'')
    _set_text("#caption", state["ticker"])


def _reset():
    state["clock"] = 0.0
    state["idx"] = 0
    state["beat_timer"] = state["pass_timer"] = state["reset_timer"] = 0.0
    state["score"] = [0, 0]
    state["shots"] = [0, 0]
    state["poss_time"] = [0.0, 0.0]
    state["trail"] = []
    state["particles"] = []
    state["shake"] = 0.0
    state["ball_air"] = 0.0
    state["cam_x"] = 0.5
    state["flash"] = None
    state["ticker"] = ""
    state["poss"] = "home"
    _build_formation()
    state["ball"].update({"x": 0.5, "y": 0.5, "tx": 0.5, "ty": 0.5, "owner": -1,
                          "flight": False, "pending": -1, "shot": None, "speed": BALL_SPD})
    _give_ball_to("home")
    _set_text("#sb-home", state["home"]["abbr"])
    _set_text("#sb-away", state["away"]["abbr"])
    _update_score()


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
@when("click", "#play-btn")
def toggle_play(event=None):
    _resume_audio()
    if not state["ready"]:
        return
    if state["idx"] >= len(state["timeline"]):
        _reset()
    state["playing"] = not state["playing"]
    _qs("#play-btn").innerText = "⏸" if state["playing"] else "▶"


@when("click", "#restart-btn")
def restart(event=None):
    _resume_audio()
    _reset()
    state["playing"] = True
    _qs("#play-btn").innerText = "⏸"


@when("click", "#speed-btn")
def cycle_speed(event=None):
    _resume_audio()
    state["speed_idx"] = (state["speed_idx"] + 1) % len(SPEEDS)
    _qs("#speed-btn").innerText = f"{SPEEDS[state['speed_idx']]}×"


@when("click", "#mute-btn")
def toggle_mute(event=None):
    state["sound"] = not state["sound"]
    _qs("#mute-btn").innerText = "🔊" if state["sound"] else "🔇"
    if state["sound"]:
        _resume_audio()


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
    _build_crowd()
    _frame_proxy = create_proxy(frame)
    window.requestAnimationFrame(_frame_proxy)

    event_id = _event_id()
    if not event_id:
        state["ticker"] = "NO MATCH SELECTED"
    else:
        try:
            resp = await fetch(f"{SUMMARY_URL}?event={event_id}")
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}")
            parse_summary(await resp.json())
            if not state["timeline"]:
                state["ticker"] = "NO PLAY-BY-PLAY FOR THIS MATCH YET"
            else:
                state["playing"] = True
                _set_text("#play-btn", "⏸")
        except Exception as exc:  # noqa: BLE001
            state["ticker"] = f"FAILED TO LOAD MATCH: {exc}"

    splash = _qs("#loading")
    if splash:
        splash.style.display = "none"


asyncio.ensure_future(boot())
