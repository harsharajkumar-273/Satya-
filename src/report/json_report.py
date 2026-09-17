"""Versioned JSON findings for CI consumers; omits screenshots and raw network bodies."""
import json
from dataclasses import asdict
from pathlib import Path
from agent.loop import summarize


def render_json_report(results, out_path):
    payload = {
        "schema_version": 1,
        "summary": summarize(results),
        "flows": [{"flow": r.flow, "claim_evidence": r.claim_evidence,
                   "findings": [asdict(f) for f in r.findings]} for r in results],
    }
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
