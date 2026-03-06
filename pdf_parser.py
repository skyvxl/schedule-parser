from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from html import unescape
from pathlib import Path

import pdfplumber

DEFAULT_YEAR = date.today().year
WEEKDAY_VALUE = "freak"
TYPE_MAP = {
    "Лекция": "Лекция",
    "Семинар": "Семинар",
    "Лабораторная": "Лабораторная работа",
}


@dataclass(slots=True)
class ParsedItem:
    name: str
    type_: str
    sub_group: str | None
    teacher: list[str] | None
    office: str | None
    time: list[dict[str, str]]
    dates: list[str]
    sort_index: float
    weekday: str = WEEKDAY_VALUE

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type_": self.type_,
            "sub_group": self.sub_group,
            "teacher": self.teacher,
            "office": self.office,
            "time": self.time,
            "dates": self.dates,
            "weekday": self.weekday,
        }


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def parse_date_token(token: str, year: int) -> date:
    return date(year, int(token[3:5]), int(token[:2]))


def expand_dates(expr: str, year: int) -> list[str]:
    dates: list[str] = []
    for part in [chunk.strip() for chunk in expr.split(",") if chunk.strip()]:
        range_match = re.fullmatch(
            r"(\d{2}\.\d{2})-(\d{2}\.\d{2})(?:\s+(ч\.н\.|к\.н\.))?",
            part,
        )
        if range_match:
            current = parse_date_token(range_match.group(1), year)
            end = parse_date_token(range_match.group(2), year)
            step_days = 14 if range_match.group(3) == "ч.н." else 7
            while current <= end:
                dates.append(current.isoformat())
                current += timedelta(days=step_days)
            continue

        if re.fullmatch(r"\d{2}\.\d{2}", part):
            dates.append(parse_date_token(part, year).isoformat())
            continue

        raise ValueError(f"Unsupported date token: {part!r}")

    return dates


def parse_entry(
    entry_text: str,
    time_ranges: list[tuple[str, str]],
    *,
    year: int,
    sort_index: float,
) -> ParsedItem:
    entry_text = normalize_text(entry_text)
    date_match = re.search(r"\[(.+)\]\s*$", entry_text)
    if not date_match:
        raise ValueError(f"Missing date range in entry: {entry_text!r}")

    prefix = entry_text[: date_match.start()].strip()
    dates_expr = date_match.group(1).strip()

    type_match = re.search(r"(Лекция|Семинар|Лабораторная)\.\s*(.*)$", prefix)
    if not type_match:
        raise ValueError(f"Missing type in entry: {entry_text!r}")

    type_label = type_match.group(1)
    remainder = type_match.group(2).strip()
    before_type = prefix[: type_match.start()].strip()

    teacher_match = re.search(
        r"([А-ЯЁ][а-яё-]+\s+[А-Я]\.\s*[А-Я]\.)\s*$",
        before_type,
    )
    teacher: list[str] | None = None
    if teacher_match:
        teacher = [
            normalize_text(teacher_match.group(1))
            .replace(" .", ".")
            .replace(". ", ".")
        ]
        subject = before_type[: teacher_match.start()].rstrip(" .")
    else:
        subject = before_type.rstrip(" .")

    subgroup: str | None = None
    subgroup_match = re.match(r"(\([АБ]\))\.\s*(.*)$", remainder)
    if subgroup_match:
        subgroup = subgroup_match.group(1)
        remainder = subgroup_match.group(2).strip()

    office = remainder.rstrip(" .") or None
    dates = expand_dates(dates_expr, year)

    return ParsedItem(
        name=subject,
        type_=TYPE_MAP[type_label],
        sub_group=subgroup,
        teacher=teacher,
        office=office,
        time=[{"start": start, "end": end} for start, end in time_ranges],
        dates=dates,
        sort_index=sort_index,
    )


def time_sort_value(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def build_time_ranges(page: pdfplumber.page.Page, table) -> list[tuple[str, str]]:
    time_ranges: list[tuple[str, str]] = []
    for header_cell in table.rows[0].cells[1:]:
        if header_cell is None:
            continue
        header_text = normalize_text(page.crop(header_cell).extract_text() or "")
        start, end = [part.strip() for part in header_text.split("-")]
        time_ranges.append((start, end))
    return time_ranges


def cell_time_ranges(
    table,
    header_ranges: list[tuple[str, str]],
    cell_bbox,
) -> list[tuple[str, str]]:
    x0, _, x1, _ = cell_bbox
    ranges: list[tuple[str, str]] = []
    for header_cell, time_range in zip(table.rows[0].cells[1:], header_ranges):
        if header_cell is None:
            continue
        hx0, _, hx1, _ = header_cell
        if x0 <= hx0 + 0.5 and x1 >= hx1 - 0.5:
            ranges.append(time_range)
    return ranges


def extract_entries_from_cell(cell_text: str) -> list[str]:
    return re.findall(r".*?\[[^\]]+\]", normalize_text(cell_text), re.S)


def parse_table(page, table, *, year: int, start_index: int) -> tuple[list[ParsedItem], int]:
    header_ranges = build_time_ranges(page, table)
    parsed_items: list[ParsedItem] = []
    sort_index = start_index

    for row in table.rows[1:]:
        for cell_bbox in row.cells[1:]:
            if cell_bbox is None:
                continue

            cell_text = page.crop(cell_bbox).extract_text(x_tolerance=1, y_tolerance=1)
            if not cell_text or "[" not in cell_text:
                continue

            time_ranges = cell_time_ranges(table, header_ranges, cell_bbox)
            if not time_ranges:
                continue

            for entry_text in extract_entries_from_cell(cell_text):
                parsed_items.append(
                    parse_entry(
                        entry_text,
                        time_ranges,
                        year=year,
                        sort_index=float(sort_index),
                    )
                )
                sort_index += 1

    return parsed_items, sort_index


def sort_items(items: list[ParsedItem]) -> None:
    items.sort(
        key=lambda item: (
            min(item.dates),
            time_sort_value(item.time[0]["start"]),
            item.sort_index,
        )
    )


def parse_pdf(
    pdf_path: Path,
    *,
    year: int = DEFAULT_YEAR,
) -> list[dict[str, object]]:
    with pdfplumber.open(pdf_path) as pdf:
        parsed_items: list[ParsedItem] = []
        sort_index = 0

        for page in pdf.pages:
            for table in page.find_tables():
                table_items, sort_index = parse_table(
                    page,
                    table,
                    year=year,
                    start_index=sort_index,
                )
                parsed_items.extend(table_items)

    sort_items(parsed_items)
    return [item.as_dict() for item in parsed_items]


def output_path_for_pdf(pdf_path: Path) -> Path:
    return pdf_path.with_name(f"parsed_{pdf_path.stem}.json")


def write_parsed_json(
    pdf_path: Path,
    parsed: list[dict[str, object]],
    *,
    output_path: Path | None = None,
) -> Path:
    target_path = output_path or output_path_for_pdf(pdf_path)
    target_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    return target_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "pdf_paths",
        nargs="*",
        type=Path,
        help="PDF files to parse. Default: all *.pdf in the current directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Custom output path. Can only be used with one PDF file.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=DEFAULT_YEAR,
        help=f"Academic year for dd.mm dates. Default: {DEFAULT_YEAR}.",
    )
    return parser.parse_args()


def discover_pdf_paths(explicit_paths: list[Path]) -> list[Path]:
    if explicit_paths:
        return explicit_paths
    return sorted(Path.cwd().glob("*.pdf"))


def main() -> None:
    args = parse_args()
    pdf_paths = discover_pdf_paths(args.pdf_paths)
    if not pdf_paths:
        raise SystemExit("No PDF files found")

    if args.output and len(pdf_paths) != 1:
        raise SystemExit("--output can only be used with one PDF file")

    for pdf_path in pdf_paths:
        parsed = parse_pdf(
            pdf_path,
            year=args.year,
        )
        target_path = write_parsed_json(
            pdf_path,
            parsed,
            output_path=args.output,
        )
        print(target_path)


if __name__ == "__main__":
    main()
