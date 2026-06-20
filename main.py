"""PyScript application logic for the GitHub Pages demo.

Everything in this file runs inside the browser via Pyodide. We use the
`pyscript` bridge to reach into the DOM and wire up event handlers.
"""

from pyscript import document, when


# ---------------------------------------------------------------------------
# Demo 1: Live Python expression evaluator
# ---------------------------------------------------------------------------
@when("click", "#eval-btn")
def evaluate_expression(event=None):
    expr = document.querySelector("#expr-input").value
    output = document.querySelector("#eval-output")
    try:
        # eval is fine here: this runs sandboxed in the user's own browser.
        result = eval(expr, {"__builtins__": __builtins__}, {})
        output.classList.remove("error")
        output.innerText = f"=> {result!r}"
    except Exception as exc:  # noqa: BLE001 - surface any error to the user
        output.classList.add("error")
        output.innerText = f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Demo 2: Counter with DOM interaction
# ---------------------------------------------------------------------------
_count = 0


def _render_count():
    document.querySelector("#counter").innerText = str(_count)


@when("click", "#inc-btn")
def increment(event=None):
    global _count
    _count += 1
    _render_count()


@when("click", "#dec-btn")
def decrement(event=None):
    global _count
    _count -= 1
    _render_count()


# ---------------------------------------------------------------------------
# Demo 3: Sieve of Eratosthenes
# ---------------------------------------------------------------------------
def primes_up_to(n: int) -> list[int]:
    if n < 2:
        return []
    sieve = bytearray([1]) * (n + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, int(n ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i :: i] = bytearray(len(sieve[i * i :: i]))
    return [i for i, is_prime in enumerate(sieve) if is_prime]


@when("click", "#prime-btn")
def compute_primes(event=None):
    output = document.querySelector("#prime-output")
    try:
        n = int(document.querySelector("#prime-n").value)
        primes = primes_up_to(n)
        output.classList.remove("error")
        preview = ", ".join(str(p) for p in primes[:50])
        suffix = " …" if len(primes) > 50 else ""
        output.innerText = (
            f"Found {len(primes)} primes ≤ {n}:\n{preview}{suffix}"
        )
    except Exception as exc:  # noqa: BLE001
        output.classList.add("error")
        output.innerText = f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def main():
    # Run each demo once so the page shows live output immediately.
    evaluate_expression()
    _render_count()
    compute_primes()

    # Hide the loading splash now that Python is ready.
    splash = document.querySelector("#loading")
    if splash:
        splash.style.display = "none"


main()
