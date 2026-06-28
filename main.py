"""CLI entry point for the Google Maps restroom extractor.

Usage:
    # Run the full pipeline (Venice + Rome)
    python main.py run

    # Run only Venice
    python main.py run --cities venice

    # Run without downloading photos
    python main.py run --skip-photos

    # Export existing data to CSV
    python main.py export

    # Show database stats
    python main.py stats

    # Run viability test
    python main.py test
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from config import ALL_CITIES, ExtractionConfig


def setup_logging(verbose: bool = False) -> None:
    """Configure structured logging with immediate flushing."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def _resolve_cities(cities_str: str | None) -> list:
    """Resolve a comma-separated city string to CityConfig objects.

    Args:
        cities_str: Comma-separated city names (e.g. 'venice,rome'), or None
            for all cities.

    Returns:
        List of matching CityConfig objects.

    Raises:
        SystemExit: If an unknown city name is provided.
    """
    if not cities_str:
        return list(ALL_CITIES)

    requested = [c.strip().lower() for c in cities_str.split(",")]
    available = {c.name.lower(): c for c in ALL_CITIES}

    resolved = []
    for name in requested:
        if name not in available:
            print(
                f"Error: unknown city '{name}'. "
                f"Available: {', '.join(available.keys())}",
                file=sys.stderr,
            )
            sys.exit(1)
        resolved.append(available[name])
    return resolved


async def cmd_run(args: argparse.Namespace) -> None:
    """Run the full extraction pipeline."""
    from pipeline import run_pipeline

    cities = _resolve_cities(args.cities)

    overrides: dict = {
        "headless": args.headless,
        "force_rescrape": args.force,
        "cities": cities,
        "skip_photos": args.skip_photos,
    }
    if args.max_reviews is not None:
        overrides["max_reviews_per_place"] = args.max_reviews
    if args.max_photos is not None:
        overrides["max_photos_per_place"] = args.max_photos
    if args.rate is not None:
        overrides["max_places_per_minute"] = args.rate

    config = ExtractionConfig(**overrides)
    stats = await run_pipeline(config)

    print("\n" + "=" * 60)
    print("EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"Cities processed: {', '.join(stats.cities_processed)}")
    print(f"Restrooms found: {stats.total_restrooms_found}")
    print(f"Restrooms saved: {stats.total_restrooms_saved}")
    print(f"Reviews extracted: {stats.total_reviews_extracted}")
    print(f"Reviews saved: {stats.total_reviews_saved}")
    print(f"Photos downloaded: {stats.total_photos_downloaded}")
    if stats.errors:
        print(f"Errors: {len(stats.errors)}")
        for err in stats.errors[:5]:
            print(f"  - {err}")
    if stats.started_at and stats.finished_at:
        duration = (stats.finished_at - stats.started_at).total_seconds()
        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        print(f"Duration: {hours}h {minutes}m")
    print("=" * 60)


async def cmd_test(args: argparse.Namespace) -> None:
    """Run the viability test."""
    from test_viability import run_viability_test

    success = await run_viability_test()
    sys.exit(0 if success else 1)


async def cmd_export(args: argparse.Namespace) -> None:
    """Export existing data to CSV."""
    from storage.csv_exporter import export_to_csv
    from storage.sqlite_store import SQLiteStore

    config = ExtractionConfig()
    store = SQLiteStore(config)
    await store.initialize()

    files = await export_to_csv(store, config)
    print("\nExported files:")
    for name, path in files.items():
        print(f"  {name}: {path}")


async def cmd_stats(args: argparse.Namespace) -> None:
    """Show database statistics."""
    from storage.sqlite_store import SQLiteStore

    config = ExtractionConfig()
    store = SQLiteStore(config)
    await store.initialize()
    stats = await store.get_stats()

    print("\n" + "=" * 40)
    print("DATABASE STATISTICS")
    print("=" * 40)
    print(f"Total restrooms: {stats['total_restrooms']}")
    print(f"Total reviews: {stats['total_reviews']}")
    print(f"Restrooms with photos: {stats['restrooms_with_photos']}")
    if stats.get("cities"):
        print("By city:")
        for city_name, count in stats["cities"].items():
            print(f"  {city_name}: {count} restrooms")
    print(f"Database: {config.db_path}")
    print("=" * 40)


def main() -> None:
    """Parse CLI arguments and run the appropriate command."""
    parser = argparse.ArgumentParser(
        description="Google Maps restroom data extractor (Venice & Rome)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run command
    run_parser = subparsers.add_parser(
        "run", help="Run the full extraction pipeline"
    )
    run_parser.add_argument(
        "--headless", action="store_true", help="Run browser in headless mode"
    )
    run_parser.add_argument(
        "--max-reviews",
        type=int,
        default=None,
        help=(
            "Hard cap on reviews per place (default: 5000). Normally unused — "
            "the scraper exits on Google's natural end-of-list signal."
        ),
    )
    run_parser.add_argument(
        "--max-photos",
        type=int,
        default=None,
        help="Max photos per place (default: 0 = unlimited).",
    )
    run_parser.add_argument(
        "--rate",
        type=float,
        default=None,
        help=(
            "Global rate cap: places per minute (default: 2). Lower this if "
            "you start hitting CAPTCHAs."
        ),
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-scrape places that already have reviews in the DB.",
    )
    run_parser.add_argument(
        "--cities",
        type=str,
        default=None,
        help=(
            "Comma-separated list of cities to process (default: all). "
            "Available: venice, rome"
        ),
    )
    run_parser.add_argument(
        "--skip-photos",
        action="store_true",
        help="Skip photo download phase.",
    )

    # test command
    subparsers.add_parser("test", help="Run the viability test")

    # export command
    subparsers.add_parser("export", help="Export data to CSV")

    # stats command
    subparsers.add_parser("stats", help="Show database statistics")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "run": cmd_run,
        "test": cmd_test,
        "export": cmd_export,
        "stats": cmd_stats,
    }

    asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()