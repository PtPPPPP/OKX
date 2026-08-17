from __future__ import annotations

import json
from pathlib import Path

from scripts.sanitize_audit_materials import sanitize_file


def test_json_sanitization_preserves_structure_and_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    original = {
        "result": "PASS",
        "accountId": "demo-account",
        "clientOrderId": "client-123",
        "nested": {"requestHeaders": {"OK-ACCESS-KEY": "secret"}},
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    assert sanitize_file(path, write=True) == 3
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["result"] == "PASS"
    assert first["accountId"].startswith("<redacted:")
    assert first["nested"]["requestHeaders"].startswith("<redacted:")

    assert sanitize_file(path, write=True) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == first


def test_markdown_sanitization_removes_linkable_ids_and_ip(tmp_path: Path) -> None:
    path = tmp_path / "audit.md"
    path.write_text(
        "result=PASS\nrun_id=0123456789abcdef0123456789abcdef\negress IP fingerprint=203.0.113.8\n",
        encoding="utf-8",
    )

    assert sanitize_file(path, write=True) == 2
    output = path.read_text(encoding="utf-8")
    assert "result=PASS" in output
    assert "0123456789abcdef0123456789abcdef" not in output
    assert "203.0.113.8" not in output
