"""The pair review screen.

The operator's whole job in this system is one decision per pair: are these two
contracts the same fact? This serves that decision and nothing else - a
side-by-side diff of the two venues' terms, links to both source documents, and
two buttons.

Deliberately *not* offered: an override. The rule layer's verdict is final, so a
pair it rejected has no approve button at all. A screen with "approve anyway" is
where the discipline of a deterministic gate quietly dies, and the spec is
explicit that severity judgements must not be made under time pressure.

Stdlib `http.server`, matching the dashboards already in `scripts/`. It binds
to localhost by default: this thing approves what trades, and it has no
authentication.
"""

from __future__ import annotations

import argparse
import html
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from arb.registry import PairStatus, approve, reject, verify_candidate
from arb.review import PairReview, TermRow, TermStatus, review_pair, review_queue
from arb.shell.candidates import CandidateStore

__all__ = ["build_handler", "build_parser", "main", "render_page"]

DEFAULT_DB = Path("data/live_orderbooks/pair_candidates.sqlite")

#: Statuses an operator may still act on. Anything else has been settled, by
#: the rules or by a person, and a second decision would overwrite the first.
_DECIDABLE = (PairStatus.PROPOSED, PairStatus.AWAITING_APPROVAL)

_STATUS_LABEL = {
    TermStatus.AGREES: "match",
    TermStatus.DIFFERS: "differs",
    TermStatus.UNSTATED: "not stated",
}


def render_page(views: list[PairReview], operator: str) -> str:
    """The whole screen. One page - the queue and every diff on it."""
    pending = [view for view in views if view.awaiting_decision]
    body = "\n".join(_render_card(view, operator) for view in views)
    return _SHELL.format(
        operator=html.escape(operator),
        pending=len(pending),
        total=len(views),
        body=body or _EMPTY,
    )


def _render_card(view: PairReview, operator: str) -> str:
    rows = "\n".join(_render_row(row) for row in view.rows)
    verdict_class = "ok" if view.verified else "bad"
    verdict_text = (
        "Rules verified - both venues state the same terms"
        if view.verified
        else "Rules rejected - "
        + html.escape(", ".join(f.value.replace("_", " ") for f in view.failures))
    )

    if view.can_approve:
        controls = _CONTROLS.format(pair_id=html.escape(view.pair_id))
    elif view.blocked_by_rules:
        controls = _BLOCKED
    else:
        controls = _DECIDED.format(
            status=html.escape(view.status.value.replace("_", " ")),
            operator=html.escape(view.operator_line()),
        )

    return _CARD.format(
        pair_id=html.escape(view.pair_id),
        kalshi_resolution=_resolution_html(view.kalshi_resolution),
        polymarket_resolution=_resolution_html(view.polymarket_resolution),
        category=html.escape(view.category),
        settlement_date=html.escape(view.settlement_date),
        confidence=html.escape(view.model_confidence),
        kalshi_ticker=html.escape(view.kalshi_ticker),
        polymarket_id=html.escape(view.polymarket_id),
        kalshi_question=html.escape(view.kalshi_question),
        polymarket_question=html.escape(view.polymarket_question),
        kalshi_url=html.escape(view.kalshi_url, quote=True),
        polymarket_url=html.escape(view.polymarket_url, quote=True),
        verdict_class=verdict_class,
        verdict_text=verdict_text,
        rows=rows,
        controls=controls,
    )


def _resolution_html(text: str) -> str:
    """The venue's resolution language, or an explicit placeholder.

    Silence has to look like silence: an empty panel reads as a rendering bug,
    and a reviewer who assumes the text was checked when it was never captured
    is approving on less evidence than they think.
    """
    if not text.strip():
        return '<p class="res-missing">No resolution text captured for this venue.</p>'
    return f'<p class="res-text">{html.escape(text)}</p>'


def _render_row(row: TermRow) -> str:
    return _ROW.format(
        status=row.status.value,
        badge=_STATUS_LABEL[row.status],
        label=html.escape(row.label),
        kalshi=html.escape(row.kalshi or "-- not stated --"),
        polymarket=html.escape(row.polymarket or "-- not stated --"),
    )


def build_handler(
    store: CandidateStore, operator: str, clock: Callable[[], int]
) -> type[BaseHTTPRequestHandler]:
    """The handler, with its collaborators supplied rather than imported.

    `clock` is passed in for the same reason the reducer takes time as an event:
    a decision timestamp that a test cannot control is a decision a test cannot
    assert on.
    """

    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "arb-review/1.0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if urlparse(self.path).path not in ("/", "/index.html"):
                self._send(HTTPStatus.NOT_FOUND, "not found", "text/plain")
                return
            page = render_page(list(review_queue(store.all())), operator)
            self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            path = urlparse(self.path).path
            if path not in ("/approve", "/reject"):
                self._send(HTTPStatus.NOT_FOUND, "not found", "text/plain")
                return

            form = self._read_form()
            pair_id = form.get("pair_id", [""])[0]
            candidate = store.get(pair_id)
            if candidate is None:
                self._send(HTTPStatus.NOT_FOUND, "no such pair", "text/plain")
                return

            # An already-decided pair is refused outright. Re-verifying one
            # would reset its status to awaiting-approval and let a stale
            # browser tab silently overwrite a decision that was already made.
            if candidate.status not in _DECIDABLE:
                self._send(
                    HTTPStatus.CONFLICT,
                    f"{pair_id} is already {candidate.status.value}",
                    "text/plain",
                )
                return

            # Re-verified on submit, not trusted from the rendered page. The
            # terms could have been re-fetched since the screen was drawn, and
            # a stale browser tab must not be able to approve a pair the rules
            # would now reject.
            candidate = verify_candidate(candidate)
            now = clock()

            try:
                if path == "/approve":
                    store.save(approve(candidate, operator=operator, at_ms=now))
                else:
                    store.save(
                        reject(
                            candidate,
                            operator=operator,
                            at_ms=now,
                            note=form.get("note", ["not the same pair"])[0],
                        )
                    )
            except ValueError as refused:
                self._send(HTTPStatus.CONFLICT, str(refused), "text/plain")
                return

            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()

        def _read_form(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return parse_qs(raw)

        def _send(self, status: HTTPStatus, body: str, content_type: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            """Quiet by default - this runs beside a collector, not alone."""

    return ReviewHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review and approve Matched Pairs.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        # A managed launcher assigns a free port through the PORT env var;
        # honoring it is what stops an orphaned instance from wedging every
        # later start. An explicit --port still wins for manual runs.
        default=int(os.environ.get("PORT", "8771")),
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--operator",
        required=True,
        help="Recorded against every decision - the calibration dataset needs "
        "to know who decided.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    import time

    store = CandidateStore(args.db)
    handler = build_handler(store, args.operator, lambda: int(time.time() * 1000))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"pair review on http://{args.host}:{args.port}  (db: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair review</title>
<style>
:root {{
  color-scheme: light;
  --ground:#f7f8f8; --panel:#ffffff; --sunk:#eef1f1; --rule:#d5dbdb; --soft:#e4e9e9;
  --ink:#12201f; --dim:#4a5b5a; --faint:#748584;
  --ok:#2f6b3f; --bad:#963b3b; --warn:#8a6410;
  --accent:#17605c; --accent-soft:#dceceb;
  --kalshi:#17605c; --polymarket:#9a5b33;
  --shadow:0 1px 2px rgba(18,32,31,.05), 0 10px 28px -18px rgba(18,32,31,.35);
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --ground:#0d1615; --panel:#141f1e; --sunk:#101a19; --rule:#263736; --soft:#1d2c2b;
    --ink:#e2eae9; --dim:#a3b5b3; --faint:#708381;
    --ok:#6fbe84; --bad:#e08585; --warn:#d9ab54;
    --accent:#5fbdb5; --accent-soft:#17302e;
    --kalshi:#5fbdb5; --polymarket:#cf9366;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 28px -18px rgba(0,0,0,.8);
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--ground); color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1060px; margin:0 auto; padding:2.2rem 1.25rem 5rem; }}
header {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.75rem 1.5rem;
  border-bottom:1px solid var(--rule); padding-bottom:1.3rem; margin-bottom:2rem; }}
h1 {{ font-family:ui-serif,Georgia,"Iowan Old Style",serif; font-size:2rem;
  margin:0; font-weight:600; letter-spacing:-.02em; }}
.chips {{ display:flex; flex-wrap:wrap; gap:.45rem; margin-left:auto; }}
.chip {{ font-size:.74rem; font-weight:650; letter-spacing:.04em; padding:.22rem .65rem;
  border-radius:999px; border:1px solid var(--rule); color:var(--dim);
  background:var(--panel); font-variant-numeric:tabular-nums; }}
.chip.awaiting {{ color:var(--accent); border-color:var(--accent);
  background:var(--accent-soft); }}
.operator {{ width:100%; color:var(--faint); font-size:.85rem; }}
.card {{ background:var(--panel); border:1px solid var(--rule); border-radius:6px;
  margin-bottom:2rem; overflow:hidden; box-shadow:var(--shadow); }}
.card > .head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.3rem 1rem;
  padding:1.05rem 1.3rem .95rem; border-bottom:1px solid var(--soft); }}
.pair-id {{ font-family:ui-monospace,Menlo,monospace; font-size:1.05rem; font-weight:600; }}
.meta {{ color:var(--faint); font-size:.82rem; }}
.questions {{ display:grid; gap:1rem; padding:1.05rem 1.3rem;
  border-bottom:1px solid var(--soft); }}
@media (min-width:760px) {{ .questions {{ grid-template-columns:1fr 1fr; gap:1.6rem; }} }}
.venue {{ border-top:3px solid var(--rule); padding-top:.55rem; }}
.venue.k {{ border-top-color:var(--kalshi); }}
.venue.p {{ border-top-color:var(--polymarket); }}
.venue h3 {{ font-size:.68rem; letter-spacing:.13em; text-transform:uppercase;
  margin:0 0 .3rem; font-weight:700; }}
.venue.k h3 {{ color:var(--kalshi); }}
.venue.p h3 {{ color:var(--polymarket); }}
.venue .q {{ margin:0 0 .35rem; font-weight:600; }}
.venue a {{ color:var(--accent); font-size:.83rem;
  font-family:ui-monospace,Menlo,monospace; }}
.verdict {{ padding:.7rem 1.3rem; font-size:.88rem; font-weight:650;
  border-bottom:1px solid var(--soft); }}
.verdict.ok {{ color:var(--ok); }}
.verdict.bad {{ color:var(--bad); }}
.section-label {{ font-size:.66rem; letter-spacing:.13em; text-transform:uppercase;
  color:var(--faint); font-weight:700; padding:.9rem 1.3rem .35rem; }}
.tablewrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:.88rem; min-width:640px; }}
th,td {{ text-align:left; padding:.5rem 1.3rem .5rem 0; vertical-align:top;
  border-bottom:1px solid var(--soft); }}
th:first-child, td:first-child {{ padding-left:1.3rem; }}
th {{ font-size:.66rem; letter-spacing:.1em; text-transform:uppercase;
  color:var(--faint); font-weight:600; }}
td.term {{ font-weight:600; white-space:nowrap; }}
tr.differs td {{ background:color-mix(in srgb, var(--bad) 9%, transparent); }}
tr.unstated td {{ background:color-mix(in srgb, var(--warn) 11%, transparent); }}
.badge {{ font-size:.64rem; letter-spacing:.06em; text-transform:uppercase;
  font-weight:700; white-space:nowrap; }}
tr.agrees .badge {{ color:var(--ok); }}
tr.differs .badge {{ color:var(--bad); }}
tr.unstated .badge {{ color:var(--warn); }}
.resolution {{ display:grid; gap:1rem; padding:.4rem 1.3rem 1.1rem;
  border-bottom:1px solid var(--soft); }}
@media (min-width:760px) {{ .resolution {{ grid-template-columns:1fr 1fr; gap:1.6rem; }} }}
.res-col h4 {{ font-size:.66rem; letter-spacing:.13em; text-transform:uppercase;
  margin:.4rem 0 .35rem; font-weight:700; }}
.res-col.k h4 {{ color:var(--kalshi); }}
.res-col.p h4 {{ color:var(--polymarket); }}
.res-text {{ margin:0; background:var(--sunk); border:1px solid var(--soft);
  border-radius:4px; padding:.7rem .85rem; font-size:.86rem; line-height:1.55;
  white-space:pre-wrap; max-height:14rem; overflow-y:auto; }}
.res-missing {{ margin:0; color:var(--warn); font-size:.85rem; font-style:italic;
  border:1px dashed var(--rule); border-radius:4px; padding:.7rem .85rem; }}
.controls {{ padding:1rem 1.3rem; display:flex; gap:.65rem; align-items:center;
  flex-wrap:wrap; }}
button {{ font:inherit; font-weight:650; font-size:.88rem; padding:.55rem 1.15rem;
  border-radius:4px; border:1px solid var(--rule); cursor:pointer;
  background:var(--panel); color:var(--ink); }}
button.approve {{ background:var(--ok); border-color:var(--ok); color:#ffffff; }}
button.reject {{ border-color:var(--bad); color:var(--bad); background:transparent; }}
button:hover {{ filter:brightness(1.08); }}
.hint {{ color:var(--faint); font-size:.84rem; margin:0; }}
.hint.blocked {{ color:var(--bad); }}
input[type=text] {{ font:inherit; font-size:.88rem; padding:.5rem .65rem;
  flex:1 1 15rem; border:1px solid var(--rule); border-radius:4px;
  background:var(--ground); color:var(--ink); }}
form {{ display:contents; }}
:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; }} }}
</style></head><body><div class="wrap">
<header>
  <h1>Pair review</h1>
  <div class="chips">
    <span class="chip awaiting">{pending} awaiting</span>
    <span class="chip">{total} total</span>
  </div>
  <p class="operator">Deciding as <strong>{operator}</strong>. An approved pair
  trades automatically; a pair the rules rejected cannot be approved here.</p>
</header>
{body}
</div></body></html>
"""

_EMPTY = """<p class="hint">No candidates yet. Propose some with
<code>arb.registry.propose</code> and save them to the candidate store.</p>"""

_CARD = """<article class="card">
  <div class="head">
    <span class="pair-id">{pair_id}</span>
    <span class="meta">{category} &middot; settles {settlement_date} &middot;
      model confidence {confidence}</span>
  </div>
  <div class="questions">
    <div class="venue k">
      <h3>Kalshi &middot; {kalshi_ticker}</h3>
      <p class="q">{kalshi_question}</p>
      <a href="{kalshi_url}" rel="noreferrer noopener" target="_blank">contract terms &#8599;</a>
    </div>
    <div class="venue p">
      <h3>Polymarket &middot; {polymarket_id}</h3>
      <p class="q">{polymarket_question}</p>
      <a href="{polymarket_url}" rel="noreferrer noopener" target="_blank">resolution rules &#8599;</a>
    </div>
  </div>
  <div class="verdict {verdict_class}">{verdict_text}</div>
  <div class="section-label">Terms, machine-compared</div>
  <div class="tablewrap"><table>
    <thead><tr><th>Term</th><th>Kalshi</th><th>Polymarket</th><th></th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <div class="section-label">Resolution, verbatim</div>
  <div class="resolution">
    <div class="res-col k"><h4>Kalshi says</h4>{kalshi_resolution}</div>
    <div class="res-col p"><h4>Polymarket says</h4>{polymarket_resolution}</div>
  </div>
  {controls}
</article>"""

_ROW = """<tr class="{status}">
  <td class="term">{label}</td><td>{kalshi}</td><td>{polymarket}</td>
  <td><span class="badge">{badge}</span></td></tr>"""

_CONTROLS = """<div class="controls">
  <form method="post" action="/approve">
    <input type="hidden" name="pair_id" value="{pair_id}">
    <button type="submit" class="approve">Same pair &mdash; approve</button>
  </form>
  <form method="post" action="/reject">
    <input type="hidden" name="pair_id" value="{pair_id}">
    <input type="text" name="note" placeholder="Why are these not the same pair?">
    <button type="submit" class="reject">Not the same pair</button>
  </form>
</div>"""

_BLOCKED = """<div class="controls">
  <p class="hint blocked">The rule layer rejected this pair, so it cannot be
    approved here and there is nothing to decide. Fix the terms at source and
    re-propose.</p>
</div>"""

_DECIDED = """<div class="controls">
  <p class="hint">Decided: <strong>{status}</strong>{operator}</p>
</div>"""


if __name__ == "__main__":  # pragma: no cover - entry point
    main()
