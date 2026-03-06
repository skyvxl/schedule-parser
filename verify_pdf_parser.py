from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from itertools import zip_longest
from pathlib import Path

from pdf_parser import (
    DEFAULT_YEAR,
    output_path_for_pdf,
    parse_pdf,
    write_parsed_json,
)


def resolve_pair(target: str) -> tuple[Path, Path]:
    path = Path(target)

    if path.suffix.lower() == ".pdf":
        pdf_path = path
        expected_path = path.with_name(f"ORIGINAL_{path.stem}.json")
        return pdf_path, expected_path

    if path.suffix.lower() == ".json" and path.name.startswith("ORIGINAL_"):
        expected_path = path
        pdf_name = expected_path.name.removeprefix("ORIGINAL_").replace(".json", ".pdf")
        pdf_path = expected_path.with_name(pdf_name)
        return pdf_path, expected_path

    pdf_path = Path(f"{target}.pdf")
    expected_path = Path(f"ORIGINAL_{target}.json")
    return pdf_path, expected_path


def discover_pairs(explicit_targets: list[str]) -> list[tuple[Path, Path]]:
    if explicit_targets:
        return [resolve_pair(target) for target in explicit_targets]

    expected_paths = sorted(Path.cwd().glob("ORIGINAL_*.json"))
    return [resolve_pair(str(expected_path)) for expected_path in expected_paths]


def first_diff(
    actual: list[dict[str, object]],
    expected: list[dict[str, object]],
) -> tuple[int, object, object] | None:
    for index, (actual_item, expected_item) in enumerate(zip_longest(actual, expected)):
        if actual_item != expected_item:
            return index, actual_item, expected_item
    return None


def diff_sets(
    actual: list[dict[str, object]],
    expected: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    to_key = lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)
    actual_counter = Counter(map(to_key, actual))
    expected_counter = Counter(map(to_key, expected))
    extra = [json.loads(value) for value, count in (actual_counter - expected_counter).items() for _ in range(count)]
    missing = [json.loads(value) for value, count in (expected_counter - actual_counter).items() for _ in range(count)]
    return extra, missing


def item_signature(item: dict[str, object]) -> tuple[object, ...]:
    return (
        item.get("name"),
        item.get("type_"),
        item.get("sub_group"),
        json.dumps(item.get("teacher"), ensure_ascii=False, sort_keys=True),
        item.get("office"),
        json.dumps(item.get("time"), ensure_ascii=False, sort_keys=True),
        item.get("weekday"),
    )


def classify_mismatch(
    extra_items: list[dict[str, object]],
    missing_items: list[dict[str, object]],
) -> str | None:
    if len(extra_items) != 1 or len(missing_items) != 1:
        return None

    extra_item = extra_items[0]
    missing_item = missing_items[0]
    if item_signature(extra_item) != item_signature(missing_item):
        return None

    if extra_item.get("dates") == missing_item.get("dates"):
        return None

    return (
        "Mismatch looks like a source conflict: the lesson identity is the same, "
        "but the date set in ORIGINAL_*.json differs from the date set extracted from the PDF."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "targets",
        nargs="*",
        help=(
            "PDF path, ORIGINAL_*.json path, or bare group name "
            "(for example: ИДБ-23-01). Default: all ORIGINAL_*.json files."
        ),
    )
    parser.add_argument(
        "--year",
        type=int,
        default=DEFAULT_YEAR,
        help=f"Academic year for dd.mm dates. Default: {DEFAULT_YEAR}.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pairs = discover_pairs(args.targets)
    if not pairs:
        print("No ORIGINAL_*.json files found")
        return 1

    failed = False

    for pdf_path, expected_path in pairs:
        if not pdf_path.exists():
            print(f"FAIL: missing PDF {pdf_path}")
            failed = True
            continue
        if not expected_path.exists():
            print(f"FAIL: missing golden JSON {expected_path}")
            failed = True
            continue

        actual = parse_pdf(pdf_path, year=args.year)
        write_parsed_json(pdf_path, actual, output_path=output_path_for_pdf(pdf_path))
        expected = json.loads(expected_path.read_text(encoding="utf-8"))

        diff = first_diff(actual, expected)
        if diff is None:
            print(f"OK: {pdf_path.name}")
            continue

        failed = True
        index, actual_item, expected_item = diff
        print(f"FAIL: {pdf_path.name}")
        print(f"First diff at index {index}")
        print("Actual:")
        print(json.dumps(actual_item, ensure_ascii=False, indent=4))
        print("Expected:")
        print(json.dumps(expected_item, ensure_ascii=False, indent=4))
        extra_items, missing_items = diff_sets(actual, expected)
        if extra_items:
            print("Extra parsed item(s) not present in golden:")
            for item in extra_items[:3]:
                print(json.dumps(item, ensure_ascii=False, indent=4))
        if missing_items:
            print("Golden item(s) missing from parsed result:")
            for item in missing_items[:3]:
                print(json.dumps(item, ensure_ascii=False, indent=4))
        classification = classify_mismatch(extra_items, missing_items)
        if classification:
            print(classification)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
