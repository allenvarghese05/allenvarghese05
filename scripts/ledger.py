#!/usr/bin/env python3
"""ledger.py: check every figure on this profile before renewing its stamp.

Telltale's rule is that a claim plain code can't reproduce never reaches the
report. This applies the same rule to README.md, with three checks:

  L1 Recompute  Facts marked "recomputed" are derived again from data committed
                to a public repo, read at a pinned commit, and must equal the
                figure shown.
  L2 Match      Every fact's proof phrases must appear on the published
                portfolio (allenvarghese.com/projects), so this page can never
                say more than the case study behind it.
  L3 Cite       Every figure in README.md must be a link to a fact, with the
                same visible text as that fact. Any other digit (outside years
                and the product names listed in facts.json) is blocked.

Usage
  python scripts/ledger.py            check only; exit 0 on a full pass, 1 otherwise
  python scripts/ledger.py --stamp    check, then rewrite assets/ledger-{dark,light}.svg

Offline / local runs (no network needed):
  --portfolio DIR               match proofs against DIR/src/data/**/*.js instead of the live site
  --repo OWNER/NAME=DIR         read a pinned repo's files with `git show` from a local clone

Python 3.10+ standard library only: nothing to install.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
FACTS = ROOT / "facts.json"
ASSETS = ROOT / "assets"

UA = "Mozilla/5.0 (compatible; allenvarghese05-ledger/1.0; +https://github.com/allenvarghese05/allenvarghese05)"
TIMEOUT = 20
FACT_LINK = re.compile(r'\[([^\]\n]+)\]\(([^)\s]+)\s+"fact:([A-Za-z0-9_.-]+)[^"]*"\)')


class Blocked(Exception):
    """A check that could not pass. The message is the reason shown in the table."""


# ── sources ────────────────────────────────────────────────────────────────

_FAILED: dict = {}


def fetch(url: str) -> str:
    if url in _FAILED:  # fail once per URL, not once per fact
        raise Blocked(_FAILED[url])
    try:
        return _fetch(url)
    except Blocked as e:
        _FAILED[url] = str(e)
        raise


def _fetch(url: str) -> str:
    last: Exception | None = None
    for attempt in range(2):  # one retry: a sleeping host shouldn't block the stamp
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt == 0:
            time.sleep(3)
    raise Blocked(f"unreachable: {url} ({last})")


def normalise(text: str) -> str:
    """One spelling for spaces and the escapes data files and page payloads use."""
    for esc, ch in (("\\u00a0", " "), ("\\u00A0", " "), ("\\xa0", " "), ("\\xA0", " "),
                    ("\\u2019", "’"), ("\\u2192", "→"), ("\\'", "'"), ('\\"', '"')):
        text = text.replace(esc, ch)
    text = text.replace("\u00a0", " ").replace("\u202f", " ")  # no-break spaces
    return re.sub(r"\s+", " ", text)


class Sources:
    def __init__(self, pins: dict, local_repos: dict, portfolio: Path | None):
        self.pins = pins
        self.local_repos = local_repos
        self.portfolio = portfolio
        self._files: dict = {}
        self._pages: dict = {}

    def repo_json(self, repo: str, file: str):
        sha = self.pins.get(repo)
        if not sha:
            raise Blocked(f"no pinned commit for {repo}")
        key = (repo, file)
        if key not in self._files:
            if repo in self.local_repos:
                out = subprocess.run(["git", "-C", self.local_repos[repo], "show", f"{sha}:{file}"],
                                     capture_output=True, text=True)
                if out.returncode:
                    raise Blocked(f"{repo}@{sha[:7]}:{file} not found locally")
                raw = out.stdout
            else:
                raw = fetch(f"https://raw.githubusercontent.com/{repo}/{sha}/{file}")
            try:
                self._files[key] = json.loads(raw)
            except json.JSONDecodeError as e:
                raise Blocked(f"{file} is not JSON ({e})")
        return self._files[key]

    def page(self, url: str) -> str:
        if self.portfolio:
            url = "portfolio-source"
        if url not in self._pages:
            if self.portfolio:
                data = self.portfolio / "src" / "data"
                if not data.is_dir():
                    raise Blocked(f"{data} not found")
                parts = [p.read_text(encoding="utf-8") for p in sorted(data.rglob("*.js"))]
                self._pages[url] = normalise("\n".join(parts))
            else:
                raw = fetch(url)
                # tags out, script bodies kept: they carry the page's own data
                self._pages[url] = normalise(html.unescape(re.sub(r"<[^>]+>", " ", raw)))
        return self._pages[url]

    @property
    def matched_to(self) -> str:
        return "the portfolio’s source files" if self.portfolio else "allenvarghese.com"


# ── L1 recompute ───────────────────────────────────────────────────────────

def walk(obj, path: str) -> list:
    values = [obj]
    for part in path.split("."):
        nxt = []
        for v in values:
            if part == "*" and isinstance(v, dict):
                nxt.extend(v.values())
            elif part == "*" and isinstance(v, list):
                nxt.extend(v)
            elif isinstance(v, dict) and part in v:
                nxt.append(v[part])
            elif isinstance(v, list) and part.isdigit() and int(part) < len(v):
                nxt.append(v[int(part)])
            else:
                raise Blocked(f"path {path} missing at '{part}'")
        values = nxt
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise Blocked(f"path {path} is not a number")
    return values


def render(x: float, as_: str) -> str:
    if as_ == "percent":
        return f"{round(x * 100)}%"
    if as_ == "int":
        return f"{int(round(x)):,}"
    return str(x)


def recompute(spec: dict, src: Sources) -> str:
    op, repo, as_ = spec["op"], spec["repo"], spec.get("as", "")
    if op == "json_path":
        values = walk(src.repo_json(repo, spec["file"]), spec["path"])
        if len(values) != 1:
            raise Blocked(f"{spec['path']} matched {len(values)} values")
        return render(values[0], as_)
    if op == "sum":
        total = 0.0
        for term in spec["terms"]:
            total += sum(walk(src.repo_json(repo, term["file"]), term["path"]))
        return render(total, as_)
    raise Blocked(f"unknown op {op!r}")  # a closed set: nothing is ever eval'd


# ── L3 cite ────────────────────────────────────────────────────────────────

def blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def lint_readme(text: str, names: list[str]) -> tuple[dict, list]:
    """Returns ({fact id: [visible text, ...]}, [(line, stray figure), ...])."""
    t = re.sub(r"<!--.*?-->", blank, text, flags=re.S)
    t = re.sub(r"^```.*?^```", blank, t, flags=re.S | re.M)
    t = re.sub(r"`[^`\n]*`", blank, t)
    seen: dict = {}

    def take(m: re.Match) -> str:
        visible = re.sub(r"<[^>]+>|\*\*|__", "", m.group(1)).strip()
        seen.setdefault(m.group(3), []).append(visible)
        return " " * len(m.group(0))

    t = FACT_LINK.sub(take, t)
    t = re.sub(r"\]\([^)\n]*\)", "]", t)            # link targets
    t = re.sub(r"<[^>]*>", blank, t, flags=re.S)    # tags, with their attributes and alt text
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", " ", t)    # email addresses
    t = re.sub(r"&#?\w+;", " ", t)                  # entities such as &nbsp;
    for name in sorted(names, key=len, reverse=True):
        t = t.replace(name, " ")
    t = re.sub(r"\b(?:19|20)\d{2}\b", " ", t)       # years
    t = re.sub(r"[’']\d{2}\b", " ", t)              # class years: ’27
    stray = [(n, m.group(0)) for n, line in enumerate(t.splitlines(), 1)
             for m in re.finditer(r"\S*\d\S*", line)]
    return seen, stray


# ── stamp ──────────────────────────────────────────────────────────────────

THEMES = {
    "dark": dict(bg="#07080c", line="#232834", ink="#f3eee4", muted="#b8b2a6", subtle="#8c877f", ice="#9cc3ff"),
    "light": dict(bg="#ffffff", line="#e5e5ea", ink="#1d1d1f", muted="#424245", subtle="#68686d", ice="#2f62b8"),
}
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
SANS = "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


def x(s: str) -> str:
    return html.escape(str(s), quote=True)


def stamp_svg(theme: str, kinds: list[str], date: str, run: str, pin: str, digest: str, matched_to: str) -> str:
    c = THEMES[theme]
    n, r = len(kinds), kinds.count("recomputed")
    head = f"LEDGER · LAST CHECKED {date.upper()} · {run.upper()}"
    meta = f"telltale@{pin[:7]} · facts.json sha-256 {digest[:12]}"
    detail = f"· {r} recomputed from source data · {n - r} matched to {matched_to}"
    ticks = []
    for i, kind in enumerate(kinds):
        tx = 92 + i * 12
        delay = f' style="animation-delay:{i * 40}ms"'
        if kind == "recomputed":
            ticks.append(f'<rect class="tk"{delay} x="{tx}" y="98" width="6" height="16" rx="1" fill="{c["ice"]}"/>')
        else:
            ticks.append(f'<rect class="tk"{delay} x="{tx + 0.75}" y="98.75" width="4.5" height="14.5" rx="1" '
                         f'fill="none" stroke="{c["ice"]}" stroke-width="1.5"/>')
    lx = 92 + n * 12 + 18
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 136" width="1200" height="136" role="img" aria-labelledby="title desc">
  <title id="title">{x(f"Ledger: {n} of {n} figures check out, last checked {date}")}</title>
  <desc id="desc">{x(f"{r} figures recomputed from Telltale's committed benchmark data at commit {pin[:7]}; {n - r} matched to {matched_to}. facts.json SHA-256 {digest}. Generated by scripts/ledger.py; renewed only when every check passes.")}</desc>
  <style>
    .mono {{ font-family: {MONO}; }}
    .sans {{ font-family: {SANS}; }}
    .tk {{ animation: print .3s ease-out backwards; }}
    @keyframes print {{ from {{ opacity: .25; }} to {{ opacity: 1; }} }}
    @media (prefers-reduced-motion: reduce) {{ .tk {{ animation: none; }} }}
  </style>
  <rect x="0.5" y="0.5" width="1199" height="135" rx="14" fill="{c["bg"]}" stroke="{c["line"]}"/>
  <circle cx="50" cy="68" r="18" fill="none" stroke="{c["ice"]}" stroke-width="2"/>
  <path d="M41 68 l6 6 l12 -13" fill="none" stroke="{c["ice"]}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
  <text class="mono" x="92" y="44" font-size="13" letter-spacing="2" fill="{c["subtle"]}">{x(head)}</text>
  <text class="mono" x="1160" y="44" font-size="13" fill="{c["subtle"]}" text-anchor="end">{x(meta)}</text>
  <text class="sans" x="92" y="82" font-size="24" font-weight="600" fill="{c["ink"]}">{x(f"{n} of {n} figures check out")}<tspan dx="10" font-size="19" font-weight="400" fill="{c["muted"]}">{x(detail)}</tspan></text>
  {chr(10).join("  " + t if i else t for i, t in enumerate(ticks))}
  <rect x="{lx}" y="102" width="8" height="8" rx="1" fill="{c["ice"]}"/>
  <text class="mono" x="{lx + 14}" y="110" font-size="12" fill="{c["subtle"]}">recomputed</text>
  <rect x="{lx + 112.75}" y="102.75" width="6.5" height="6.5" rx="1" fill="none" stroke="{c["ice"]}" stroke-width="1.5"/>
  <text class="mono" x="{lx + 126}" y="110" font-size="12" fill="{c["subtle"]}">matched to the case study</text>
</svg>
"""


# ── main ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stamp", action="store_true", help="rewrite the ledger SVGs after a full pass")
    ap.add_argument("--portfolio", type=Path, help="match proofs against a local portfolio checkout")
    ap.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME=DIR",
                    help="read a pinned repo from a local clone")
    args = ap.parse_args()

    raw_facts = FACTS.read_bytes()
    doc = json.loads(raw_facts)
    facts = doc["facts"]
    local = dict(spec.split("=", 1) for spec in args.repo)
    src = Sources(doc["pins"], local, args.portfolio)
    readme = README.read_text(encoding="utf-8")
    seen, stray = lint_readme(readme, doc.get("names", []))

    rows, failed, facts_failed = [], 0, 0
    for f in facts:
        reasons = []
        try:
            if f["kind"] == "recomputed":
                got = recompute(f["recompute"], src)
                if got != f["display"]:
                    reasons.append(f"L1 recomputed {got}")
            text = src.page(f.get("source", doc["site"]))
            for phrase in f.get("proof", []):
                if normalise(phrase) not in text:
                    reasons.append(f"L2 not on {src.matched_to}: {phrase!r}")
        except Blocked as e:
            reasons.append(str(e))
        shown = seen.pop(f["id"], [])
        if not shown:
            reasons.append("L3 not linked in README")
        for v in shown:
            if v != f["display"]:
                reasons.append(f"L3 README shows {v!r}")
        failed += bool(reasons)
        facts_failed += bool(reasons)
        rows.append((f["id"], f["display"], f["kind"], "BLOCKED" if reasons else "PASS", "; ".join(reasons)))
    for fid, shown in seen.items():
        failed += 1
        rows.append((fid, ",".join(shown), "?", "BLOCKED", "L3 linked in README but not in facts.json"))
    for line, token in stray:
        failed += 1
        rows.append((f"README:{line}", token, "-", "BLOCKED", "L3 figure with no fact behind it"))

    width = max(len(r[0]) for r in rows)
    for r in rows:
        print(f"{r[0]:<{width}}  {r[1]:>8}  {r[2]:<10}  {r[3]:<7}  {r[4]}")
    print(f"\n{len(facts) - facts_failed} of {len(facts)} facts pass; {failed - facts_failed} other blocks.")
    if failed:
        print("Nothing stamped.")
        return 1

    if args.stamp:
        now = dt.datetime.now(dt.timezone.utc)
        date = f"{now.day} {now:%b %Y}"
        run = f"run {os.environ['RUN_NUMBER']}" if os.environ.get("RUN_NUMBER") else "local run"
        digest = hashlib.sha256(raw_facts).hexdigest()
        pin = doc["pins"].get("allenvarghese05/telltale", "")
        kinds = [f["kind"] for f in facts]
        for theme in THEMES:
            out = ASSETS / f"ledger-{theme}.svg"
            out.write_text(stamp_svg(theme, kinds, date, run, pin, digest, src.matched_to), encoding="utf-8")
            print(f"stamped {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
