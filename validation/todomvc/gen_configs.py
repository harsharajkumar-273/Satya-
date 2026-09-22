"""
Generate the Satya flow configs used for the TodoMVC validation in docs/VALIDATION.md.

    python validation/todomvc/gen_configs.py vanillajs            # -> vanillajs.yaml
    python validation/todomvc/gen_configs.py componentjs .todo__new

Assumes the TodoMVC examples are served at http://127.0.0.1:8090/examples/<impl>/
(see docs/VALIDATION.md for exact setup). Every config uses reload ground truth:
Satya never reads these apps' storage directly -- it only acts on the page and
reloads it, exactly as it would on a site it has no backend access to.
"""
import sys
from pathlib import Path

import yaml

impl = sys.argv[1]
new = sys.argv[2] if len(sys.argv) > 2 else "#new-todo"
row = "li[data-id]"
cfg = {
    "base_url": "http://127.0.0.1:8090",
    "entry_path": f"/examples/{impl}/index.html",
    "ground_truth": "reload",
    "headless": True,
    "api_filters": ["/api/"],
    "selectors": {"row_selector": row, "toast_selector": "#toast", "id_attr": "data-id"},
    "flows": [
        {"description": "add todo 'Buy milk'", "actions": [
            {"fill": {"selector": new, "value": "Buy milk"}},
            {"press": {"selector": new, "key": "Enter"}}]},
        {"description": "add todo 'Walk dog'", "actions": [
            {"fill": {"selector": new, "value": "Walk dog"}},
            {"press": {"selector": new, "key": "Enter"}}]},
        {"description": "rename first todo to 'Buy oat milk'", "actions": [
            {"dblclick": f"{row} >> nth=0 >> label"},
            {"fill": {"selector": f"{row} >> nth=0 >> input.edit", "value": "Buy oat milk"}},
            {"press": {"selector": f"{row} >> nth=0 >> input.edit", "key": "Enter"}}]},
        {"description": "mark first todo complete", "actions": [
            {"check": f"{row} >> nth=0 >> input.toggle"}]},
        {"description": "delete last todo", "actions": [
            {"hover": f"{row} >> nth=-1"},
            {"click": f"{row} >> nth=-1 >> .destroy"}]},
    ],
}
out = Path(__file__).with_name(f"{impl}.yaml")
yaml.safe_dump(cfg, open(out, "w"), sort_keys=False)
print(f"wrote {out}")
