"""Command line entry for the contact crawler."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from contact_crawler.crawl import SiteResult, crawl_target
from contact_crawler.fetch import Fetcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract public emails, phones, and addresses from websites.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Domains, full URLs, or a text file with one target per line",
    )
    parser.add_argument("-o", "--output", help="Also write JSON results to this file")
    parser.add_argument("--csv", help="Write a one-row-per-site CSV summary")
    parser.add_argument("--timeout", type=float, default=15, help="Per-request timeout in seconds")
    parser.add_argument("--workers", type=int, default=10, help="How many sites to fetch at once")
    parser.add_argument(
        "--rate",
        type=float,
        default=10,
        help="Most HTTP requests started per second across all workers (0 = no limit)",
    )
    parser.add_argument(
        "--max-contact-pages",
        type=int,
        default=4,
        help="Extra pages to open when the homepage has no usable email",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Do not try common /contact paths when no contact link is found",
    )
    args = parser.parse_args(argv)

    targets = _load_inputs(args.inputs)
    if not targets:
        print("No domains to crawl.", file=sys.stderr)
        return 2

    fetcher = Fetcher(timeout=args.timeout, requests_per_second=max(0.0, args.rate))
    results = _crawl_all(
        targets,
        fetcher,
        workers=max(1, args.workers),
        max_contact_pages=max(0, args.max_contact_pages),
        probe=not args.no_probe,
    )
    payload = [result.to_dict() for result in results]
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    if args.csv:
        _write_csv(Path(args.csv), results)
    return 0


def _load_inputs(items: list[str]) -> list[str]:
    targets: list[str] = []
    for item in items:
        path = Path(item)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    targets.append(line)
        elif item.strip():
            targets.append(item.strip())
    return targets


def _crawl_all(
    targets: list[str],
    fetcher: Fetcher,
    workers: int,
    max_contact_pages: int,
    probe: bool,
) -> list[SiteResult]:
    results: list[SiteResult | None] = [None] * len(targets)

    def run(index: int, query: str) -> tuple[int, SiteResult]:
        result = crawl_target(
            query,
            fetcher=fetcher,
            max_contact_pages=max_contact_pages,
            probe=probe,
        )
        return index, result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, index, query) for index, query in enumerate(targets)]
        done = 0
        for future in as_completed(futures):
            index, result = future.result()
            results[index] = result
            done += 1
            print(
                f"[{done}/{len(targets)}] {result.query} -> {result.status} "
                f"emails={len(result.emails)} phones={len(result.phones)}",
                file=sys.stderr,
            )
    return [result for result in results if result is not None]


def _write_csv(path: Path, results: list[SiteResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "query",
                "status",
                "final_url",
                "title",
                "best_email",
                "emails",
                "phones",
                "addresses",
                "contact_links",
                "pages_fetched",
                "notes",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "query": result.query,
                    "status": result.status,
                    "final_url": result.final_url or "",
                    "title": result.title or "",
                    "best_email": result.best_email or "",
                    "emails": "; ".join(hit.email for hit in result.emails),
                    "phones": "; ".join(hit.phone for hit in result.phones),
                    "addresses": "; ".join(result.addresses),
                    "contact_links": "; ".join(result.contact_links),
                    "pages_fetched": "; ".join(result.pages_fetched),
                    "notes": "; ".join(result.notes),
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
