#!/usr/bin/env python3
"""Build locator bridges for real EPUBs and verify exact quote round-trips."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from locator_bridge import (  # noqa: E402
    _normalized_span_to_legacy,
    _text_across_blocks,
    _unique_match,
    build_bridge_payload,
    normalize_text_v1,
)
from epub_parser import parse_uploaded_book_path  # noqa: E402


def validate(path: Path) -> dict:
    started = time.perf_counter()
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    parsed = parse_uploaded_book_path(
        path.name, path, source_sha256=digest, include_assets=False
    )
    chapters = [
        {
            "id": uuid5(NAMESPACE_URL, f"{digest}:{item.chapter_index}"),
            "chapter_index": item.chapter_index,
            "href": item.href,
            "content_html": item.content_html,
        }
        for item in parsed.chapters
    ]
    payload = build_bridge_payload(
        path,
        book_id=uuid5(NAMESPACE_URL, digest),
        source_sha256=digest,
        chapters=chapters,
    )
    attempted = mapped = ambiguous = original_unique = 0
    failures = []
    for chapter in payload["chapters"]:
        for block in chapter["blocks"]:
            quote = normalize_text_v1(block["text"])
            if len(quote) < 4:
                continue
            quote = normalize_text_v1(quote[: min(24, len(quote))])
            start_hint = int(block["canonical_start"])
            before = normalize_text_v1(chapter["normalized_text"][max(0, start_hint - 120):start_hint])
            after = normalize_text_v1(chapter["normalized_text"][start_hint + len(quote):start_hint + len(quote) + 120])
            attempted += 1
            normalized_match = _unique_match(chapter["normalized_text"], quote, before, after)
            if normalized_match is None:
                ambiguous += 1
                failures.append({"href": chapter["href"], "block_id": block["block_id"], "reason": "ambiguous", "quote": quote})
                continue
            anchor = _normalized_span_to_legacy(chapter, *normalized_match)
            actual = _text_across_blocks(
                [{"block_id": item["block_id"], "text": item["text"]} for item in chapter["blocks"]],
                anchor["start_block_id"], anchor["start_offset"],
                anchor["end_block_id"], anchor["end_offset"],
            )
            if normalize_text_v1(actual) == quote:
                mapped += 1
            else:
                failures.append({"href": chapter["href"], "block_id": block["block_id"], "reason": "roundtrip", "quote": quote, "actual": normalize_text_v1(actual), "anchor": anchor})
            if _unique_match(chapter["original_text"], quote, "", "") is not None:
                original_unique += 1
            if attempted >= 200:
                break
        if attempted >= 200:
            break
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "epub_version": parsed.epub_version,
        "chapters": len(parsed.chapters),
        "bridge_chapters": len(payload["chapters"]),
        "blocks_sampled": attempted,
        "legacy_roundtrip_exact": mapped,
        "publisher_quote_unique": original_unique,
        "ambiguous_without_context": ambiguous,
        "failures": failures[:10],
        "bridge_bytes": len(encoded),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "result": "PASS" if attempted and mapped == attempted else "FAIL",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("epubs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = [validate(path) for path in args.epubs]
    payload = {"results": results, "passed": sum(x["result"] == "PASS" for x in results)}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
