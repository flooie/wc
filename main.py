"""World Cup scores, fetched and rendered in Python via PyScript.

Runs entirely in the browser (Pyodide). Data comes from ESPN's public
soccer scoreboard API for the FIFA World Cup league (`fifa.world`).

ESPN endpoint:
    https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard
    ?dates=YYYYMMDD   (optional; defaults to the current matchday)
"""

import asyncio
from datetime import date

from pyscript import document, fetch, when

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
)

# Handle to the running auto-refresh loop so we can cancel it.
_auto_task = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _q(selector):
    return document.querySelector(selector)


def _set_status(message, *, error=False):
    el = _q("#status-line")
    el.innerText = message
    el.classList.toggle("error", error)


def _esc(text):
    """Minimal HTML escaping for values we inject via innerHTML."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _competitors(competition):
    """Return (home, away) competitor dicts, tolerant of ordering."""
    comps = competition.get("competitors", [])
    home = next((c for c in comps if c.get("homeAway") == "home"), None)
    away = next((c for c in comps if c.get("homeAway") == "away"), None)
    # Fall back to positional order if homeAway is missing.
    if home is None or away is None:
        home = comps[0] if comps else {}
        away = comps[1] if len(comps) > 1 else {}
    return home, away


def _team_block(competitor, *, align):
    team = competitor.get("team", {}) or {}
    name = team.get("shortDisplayName") or team.get("displayName") or "TBD"
    abbr = team.get("abbreviation", "")
    logo = team.get("logo", "")
    winner = competitor.get("winner") is True
    logo_html = (
        f'<img class="logo" src="{_esc(logo)}" alt="{_esc(abbr)}" loading="lazy" />'
        if logo
        else '<span class="logo placeholder">⚽</span>'
    )
    win_cls = " winner" if winner else ""
    return (
        f'<div class="team {align}{win_cls}">'
        f"{logo_html}"
        f'<span class="team-name">{_esc(name)}</span>'
        f"</div>"
    )


def _match_card(event):
    competition = (event.get("competitions") or [{}])[0]
    home, away = _competitors(competition)

    status = event.get("status", {}) or {}
    stype = status.get("type", {}) or {}
    state = stype.get("state", "")  # "pre" | "in" | "post"
    detail = stype.get("shortDetail") or stype.get("detail") or ""

    home_score = home.get("score", "") if state != "pre" else ""
    away_score = away.get("score", "") if state != "pre" else ""

    # Group / round label, e.g. "Group A".
    notes = competition.get("notes") or []
    note = notes[0].get("headline") if notes else ""

    venue = (competition.get("venue") or {}).get("fullName", "")

    state_cls = {"in": "live", "post": "final", "pre": "upcoming"}.get(state, "")
    score_html = (
        f'<span class="score">{_esc(home_score)}</span>'
        f'<span class="sep">–</span>'
        f'<span class="score">{_esc(away_score)}</span>'
        if state != "pre"
        else '<span class="sep">vs</span>'
    )

    meta_bits = " · ".join(b for b in (_esc(note), _esc(venue)) if b)

    return (
        f'<article class="match {state_cls}">'
        f'  <div class="match-status {state_cls}">{_esc(detail)}</div>'
        f'  <div class="scoreline">'
        f"    {_team_block(home, align='home')}"
        f'    <div class="score-box">{score_html}</div>'
        f"    {_team_block(away, align='away')}"
        f"  </div>"
        f'  <div class="match-meta">{meta_bits}</div>'
        f"</article>"
    )


def _render(data):
    events = data.get("events", []) or []
    container = _q("#matches")
    if not events:
        league = (data.get("leagues") or [{}])[0].get("name", "World Cup")
        container.innerHTML = (
            f'<p class="empty">No {_esc(league)} fixtures scheduled for this date.</p>'
        )
        return

    # Live matches first, then upcoming, then finished.
    order = {"in": 0, "pre": 1, "post": 2}

    def sort_key(ev):
        st = ((ev.get("status") or {}).get("type") or {}).get("state", "")
        return (order.get(st, 3), ev.get("date", ""))

    cards = "".join(_match_card(ev) for ev in sorted(events, key=sort_key))
    container.innerHTML = cards


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
async def load_matches(event=None):
    date_str = _q("#date-input").value  # "YYYY-MM-DD"
    url = SCOREBOARD_URL
    if date_str:
        url += f"?dates={date_str.replace('-', '')}"

    _set_status("Loading…")
    try:
        response = await fetch(url)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}")
        data = await response.json()
    except Exception as exc:  # noqa: BLE001
        _set_status(f"Failed to load: {exc}", error=True)
        return

    _render(data)
    now = data.get("day", {}).get("date") or date_str or "today"
    _set_status(f"Updated {now} · {len(data.get('events', []) or [])} match(es)")


@when("click", "#load-btn")
async def on_load_click(event=None):
    await load_matches()


@when("change", "#auto-refresh")
def on_auto_toggle(event=None):
    global _auto_task
    enabled = _q("#auto-refresh").checked
    if enabled and _auto_task is None:
        _auto_task = asyncio.ensure_future(_auto_refresh_loop())
    elif not enabled and _auto_task is not None:
        _auto_task.cancel()
        _auto_task = None


async def _auto_refresh_loop():
    try:
        while True:
            await asyncio.sleep(30)
            await load_matches()
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
async def main():
    _q("#date-input").value = date.today().isoformat()
    await load_matches()
    splash = _q("#loading")
    if splash:
        splash.style.display = "none"


asyncio.ensure_future(main())
