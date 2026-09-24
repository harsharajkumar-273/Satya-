"""
Self-contained HTML report for a Veritas run.

Renders one section per flow: the verdict(s), the claim Veritas inferred, the
network calls it saw, and -- the part a plain pytest log can't give you -- the
actual before/after screenshots side by side, since this is fundamentally a
UI-testing tool and its own output had never shown a UI before this.

Pure function of FlowResult data: no Playwright/browser dependency, so it's
unit-testable with hand-built FlowResult fixtures (see tests/test_report.py).
"""
from __future__ import annotations

import base64
import html
from datetime import datetime, timezone
from pathlib import Path

from agent.loop import summarize
from models import FlowResult, Verdict

_VERDICT_STYLE = {
    Verdict.AGREE: ("#1a7f37", "#dafbe1"),
    Verdict.NO_CLAIM: ("#57606a", "#eaeef2"),
    Verdict.UI_LIED: ("#cf222e", "#ffebe9"),
    Verdict.NO_REQUEST: ("#cf222e", "#ffebe9"),
    Verdict.BACKEND_ERROR: ("#cf222e", "#ffebe9"),
    Verdict.DATA_LEAK: ("#9a6700", "#fff8c5"),
    Verdict.UNSTABLE_RENDER: ("#cf222e", "#ffebe9"),
    Verdict.ACTION_FAILED: ("#57606a", "#eaeef2"),
}

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f6f8fa; color: #1f2328; margin: 0; padding: 32px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: #57606a; font-size: 13px; margin-bottom: 24px; }
.summary { display: flex; gap: 12px; margin-bottom: 28px; flex-wrap: wrap; }
.stat { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
        padding: 10px 16px; min-width: 120px; }
.stat .n { font-size: 22px; font-weight: 600; display: block; }
.stat .l { font-size: 12px; color: #57606a; }
.flow { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
        padding: 18px 20px; margin-bottom: 16px; }
.flow h2 { font-size: 15px; margin: 0 0 10px; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 12px;
         font-size: 12px; font-weight: 600; margin: 2px 6px 2px 0; }
.shots { display: flex; gap: 14px; margin: 12px 0; }
.shots figure { margin: 0; flex: 1; min-width: 0; }
.shots img { max-width: 100%; border: 1px solid #d0d7de; border-radius: 6px; display: block; }
.shots figcaption { font-size: 11px; color: #57606a; margin-top: 4px; text-align: center; }
.no-shot { border: 1px dashed #d0d7de; border-radius: 6px; padding: 24px;
           text-align: center; color: #8b949e; font-size: 12px; }
.evidence { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px;
            background: #f6f8fa; border-radius: 6px; padding: 8px 10px;
            white-space: pre-wrap; word-break: break-word; margin: 4px 0; }
table.calls { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
table.calls th, table.calls td { text-align: left; padding: 4px 8px;
            border-bottom: 1px solid #eaeef2; }
.detail { font-size: 13px; margin: 4px 0 10px; color: #1f2328; }
"""


def _b64_img(png_bytes: bytes) -> str | None:
    if not png_bytes:
        return None
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _badge(verdict: Verdict) -> str:
    fg, bg = _VERDICT_STYLE.get(verdict, ("#57606a", "#eaeef2"))
    return f'<span class="badge" style="color:{fg};background:{bg}">{html.escape(verdict.value)}</span>'


def _flow_section(result: FlowResult) -> str:
    trace = result.trace
    before_uri = _b64_img(trace.ui_before_png)
    after_uri = _b64_img(trace.ui_after_png)

    def _shot(uri: str | None, label: str) -> str:
        if uri:
            return f'<figure><img src="{uri}" alt="{label}"><figcaption>{label}</figcaption></figure>'
        return f'<figure><div class="no-shot">no {label.lower()} screenshot captured</div><figcaption>{label}</figcaption></figure>'

    shots = f'<div class="shots">{_shot(before_uri, "Before")}{_shot(after_uri, "After")}</div>'

    calls_rows = "".join(
        f"<tr><td>{html.escape(c.method)}</td><td>{html.escape(c.url)}</td>"
        f"<td>{c.status if c.status is not None else '—'}</td></tr>"
        for c in trace.network_calls
    )
    calls_table = (
        f'<table class="calls"><thead><tr><th>Method</th><th>URL</th><th>Status</th></tr></thead>'
        f"<tbody>{calls_rows}</tbody></table>"
        if trace.network_calls
        else '<div class="detail">(no network calls captured)</div>'
    )

    findings_html = "".join(
        f'<div>{_badge(f.verdict)}<span class="detail">{html.escape(f.summary)}</span>'
        f'<div class="evidence">{html.escape(f.detail)}</div></div>'
        for f in result.findings
    )

    return f"""
<section class="flow">
  <h2>{html.escape(result.flow)}</h2>
  {findings_html}
  <div class="evidence">claim evidence: {html.escape(result.claim_evidence)}</div>
  {shots}
  {calls_table}
</section>"""


def render_html_report(results: list[FlowResult], out_path: str | Path, *, title: str = "Veritas Report") -> Path:
    """Render `results` as a self-contained HTML file at out_path. Returns the Path written."""
    summary = summarize(results)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    stats = "".join(
        f'<div class="stat"><span class="n">{value}</span><span class="l">{label}</span></div>'
        for value, label in (
            (summary["flows_checked"], "flows checked"),
            (summary["problems_found"], "problems found"),
            (summary["inconclusive_found"], "inconclusive"),
            (summary["findings_total"], "total findings"),
        )
    )

    body = "".join(_flow_section(r) for r in results)

    out = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="meta">Generated {generated_at}</div>
<div class="summary">{stats}</div>
{body}
</body>
</html>"""

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    return out_path
