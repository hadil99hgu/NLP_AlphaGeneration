from __future__ import annotations

import argparse
import csv
import json
import re
import sys

from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote
from src.edgar.client import SecClient

from src.config import (
    load_config,
    resolve_end_date,
    resolve_path,
)

def parse_iso_date(value: str | None) -> date | None:
    """Convert a YYYY-MM-DD string to a date object."""
    if value is None:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid date: {value!r}. Expected format: YYYY-MM-DD."
        ) from exc

def safe_filename(value: str) -> str:
    """Convert a string into a safe filename."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned or "unnamed"

def write_manifest(
    rows: list[dict[str, Any]],
    output_path: Path,
    fieldnames: list[str],
) -> None:
    """Write a CSV manifest describing downloaded artifacts."""

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        mode="w",
        encoding="utf-8",
        newline="",
    ) as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)

def normalize_cik(cik: int | str) -> tuple[int, str]:
    """Return the CIK as an integer and as a zero-padded 10-digit string."""
    cik_int = int(str(cik).lstrip("0") or "0")

    if cik_int <= 0:
        raise ValueError(f"Invalid CIK: {cik!r}")

    return cik_int, f"{cik_int:010d}"




def columnar_to_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the SEC column-oriented format into a list of row dictionaries."""
    columns = {
        key: values
        for key, values in data.items()
        if isinstance(values, list)
    }

    if not columns:
        return []

    row_count = max(len(values) for values in columns.values())

    return [
        {
            key: values[index] if index < len(values) else None
            for key, values in columns.items()
        }
        for index in range(row_count)
    ]




def load_or_download_json(
    client: SecClient,
    *,
    url: str,
    output_path: Path,
) -> dict[str, Any]:
    """Read a cached JSON file or download it."""
    if output_path.exists() and output_path.stat().st_size > 0:
        return json.loads(
            output_path.read_text(encoding="utf-8")
        )

    payload = client.get_json(url)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return payload


def download_ticker_mapping(
    client: SecClient,
    output_dir: Path,
    url: str,
) -> dict[str, dict[str, Any]]:
    """Download and build the ticker-to-CIK mapping."""

    payload = client.get_json(url)

    mapping: dict[str, dict[str, Any]] = {}

    for record in payload.values():
        if not isinstance(record, dict):
            continue

        ticker = str(
            record.get("ticker", "")
        ).upper().strip()

        cik_value = record.get("cik_str")

        if not ticker or cik_value is None:
            continue

        cik, cik10 = normalize_cik(cik_value)

        mapping[ticker] = {
            "ticker": ticker,
            "cik": cik,
            "cik10": cik10,
            "company_name": record.get("title"),
        }

        print(
            f"Mapping added: {ticker} -> "
            f"CIK: {cik}, "
            f"CIK10: {cik10}, "
            f"Company Name: {record.get('title')}"
        )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = output_dir / "ticker_mapping.json"

    output_path.write_text(
        json.dumps(
            mapping,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return mapping


def download_submission_history(
    client: SecClient,
    *,
    cik10: str,
    output_dir: Path,
    submissions_url: str,
    submissions_extra_url: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Download the complete SEC submission history for one issuer."""

    submissions_dir = output_dir / "submissions"

    # -------------------------------------------------------------
    # 1. Download the main submissions JSON
    # -------------------------------------------------------------

    main_url = submissions_url.format(
        cik10=cik10,
    )

    main_payload = load_or_download_json(
        client,
        url=main_url,
        output_path=(
            submissions_dir
            / f"CIK{cik10}.json"
        ),
    )

    # -------------------------------------------------------------
    # 2. Extract recent filings
    # -------------------------------------------------------------

    filings = columnar_to_rows(
        main_payload
        .get("filings", {})
        .get("recent", {})
    )

    # -------------------------------------------------------------
    # 3. Find historical submission files
    # -------------------------------------------------------------

    historical_files = (
        main_payload
        .get("filings", {})
        .get("files", [])
    )

    # -------------------------------------------------------------
    # 4. Download and append historical filings
    # -------------------------------------------------------------

    for file_info in historical_files:

        if not isinstance(file_info, dict):
            continue

        filename = file_info.get("name")

        if not filename:
            continue

        extra_url = submissions_extra_url.format(
            filename=filename,
        )

        extra_payload = load_or_download_json(
            client,
            url=extra_url,
            output_path=(
                submissions_dir
                / safe_filename(filename)
            ),
        )

        historical_filings = columnar_to_rows(
            extra_payload
        )

        filings.extend(
            historical_filings
        )

    # -------------------------------------------------------------
    # 5. Deduplicate by accession number
    # -------------------------------------------------------------

    unique_filings: dict[
        str,
        dict[str, Any],
    ] = {}

    for filing in filings:

        accession_number = filing.get(
            "accessionNumber"
        )

        if not accession_number:
            continue

        unique_filings[
            str(accession_number)
        ] = filing

    filings = list(
        unique_filings.values()
    )

    return main_payload, filings

def select_filings(
    filings: list[dict[str, Any]],
    *,
    forms: set[str],
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, Any]]:
    """Filter filings by form type and date range."""
    selected: list[dict[str, Any]] = []

    for filing in filings:
        form = str(
            filing.get("form", "")
        ).upper()

        filing_date = parse_iso_date(
            filing.get("filingDate")
        )

        if form not in forms:
            continue

        if filing_date is None:
            continue

        if (
            start_date is not None
            and filing_date < start_date
        ):
            continue

        if (
            end_date is not None
            and filing_date >= end_date
        ):
            continue

        if not filing.get("accessionNumber"):
            continue

        if not filing.get("primaryDocument"):
            continue

        selected.append(filing)

    selected.sort(
        key=lambda filing: (
            str(filing.get("filingDate", "")),
            str(filing.get("accessionNumber", "")),
        )
    )

    return selected


def build_document_url(
    *,
    cik: int,
    accession_number: str,
    primary_document: str,
    document_url_template: str,
) -> str:
    """Build the URL of a filing's primary document in the EDGAR archives."""

    accession_compact = accession_number.replace(
        "-",
        "",
    )

    return document_url_template.format(
        cik=cik,
        accession_compact=accession_compact,
        primary_document=quote(
            primary_document,
            safe="",
        ),
    )

def download_primary_document(
    client: SecClient,
    *,
    ticker: str,
    cik: int,
    filing: dict[str, Any],
    output_dir: Path,
    document_url_template: str,
) -> tuple[Path, str]:
    """Download the primary document for a filing."""
    accession_number = str(
        filing["accessionNumber"]
    )

    primary_document = str(
        filing["primaryDocument"]
    )

    filing_date = str(
        filing["filingDate"]
    )

    form = str(
        filing["form"]
    )

    ticker_dir = (
        output_dir
        / "filings"
        / safe_filename(ticker)
    )

    ticker_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    suffix = (
        Path(primary_document).suffix
        or ".html"
    )

    output_path = ticker_dir / safe_filename(
        f"{filing_date}_{form}_"
        f"{accession_number.replace('-', '')}"
        f"{suffix}"
    )

    document_url = build_document_url(
        cik=cik,
        accession_number=accession_number,
        primary_document=primary_document,
        document_url_template=document_url_template,
    )

    if (
        output_path.exists()
        and output_path.stat().st_size > 0
    ):
        print(
            f"[cached]     "
            f"{ticker} "
            f"{form} "
            f"{filing_date}"
        )

        return output_path, document_url

    response = client.get(document_url)

    output_path.write_bytes(
        response.content
    )

    print(
        f"[downloaded] "
        f"{ticker} "
        f"{form} "
        f"{filing_date}"
    )

    return output_path, document_url


def write_metadata_csv(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    """Write the CSV index of downloaded documents."""
    fieldnames = [
        "ticker",
        "company_name",
        "cik",
        "cik10",
        "form",
        "filing_date",
        "report_date",
        "acceptance_datetime",
        "accession_number",
        "primary_document",
        "document_url",
        "local_path",
    ]

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(rows)


def run_download(
    client: SecClient,
    *,
    tickers: list[str],
    forms: list[str],
    start: str,
    end: str,
    output_dir: Path,
    company_tickers_url: str,
    submissions_url: str,
    submissions_extra_url: str,
    document_url_template: str,
    max_filings: int | None = None,
) -> Path:
    """Orchestrate the SEC filing download process."""

    mapping = download_ticker_mapping(
        client,
        output_dir=output_dir,
        url=company_tickers_url,
    )

    start_date = parse_iso_date(start)
    end_date = parse_iso_date(end)

    accepted_forms = {
        form.upper()
        for form in forms
    }

    metadata_rows: list[dict[str, Any]] = []

    downloaded_count = 0

    for raw_ticker in tickers:
        ticker = raw_ticker.upper().strip()

        company = mapping.get(ticker)

        if company is None:
            print(
                f"[warning] Ticker not found "
                f"in SEC mapping: {ticker}",
                file=sys.stderr,
            )
            continue

        print(
            f"\n=== {ticker} — "
            f"{company['company_name']} "
            f"(CIK {company['cik10']}) ==="
        )

        _, filings = download_submission_history(
            client,
            cik10=company["cik10"],
            output_dir=output_dir,
            submissions_url=submissions_url,
            submissions_extra_url=submissions_extra_url,
        )

        filings = select_filings(
            filings,
            forms=accepted_forms,
            start_date=start_date,
            end_date=end_date,
        )

        for filing in filings:
            if (
                max_filings is not None
                and downloaded_count >= max_filings
            ):
                break

            try:
                local_path, document_url = (
                    download_primary_document(
                        client,
                        ticker=ticker,
                        cik=company["cik"],
                        filing=filing,
                        output_dir=output_dir,
                        document_url_template=document_url_template,
                    )
                )

            except Exception as exc:
                print(
                    f"[error] {ticker} "
                    f"{filing.get('accessionNumber')}: "
                    f"{exc}",
                    file=sys.stderr,
                )
                continue

            metadata_rows.append(
                {
                    "ticker": ticker,
                    "company_name": company["company_name"],
                    "cik": company["cik"],
                    "cik10": company["cik10"],
                    "form": filing.get("form"),
                    "filing_date": filing.get(
                        "filingDate"
                    ),
                    "report_date": filing.get(
                        "reportDate"
                    ),
                    "acceptance_datetime": filing.get(
                        "acceptanceDateTime"
                    ),
                    "accession_number": filing.get(
                        "accessionNumber"
                    ),
                    "primary_document": filing.get(
                        "primaryDocument"
                    ),
                    "document_url": document_url,
                    "local_path": str(local_path),
                }
            )

            downloaded_count += 1

        if (
            max_filings is not None
            and downloaded_count >= max_filings
        ):
            break

    metadata_path = (
        output_dir
        / "filings_metadata.csv"
    )

    write_metadata_csv(
        metadata_rows,
        metadata_path,
    )

    print("\nDownload complete.")
    print(
        f"Indexed documents: "
        f"{len(metadata_rows)}"
    )
    print(
        f"CSV index: "
        f"{metadata_path.resolve()}"
    )

    return metadata_path


def main() -> None:
    config = load_config(
        Path("configs/data.yaml")
    )

    sec_config = config["sec"]
    urls = sec_config["urls"]

    start_date = sec_config["start_date"]

    end_date = resolve_end_date(
        sec_config["end_date"]
    )

    raw_dir = resolve_path(
        config["paths"]["raw"]
    )

    sec_output_dir = (
        raw_dir
        / "sec"
    )

    client = SecClient(
        name="Hadil Ben Selma",
        email="hadylbenselma@gmail.com",
        max_requests_per_second=(
            sec_config[
                "max_requests_per_second"
            ]
        ),
    )

    try:
        run_download(
            client,
            tickers=sec_config["tickers"],
            forms=sec_config["forms"],
            start=start_date,
            end=end_date,
            output_dir=sec_output_dir,
            company_tickers_url=(
                urls["company_tickers"]
            ),
            submissions_url=(
                urls["submissions"]
            ),
            submissions_extra_url=(
                urls["submissions_extra"]
            ),
            document_url_template=(
                urls["document"]
            ),
        )

    finally:
        client.close()


if __name__ == "__main__":
    main()