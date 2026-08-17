"""Deterministically redact account-linkable values from local audit evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "account_id",
        "accountid",
        "apikey",
        "api_key",
        "api_key_fingerprint",
        "api_key_fingerprint_sha256_prefix",
        "apikeyfingerprint",
        "client_order_id",
        "clientorderid",
        "clordid",
        "egress_ip",
        "egress_ip_fingerprint",
        "egressipfingerprint",
        "ip",
        "ordid",
        "order_id",
        "orderid",
        "passphrase",
        "proposal_id",
        "request_headers",
        "requestheaders",
        "run_id",
        "session_token",
        "sessiontoken",
        "signal_id",
        "subacct",
        "uid",
    }
)
IDENTIFIER_PATTERN = re.compile(r"(?<![0-9a-f])(?P<value>[0-9a-f]{32})(?![0-9a-f])", re.I)
IP_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
LABEL_PATTERN = re.compile(
    r"(?P<label>(?:ordId|order_id|client_order_id|clOrdId|run_id|proposal_id|"
    r"api[_ ]?key fingerprint|egress IP fingerprint)\s*[=:]\s*)"
    r"(?P<value>[^\s,;`\]\}\)]+)",
    re.I,
)


def redact_value(value: object) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"<redacted:{digest}>"


def is_redacted(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"<redacted:[0-9a-f]{12}>", value) is not None


def sanitize_json(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            if (
                key.lower() in SENSITIVE_KEYS
                and item not in (None, "", [], {})
                and not is_redacted(item)
            ):
                result[key] = redact_value(item)
                count += 1
            else:
                result[key], nested = sanitize_json(item)
                count += nested
        return result, count
    if isinstance(value, list):
        clean_items: list[Any] = []
        count = 0
        for item in value:
            clean, nested = sanitize_json(item)
            clean_items.append(clean)
            count += nested
        return clean_items, count
    return value, 0


def sanitize_text(content: str) -> tuple[str, int]:
    count = 0

    def replace_identifier(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return redact_value(match.group("value"))

    def replace_ip(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return redact_value(match.group(0))

    def replace_label(match: re.Match[str]) -> str:
        nonlocal count
        if is_redacted(match.group("value")):
            return match.group(0)
        count += 1
        return match.group("label") + redact_value(match.group("value"))

    content = LABEL_PATTERN.sub(replace_label, content)
    content = IDENTIFIER_PATTERN.sub(replace_identifier, content)
    return IP_PATTERN.sub(replace_ip, content), count


def sanitize_file(path: Path, *, write: bool) -> int:
    if path.suffix.lower() == ".json":
        loaded = json.loads(path.read_text(encoding="utf-8"))
        clean, count = sanitize_json(loaded)
        content = json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        content, count = sanitize_text(path.read_text(encoding="utf-8"))
    if write and count:
        path.write_text(content, encoding="utf-8")
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    files = sorted(
        {
            file
            for path in args.paths
            for file in (path.rglob("*") if path.is_dir() else (path,))
            if file.is_file() and file.suffix.lower() in {".json", ".md"}
        }
    )
    counts = {str(path): sanitize_file(path, write=args.write) for path in files}
    changed = {path: count for path, count in counts.items() if count}
    print(
        json.dumps(
            {
                "mode": "write" if args.write else "dry_run",
                "files_scanned": len(files),
                "files_changed": len(changed),
                "redactions": sum(changed.values()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
