# 🐍 PyScript on GitHub Pages

A minimal, self-contained example of running **Python in the browser** with
[PyScript](https://pyscript.net) and serving it for free from **GitHub Pages**.
No backend, no build step — just static files.

## What's inside

| File | Purpose |
| --- | --- |
| `index.html` | Page markup, loads PyScript core, and mounts the app. |
| `main.py` | The Python application (runs in the browser via Pyodide). |
| `pyscript.toml` | PyScript configuration (packages, metadata). |
| `styles.css` | Styling for the page. |
| `.github/workflows/deploy.yml` | Auto-deploys the site to GitHub Pages on push. |

## Live demos on the page

1. **Live Python evaluator** — type an expression and evaluate it instantly.
2. **DOM interaction** — a counter wired to Python event handlers.
3. **Sieve of Eratosthenes** — run a real algorithm client-side.

## Run it locally

PyScript needs the files served over HTTP (not opened as `file://`). Any static
server works:

```bash
# Python's built-in server
python3 -m http.server 8000
# then open http://localhost:8000
```

## Deploy to GitHub Pages

The included workflow deploys automatically. To enable it:

1. Push this repository to GitHub.
2. Go to **Settings → Pages**.
3. Under **Build and deployment → Source**, choose **GitHub Actions**.
4. Push to the configured branch — the workflow builds and publishes the site,
   and the deployment URL appears in the Actions run summary.

## Customize

- **Add Python packages:** list them in `pyscript.toml` under `packages`,
  e.g. `packages = ["numpy", "matplotlib"]`.
- **Add a demo:** add markup to `index.html` and a `@when(...)` handler in
  `main.py`.

## How it works

PyScript loads [Pyodide](https://pyodide.org) (CPython compiled to
WebAssembly) into the browser. The `<script type="py">` tag runs `main.py`,
and the `pyscript` bridge (`document`, `when`) lets Python read and update the
DOM and respond to events — all on the client, with nothing sent to a server.
