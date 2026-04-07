#!/usr/bin/env python3
"""Cafe Scraper Pipeline — CLI entry point.

Orchestrates the data pipeline:
  1. Scrape (Google Maps, TikTok) → raw JSON
  2. Extract & normalize → normalized JSON per schema

Usage:
  # Full pipeline (scrape + extract)
  python main.py --keyword "coffee" --city "Samarinda" --tiktok-query "cafe samarinda"

  # Scrape only
  python main.py scrape --keyword "coffee" --city "Samarinda"

  # Extract only (from existing raw data)
  python main.py extract --date 2026-04-05

  # List available raw data
  python main.py list --date 2026-04-05
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = str(Path(__file__).parent)
sys.path.insert(0, PROJECT_ROOT)

from pipeline_utils import (
    generate_run_id,
    get_run_date,
    get_timestamp,
    load_json,
    save_json,
)
from models.schemas import ScrapeRun


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cafe-pipeline")


def _resolve_run_date(value: str | None) -> str:
    run_date = value or get_run_date()
    datetime.strptime(run_date, "%Y-%m-%d")
    return run_date


def _resolve_run_id(value: str | None) -> str:
    return value or generate_run_id()


def _result_count(result: Any) -> int:
    if result is None:
        return 0
    if isinstance(result, bool):
        return int(result)
    if isinstance(result, int):
        return result
    if isinstance(result, (list, tuple, set)):
        return len(result)
    if isinstance(result, dict):
        for key in ("total", "count"):
            value = result.get(key)
            if isinstance(value, int):
                return value
        for key in ("results", "records", "items", "data"):
            value = result.get(key)
            if isinstance(value, (list, tuple, set)):
                return len(value)
    return 0


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _summarize_tree(root: Path, date_filter: str | None = None) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    if not root.exists():
        return summary

    for date_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name):
        if date_filter and date_dir.name != date_filter:
            continue

        children: list[dict[str, Any]] = []
        direct_files = [item for item in date_dir.iterdir() if item.is_file()]
        if direct_files:
            children.append(
                {
                    "name": ".",
                    "file_count": len(direct_files),
                    "size_bytes": sum(item.stat().st_size for item in direct_files),
                }
            )

        for child_dir in sorted((item for item in date_dir.iterdir() if item.is_dir()), key=lambda p: p.name):
            file_count = 0
            size_bytes = 0
            for walk_root, _, files in os.walk(child_dir):
                for filename in files:
                    path = Path(walk_root) / filename
                    try:
                        size_bytes += path.stat().st_size
                        file_count += 1
                    except OSError:
                        logger.warning("Skipping unreadable file during listing: %s", path)

            children.append(
                {
                    "name": child_dir.name,
                    "file_count": file_count,
                    "size_bytes": size_bytes,
                }
            )

        summary.append({"date": date_dir.name, "children": children})

    return summary


def _log_tree_summary(title: str, summary: list[dict[str, Any]]) -> None:
    logger.info(title)
    if not summary:
        logger.info("  (no data found)")
        return

    for date_entry in summary:
        logger.info("  %s", date_entry["date"])
        children = date_entry["children"]
        if not children:
            logger.info("    (empty)")
            continue
        for child in children:
            logger.info(
                "    %-20s files=%-4s size=%s",
                child["name"],
                child["file_count"],
                _format_bytes(child["size_bytes"]),
            )


def _metadata_path(output_dir: str, run_date: str, run_id: str) -> Path:
    return Path(output_dir) / run_date / "_runs" / f"{run_id}.json"


def _save_run_metadata(path: Path, payload: dict[str, Any]) -> None:
    save_json(str(path), payload)
    logger.info("Saved run metadata: %s", path)


async def cmd_scrape(args):
    """Run scrapers and save raw data."""
    run_id = _resolve_run_id(getattr(args, "run_id", None))
    run_date = _resolve_run_date(getattr(args, "run_date", None))
    started_at = get_timestamp()
    metadata_path = _metadata_path(args.output_dir, run_date, run_id)

    sources: list[str] = []
    queries: list[str] = []
    if args.keyword and args.city:
        sources.append("google_maps")
        queries.append(f"{args.keyword} {args.city}".strip())
    if args.tiktok_query:
        sources.append("tiktok")
        queries.append(args.tiktok_query)

    scrape_run = ScrapeRun(
        run_id=run_id,
        run_date=run_date,
        sources=sources,
        queries=queries,
        started_at=started_at,
        status="running",
    )
    _save_run_metadata(metadata_path, scrape_run.to_json_dict())

    logger.info("Starting scrape run %s for date %s", run_id, run_date)

    total_raw_records = 0
    completed_sources: list[str] = []
    failed_sources: list[str] = []

    if args.keyword and args.city:
        try:
            scrape_google_maps = getattr(
                importlib.import_module("pipelines.google_maps_scraper"),
                "scrape_google_maps",
            )
        except (ImportError, AttributeError) as exc:
            logger.warning("Google Maps scraper unavailable, skipping: %s", exc)
        else:
            try:
                result = await scrape_google_maps(
                    keyword=args.keyword,
                    city=args.city,
                    max_results=args.max_results,
                    headless=not args.show_browser,
                    output_dir=args.output_dir,
                    run_id=run_id,
                    run_date=run_date,
                )
                record_count = _result_count(result)
                total_raw_records += record_count
                completed_sources.append("google_maps")
                logger.info("Google Maps scrape completed with %s record(s)", record_count)
            except Exception:
                failed_sources.append("google_maps")
                logger.exception("Google Maps scrape failed")

    if args.tiktok_query:
        try:
            scrape_tiktok = getattr(
                importlib.import_module("pipelines.tiktok_scraper"),
                "scrape_tiktok",
            )
        except (ImportError, AttributeError) as exc:
            logger.warning("TikTok scraper unavailable, skipping: %s", exc)
        else:
            try:
                result = await scrape_tiktok(
                    query=args.tiktok_query,
                    max_results=args.tiktok_max,
                    headless=not args.show_browser,
                    output_dir=args.output_dir,
                    run_id=run_id,
                    run_date=run_date,
                )
                record_count = _result_count(result)
                total_raw_records += record_count
                completed_sources.append("tiktok")
                logger.info("TikTok scrape completed with %s record(s)", record_count)
            except Exception:
                failed_sources.append("tiktok")
                logger.exception("TikTok scrape failed")

    run_payload = load_json(str(metadata_path))
    if not isinstance(run_payload, dict):
        run_payload = scrape_run.to_json_dict()

    run_payload.update(
        {
            "completed_at": get_timestamp(),
            "total_raw_records": total_raw_records,
            "status": "failed" if failed_sources else "completed",
        }
    )
    _save_run_metadata(metadata_path, run_payload)

    logger.info(
        "Scrape summary | run_id=%s | completed=%s | failed=%s | total_raw_records=%s",
        run_id,
        completed_sources or ["none"],
        failed_sources or ["none"],
        total_raw_records,
    )

    return {
        "run_id": run_id,
        "run_date": run_date,
        "metadata_path": str(metadata_path),
        "completed_sources": completed_sources,
        "failed_sources": failed_sources,
        "total_raw_records": total_raw_records,
    }


def cmd_extract(args):
    """Run extraction/normalization on existing raw data."""
    run_id = getattr(args, "run_id", None)
    run_date = _resolve_run_date(getattr(args, "run_date", None))
    logger.info(
        "Starting extraction for date %s (run_id=%s) from %s into %s",
        run_date,
        run_id or "auto",
        args.raw_dir,
        args.normalized_dir,
    )

    try:
        run_extraction = getattr(importlib.import_module("pipelines.extractor"), "run_extraction")
    except (ImportError, AttributeError) as exc:
        logger.warning("Extractor unavailable, skipping extraction: %s", exc)
        return {"status": "skipped", "reason": str(exc), "run_id": run_id, "run_date": run_date}

    result = run_extraction(
        raw_dir=args.raw_dir,
        normalized_dir=args.normalized_dir,
        run_id=run_id,
        run_date=run_date,
    )
    logger.info("Extraction summary: %s", result)
    return result


def cmd_list(args):
    """List available raw data directories and files."""
    date_filter = None
    run_id = getattr(args, "run_id", None)
    if getattr(args, "run_date", None):
        date_filter = _resolve_run_date(args.run_date)

    raw_summary = _summarize_tree(Path(args.raw_dir), date_filter)
    normalized_summary = _summarize_tree(Path(args.normalized_dir), date_filter)

    logger.info("Listing data for run_date=%s run_id=%s", date_filter or "all", run_id or "all")
    _log_tree_summary("Available raw data:", raw_summary)
    _log_tree_summary("Available normalized data:", normalized_summary)

    return {
        "run_id": run_id,
        "run_date": date_filter,
        "raw_summary": raw_summary,
        "normalized_summary": normalized_summary,
    }


async def cmd_run(args):
    """Run full pipeline: scrape + extract."""
    scrape_summary = await cmd_scrape(args)
    if not hasattr(args, "raw_dir"):
        args.raw_dir = args.output_dir
    if not hasattr(args, "normalized_dir"):
        args.normalized_dir = "data/normalized"
    if not getattr(args, "run_date", None):
        args.run_date = scrape_summary["run_date"]
    if not getattr(args, "run_id", None):
        args.run_id = scrape_summary["run_id"]

    extract_summary = cmd_extract(args)
    logger.info(
        "Full pipeline complete | run_id=%s | raw_records=%s | extract_result=%s",
        scrape_summary["run_id"],
        scrape_summary["total_raw_records"],
        extract_summary,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Cafe Scraper Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Pipeline commands")

    run_parser = subparsers.add_parser("run", help="Full pipeline: scrape + extract")
    _add_common_args(run_parser)

    scrape_parser = subparsers.add_parser("scrape", help="Scrape data only")
    _add_common_args(scrape_parser)

    extract_parser = subparsers.add_parser("extract", help="Extract & normalize only")
    extract_parser.add_argument("--raw-dir", default="data/raw")
    extract_parser.add_argument("--normalized-dir", default="data/normalized")
    extract_parser.add_argument("--run-id", "--run_id", dest="run_id", default=None, help="Run ID")
    extract_parser.add_argument(
        "--date",
        "--run-date",
        "--run_date",
        dest="run_date",
        default=None,
        help="YYYY-MM-DD (default: today)",
    )

    list_parser = subparsers.add_parser("list", help="List available data")
    list_parser.add_argument("--run-id", "--run_id", dest="run_id", default=None)
    list_parser.add_argument("--date", "--run-date", "--run_date", dest="run_date", default=None)
    list_parser.add_argument("--raw-dir", default="data/raw")
    list_parser.add_argument("--normalized-dir", default="data/normalized")

    return parser


def _add_common_args(parser):
    """Add shared scraping arguments."""
    parser.add_argument("--keyword", "-k", default=None, help="Google Maps search keyword")
    parser.add_argument("--city", "-c", default=None, help="Target city")
    parser.add_argument("--max-results", "-m", type=int, default=50, help="Max Google Maps results")
    parser.add_argument("--tiktok-query", "-q", default=None, help="TikTok search query")
    parser.add_argument("--tiktok-max", type=int, default=30, help="Max TikTok results")
    parser.add_argument("--output-dir", default="data/raw")
    parser.add_argument("--show-browser", action="store_true", help="Show browser (debug)")
    parser.add_argument("--run-id", "--run_id", dest="run_id", default=None, help="Run ID")
    parser.add_argument(
        "--date",
        "--run-date",
        "--run_date",
        dest="run_date",
        default=None,
        help="Run date YYYY-MM-DD",
    )


def main():
    parser = build_parser()
    argv = sys.argv[1:]
    if argv and argv[0].startswith("-"):
        argv = ["run", *argv]

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return

    if args.command in ("run", "scrape"):
        asyncio.run(_async_main(args))
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "list":
        cmd_list(args)


async def _async_main(args):
    if args.command == "run":
        await cmd_run(args)
    elif args.command == "scrape":
        await cmd_scrape(args)


if __name__ == "__main__":
    main()
