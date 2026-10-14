"""Run the same deterministic table/plot logic locally, without LLMs or Docker."""

import argparse
import json
from pathlib import Path

from matching import expand_columns, match_column, match_table
from tables import Table, parse_csv, parse_markdown


def resolve_y_columns(table: Table, x: str | None, requests: list[str]) -> tuple[list[str], list[str]]:
    """Resolve each --y request to real columns; collective phrases expand to a group."""
    numeric = table.numeric_columns()
    resolved: list[str] = []
    unresolved: list[str] = []
    for request in requests:
        column = match_column(table.columns, request)
        if column and column in numeric and column != x:
            resolved.append(column)
            continue
        expanded = expand_columns(table.columns, numeric, request, x)
        if expanded:
            resolved.extend(expanded)
            continue
        unresolved.append(request)
    return list(dict.fromkeys(resolved)), unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List captions and columns in Markdown")
    listing.add_argument("source", type=Path)
    extraction = commands.add_parser("extract", help="Extract one Markdown table to CSV and JSON")
    extraction.add_argument("source", type=Path)
    extraction.add_argument("--query", required=True)
    extraction.add_argument("--output", type=Path, required=True, help="Output filename stem")
    plotting = commands.add_parser("plot", help="Plot CSV/TSV or extracted JSON")
    plotting.add_argument("source", type=Path)
    plotting.add_argument("--x", required=True)
    plotting.add_argument("--y", nargs="+", required=True)
    plotting.add_argument("--output", type=Path, required=True)
    plotting.add_argument(
        "--chart-type", choices=["auto", "line", "scatter", "bar"], default="auto"
    )
    args = parser.parse_args()
    try:
        text = args.source.read_text(encoding="utf-8-sig")
        if args.command in {"list", "extract"}:
            tables = parse_markdown(text)
            if args.command == "list":
                print(
                    json.dumps(
                        [
                            {"caption": t.caption, "columns": t.columns, "rows": len(t.rows)}
                            for t in tables
                        ],
                        indent=2,
                    )
                )
                return
            index = match_table(tables, args.query)
            if index is None:
                parser.error("No unique caption match. Run list and provide more specific words.")
            table = tables[index]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.with_suffix(".csv").write_text(table.to_csv(), encoding="utf-8")
            args.output.with_suffix(".json").write_text(
                json.dumps(table.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"Extracted {len(table.rows)} rows: {args.output}.csv and .json")
        else:
            from plotting import render_plot

            table = (
                Table.from_dict(json.loads(text))
                if args.source.suffix.lower() == ".json"
                else parse_csv(text, args.source.stem)
            )
            x = match_column(table.columns, args.x)
            ys, unresolved = resolve_y_columns(table, x, args.y)
            if not x or unresolved or not ys:
                parser.error(f"Choose unambiguous columns from: {', '.join(table.columns)}")
            print(json.dumps(render_plot(table, x, ys, args.output, args.chart_type)))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
