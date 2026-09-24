from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from storage import RunStore


def test_run_store_survives_new_instance(tmp_path):
    path = tmp_path / "runs.sqlite3"
    run = {"id": "r1", "status": "queued", "created_at": 1.0,
           "mode": "url", "url": "https://example.test", "flows": []}
    RunStore(path).create(run)
    RunStore(path).update("r1", status="complete", browser_audit={"http_status": 200})
    loaded = RunStore(path).get("r1")
    assert loaded["status"] == "complete"
    assert loaded["browser_audit"]["http_status"] == 200
