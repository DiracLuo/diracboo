"""Import Qlib prediction artifacts into Alpha Ledger's model_scores table.

Reads pred.pkl (MultiIndex: datetime, instrument) produced by Qlib's SignalRecord,
converts ticker format, computes cross-sectional rank/percentile, and stores in model_scores.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .ledger import now_utc
from .tickers import qlib_instrument_to_ticker


@dataclass(frozen=True)
class ImportResult:
    model_name: str
    model_version: str
    artifact_path: str
    imported_count: int = 0
    date_range: tuple[str, str] | None = None
    ticker_mapping_failures: int = 0
    avg_stocks_per_day: float = 0.0
    warnings: tuple[str, ...] = ()


def import_qlib_predictions(
    conn: sqlite3.Connection,
    artifact_path: Path,
    model_name: str,
    model_version: str,
    market: str = "CN_A",
) -> ImportResult:
    """Import Qlib pred.pkl into model_scores table.

    Args:
        conn: Database connection.
        artifact_path: Path to pred.pkl file.
        model_name: Name identifier for the model (e.g. 'qlib_alpha360_lgb').
        model_version: Version identifier (e.g. 'smoke_v1').
        market: Market identifier for the scores.

    Returns:
        ImportResult with statistics.
    """
    artifact_path = Path(artifact_path)
    if not artifact_path.exists():
        return ImportResult(
            model_name=model_name,
            model_version=model_version,
            artifact_path=str(artifact_path),
            warnings=(f"Artifact not found: {artifact_path}",),
        )

    # Read pred.pkl
    pred = pd.read_pickle(artifact_path)

    # Handle different pred formats
    if isinstance(pred, pd.Series):
        pred = pred.to_frame("score")

    if not isinstance(pred.index, pd.MultiIndex):
        return ImportResult(
            model_name=model_name,
            model_version=model_version,
            artifact_path=str(artifact_path),
            warnings=("pred.pkl does not have MultiIndex (datetime, instrument)",),
        )

    # Extract datetime and instrument levels
    dt_level = pred.index.get_level_values("datetime")
    inst_level = pred.index.get_level_values("instrument")

    # Convert instrument to Alpha Ledger ticker
    ticker_mapping_failures = 0
    records: list[dict[str, Any]] = []

    for idx in range(len(pred)):
        dt = dt_level[idx]
        inst = str(inst_level[idx])
        score_val = float(pred.iloc[idx, 0])

        ticker = qlib_instrument_to_ticker(inst)
        if ticker is None:
            ticker_mapping_failures += 1
            continue

        score_date = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]

        records.append({
            "model_name": model_name,
            "model_version": model_version,
            "market": market,
            "ticker": ticker,
            "score_date": score_date,
            "score": score_val,
        })

    if not records:
        return ImportResult(
            model_name=model_name,
            model_version=model_version,
            artifact_path=str(artifact_path),
            ticker_mapping_failures=ticker_mapping_failures,
            warnings=("No valid records after ticker mapping",),
        )

    # Compute rank and percentile per score_date
    df = pd.DataFrame(records)
    df["rank"] = df.groupby("score_date")["score"].rank(ascending=False, method="min").astype(int)
    df["percentile"] = df.groupby("score_date")["score"].rank(ascending=True, pct=True)

    # Insert into model_scores with UPSERT
    source_artifact = str(artifact_path)
    created_at = now_utc()
    imported = 0

    for _, row in df.iterrows():
        conn.execute(
            """
            INSERT INTO model_scores
                (model_name, model_version, market, ticker, score_date, score, rank, percentile, source_artifact, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_name, model_version, market, ticker, score_date) DO UPDATE SET
                score=excluded.score,
                rank=excluded.rank,
                percentile=excluded.percentile,
                source_artifact=excluded.source_artifact,
                created_at=excluded.created_at
            """,
            (
                row["model_name"], row["model_version"], row["market"],
                row["ticker"], row["score_date"], row["score"],
                row["rank"], row["percentile"], source_artifact, created_at,
            ),
        )
        imported += 1

    conn.commit()

    # Compute stats
    dates = sorted(df["score_date"].unique())
    avg_stocks = len(df) / len(dates) if dates else 0.0
    warnings: list[str] = []
    if ticker_mapping_failures > 0:
        warnings.append(f"{ticker_mapping_failures} records skipped due to ticker mapping failure")

    return ImportResult(
        model_name=model_name,
        model_version=model_version,
        artifact_path=str(artifact_path),
        imported_count=imported,
        date_range=(dates[0], dates[-1]) if dates else None,
        ticker_mapping_failures=ticker_mapping_failures,
        avg_stocks_per_day=avg_stocks,
        warnings=tuple(warnings),
    )


def write_import_report(result: ImportResult, output_dir: Path) -> tuple[Path, Path]:
    """Write import report as md and json."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON report
    json_data: dict[str, Any] = {
        "model_name": result.model_name,
        "model_version": result.model_version,
        "artifact_path": result.artifact_path,
        "imported_count": result.imported_count,
        "date_range": list(result.date_range) if result.date_range else None,
        "ticker_mapping_failures": result.ticker_mapping_failures,
        "avg_stocks_per_day": round(result.avg_stocks_per_day, 1),
        "warnings": list(result.warnings),
    }

    json_path = output_dir / "qlib_predictions_import_report.json"
    json_path.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Markdown report
    lines: list[str] = []
    lines.append("# Qlib Predictions Import Report")
    lines.append("")
    lines.append(f"- Model: `{result.model_name}` version `{result.model_version}`")
    lines.append(f"- Artifact: `{result.artifact_path}`")
    lines.append(f"- Imported records: {result.imported_count}")
    if result.date_range:
        lines.append(f"- Date range: {result.date_range[0]} to {result.date_range[1]}")
    lines.append(f"- Ticker mapping failures: {result.ticker_mapping_failures}")
    lines.append(f"- Avg stocks per day: {result.avg_stocks_per_day:.1f}")
    lines.append("")
    if result.warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in result.warnings:
            lines.append(f"- {w}")
        lines.append("")
    else:
        lines.append("## Warnings")
        lines.append("")
        lines.append("None.")
        lines.append("")

    md_path = output_dir / "qlib_predictions_import_report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return md_path, json_path
