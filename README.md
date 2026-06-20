# 🏆 World Cup Scores — Python in the Browser

A static site that fetches **FIFA World Cup** fixtures from the public **ESPN
API** and renders them entirely in **Python** using [PyScript](https://pyscript.net).
No JavaScript, no backend — just static files served from **GitHub Pages**.

## What's inside

| File | Purpose |
| --- | --- |
| `index.html` | Scoreboard page; loads PyScript and mounts the app. |
| `main.py` | Fetches ESPN data and renders match cards (runs in the browser). |
| `match.html` | The arcade match-replay page (`match.html?event=<id>`). |
| `replay.py` | Canvas game-loop that animates a match's key events. |
| `pyscript.toml` | PyScript configuration. |
| `styles.css` | Styling for the scoreboard and the arcade page. |
| `.github/workflows/deploy.yml` | Auto-deploys the site to GitHub Pages on push. |

## Arcade match replay

Clicking a match opens `match.html?event=<id>`, an **old-school video-game
replay** drawn on an HTML5 `<canvas>` and driven by a `requestAnimationFrame`
loop — written in Python (the browser cousin of an iOS SpriteKit scene). A
virtual match clock advances and the match's key events (goals, cards,
substitutions) fire at their minute, with a retro pixel pitch, scoreboard,
scanline/CRT overlay, and play / restart / speed controls. Match details come
from ESPN's summary endpoint:

```
https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event=<id>
```

## Data source

The app reads from ESPN's public soccer scoreboard endpoint for the FIFA World
Cup league (`fifa.world`):

```
https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard
```

An optional `?dates=YYYYMMDD` query parameter selects a specific matchday. The
date picker on the page builds this for you. These ESPN endpoints send
permissive CORS headers, so the browser can fetch them directly from the
static site.

## Features

- **Date picker** to load fixtures for any day of the tournament.
- **Live / Final / Upcoming** states, with live matches sorted to the top and a
  pulsing indicator.
- **Auto-refresh** (every 30s) toggle for following matches in progress.
- Team logos, scores, group/round labels, and venue — all rendered from Python.

## How the Python fetches data

PyScript exposes the browser's `fetch` to Python:

```python
from pyscript import fetch

response = await fetch(url)
data = await response.json()
```

The `@when(...)` decorators wire button clicks and the auto-refresh checkbox to
async Python handlers — no JavaScript involved.

## Run it locally

PyScript needs the files served over HTTP (not `file://`):

```bash
python3 -m http.server 8000
# then open http://localhost:8000
```

## Deploy to GitHub Pages

The included workflow deploys automatically:

1. **Settings → Pages → Build and deployment → Source → GitHub Actions.**
2. Push to the configured branch; the workflow publishes the site and the URL
   appears in the Actions run summary (and in Settings → Pages).

> Not affiliated with ESPN or FIFA. Uses publicly accessible API endpoints.
