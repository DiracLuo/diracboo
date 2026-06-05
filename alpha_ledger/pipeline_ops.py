"""Production data/model pipeline helpers for Alpha Ledger.

This module keeps the production path separate from research training:

- qlib_refresh updates the local Qlib bin data and records a dataset version.
- model_predict runs inference with existing production models only.
- production_daily validates prepared data/predictions and writes the production
  daily report by default.
- model_arena trains research models and writes comparison artifacts without
  importing them into production model_scores.
"""

from __future__ import annotations

import json
import math
import os
import pickle
import shutil
import sqlite3
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .benchmarks import benchmark_for_asset
from .adjustments import detect_adjustment_breaks, qfq_repair_daily
from .data_ops import audit_data_coverage, data_update
from .db import connect, init_db
from .ledger import now_utc
from .metrics import trade_cost_pct
from .qlib_export import export_qlib_csv, write_quality_report
from .reporting import write_daily_plan
from .screener import refine_candidates_with_intraday, screen_all
from .tickers import qlib_instrument_to_ticker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_BIN = "/opt/anaconda3/bin/python3"
QLIB_DIR = Path.home() / ".qlib" / "qlib_data" / "alpha_ledger_full"
QLIB_EXPORT_ROOT = PROJECT_ROOT / "outputs" / "qlib_refresh"
PRODUCTION_DAILY_DIR = PROJECT_ROOT / "reports" / "production" / "daily"
PRODUCTION_RUNS_DIR = PROJECT_ROOT / "reports" / "production" / "runs"
MODEL_VALIDATION_DIR = PROJECT_ROOT / "reports" / "model_validation"
MODEL_GOVERNANCE_DIR = PROJECT_ROOT / "reports" / "model_governance"
PRODUCTION_ASYNC_DIR = PROJECT_ROOT / "reports" / "production" / "async"
DUMP_BIN = Path.home() / "code" / "external" / "qlib" / "scripts" / "dump_bin.py"
QLIB_FIELDS = ("open", "close", "high", "low", "volume", "vwap", "money", "factor", "change")


@dataclass(frozen=True)
class QlibRefreshResult:
    version: str
    mode: str
    status: str
    start_date: str
    end_date: str
    staging_dir: str
    row_count: int = 0
    ticker_count: int = 0
    error_message: str = ""


@dataclass(frozen=True)
class PredictionResult:
    run_id: str
    status: str
    as_of_date: str
    model_count: int = 0
    score_count: int = 0
    output_dir: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class ProductionDailyResult:
    status: str
    report_path: str
    error_message: str = ""


@dataclass(frozen=True)
class ProductionRunResult:
    run_id: str
    status: str
    as_of_date: str
    report_path: str
    summary_path: str
    failed_step: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class ModelValidationResult:
    status: str
    report_path: str
    model_count: int
    error_message: str = ""


@dataclass(frozen=True)
class ModelEvaluateResult:
    run_id: str
    status: str
    report_path: str
    metrics_path: str
    model_count: int
    pass_count: int
    watch_count: int
    fail_count: int
    insufficient_count: int
    error_message: str = ""


@dataclass(frozen=True)
class ProductionAsyncResult:
    status: str
    report_path: str
    task_count: int
    failed_count: int
    error_message: str = ""


@dataclass(frozen=True)
class ModelGovernanceResult:
    status: str
    report_path: str
    model_count: int
    needs_review_count: int
    error_message: str = ""


@dataclass(frozen=True)
class ArenaModelSpec:
    model_name: str
    model_version: str
    feature_set: str
    horizon_days: int
    train_start: str
    train_end: str
    valid_start: str
    valid_end: str
    test_start: str
    test_end: str

    @property
    def label_name(self) -> str:
        return f"T+{self.horizon_days}"

    @property
    def label_expr(self) -> str:
        return f"Ref($close, -{self.horizon_days}) / Ref($open, -1) - 1"


@dataclass
class ArenaModelResult:
    spec: ArenaModelSpec
    status: str
    workflow_path: str
    artifact_path: str = ""
    elapsed_seconds: float = 0.0
    ic: float | None = None
    rank_ic: float | None = None
    top_return: float | None = None
    bottom_return: float | None = None
    top_bottom_spread: float | None = None
    top_win_rate: float | None = None
    avg_return: float | None = None
    sample_count: int = 0
    error_message: str = ""

    def score_for_ranking(self) -> float:
        if self.rank_ic is not None:
            return self.rank_ic
        if self.top_bottom_spread is not None:
            return self.top_bottom_spread
        return -999.0


@dataclass(frozen=True)
class ArenaResult:
    run_id: str
    status: str
    report_path: str
    output_dir: str
    total_models: int
    completed_models: int
    failed_models: int
    recommended_model: str | None
    results: tuple[ArenaModelResult, ...] = field(default_factory=tuple)


def _compact_date(value: str) -> str:
    return value.replace("-", "")


def _run_id(prefix: str, as_of: str) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{_compact_date(as_of)}_{stamp}"


def _read_qlib_calendar_max(qlib_dir: Path | None = None) -> str | None:
    qlib_dir = qlib_dir or QLIB_DIR
    calendar_path = qlib_dir.expanduser() / "calendars" / "day.txt"
    if not calendar_path.exists():
        return None
    dates = [line.strip() for line in calendar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return max(dates) if dates else None


def _latest_successful_qlib_version(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM qlib_dataset_versions
        WHERE status = 'SUCCESS'
        ORDER BY end_date DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def _price_min_date(conn: sqlite3.Connection, market: str = "CN_A") -> str:
    row = conn.execute("SELECT MIN(date) AS d FROM price_bars WHERE market = ?", (market,)).fetchone()
    if not row or not row["d"]:
        raise RuntimeError("price_bars has no CN_A data")
    return str(row["d"])


def _insert_qlib_version(
    conn: sqlite3.Connection,
    *,
    version: str,
    as_of: str,
    start: str,
    end: str,
    mode: str,
    status: str,
    staging_dir: str,
    row_count: int = 0,
    ticker_count: int = 0,
    command: str = "",
    error_message: str = "",
) -> None:
    now = now_utc()
    conn.execute(
        """
        INSERT INTO qlib_dataset_versions (
            version, as_of_date, start_date, end_date, mode, status,
            provider_uri, qlib_dir, staging_dir, markets, fields_json,
            ticker_count, row_count, command, error_message, created_at, finished_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CN_A', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version,
            as_of,
            start,
            end,
            mode,
            status,
            str(QLIB_DIR),
            str(QLIB_DIR),
            staging_dir,
            json.dumps(list(QLIB_FIELDS)),
            ticker_count,
            row_count,
            command,
            error_message,
            now,
            now,
        ),
    )
    conn.commit()


def qlib_refresh(
    conn: sqlite3.Connection,
    as_of: str,
    mode: str = "incremental",
    max_workers: int = 8,
    output_root: Path | None = None,
) -> QlibRefreshResult:
    """Refresh Qlib bin data and record the dataset version."""
    if mode not in {"incremental", "full"}:
        raise ValueError("mode must be incremental or full")
    output_root = output_root or QLIB_EXPORT_ROOT
    version = f"qlib_{_compact_date(as_of)}_{mode}_{datetime.utcnow().strftime('%H%M%S')}"

    latest = _latest_successful_qlib_version(conn)
    calendar_max = _read_qlib_calendar_max()
    if mode == "incremental" and latest is None and calendar_max and calendar_max >= as_of:
        _insert_qlib_version(
            conn,
            version=version,
            as_of=as_of,
            start=as_of,
            end=as_of,
            mode="metadata_bootstrap",
            status="SUCCESS",
            staging_dir="",
            command="bootstrap existing qlib calendar",
        )
        return QlibRefreshResult(version, "metadata_bootstrap", "SUCCESS", as_of, as_of, "")

    if mode == "incremental" and latest and str(latest["end_date"]) >= as_of:
        _insert_qlib_version(
            conn,
            version=version,
            as_of=as_of,
            start=as_of,
            end=as_of,
            mode="incremental_noop",
            status="SUCCESS",
            staging_dir="",
            command="existing qlib version already covers as_of",
        )
        return QlibRefreshResult(version, "incremental_noop", "SUCCESS", as_of, as_of, "")

    effective_mode = mode
    if mode == "incremental" and calendar_max:
        start = (date.fromisoformat(calendar_max) + timedelta(days=1)).isoformat()
        if start > as_of:
            start = as_of
    else:
        effective_mode = "full"
        start = _price_min_date(conn)

    staging_dir = output_root / f"{_compact_date(as_of)}_{effective_mode}" / "csv"
    export_result = export_qlib_csv(conn, start, as_of, staging_dir, markets={"CN_A"})
    write_quality_report(export_result, staging_dir)

    if not DUMP_BIN.exists():
        error = f"Qlib dump_bin.py not found: {DUMP_BIN}"
        _insert_qlib_version(
            conn,
            version=version,
            as_of=as_of,
            start=start,
            end=as_of,
            mode=effective_mode,
            status="FAILED",
            staging_dir=str(staging_dir),
            row_count=export_result.total_bars,
            ticker_count=export_result.csv_count,
            error_message=error,
        )
        return QlibRefreshResult(version, effective_mode, "FAILED", start, as_of, str(staging_dir), error_message=error)

    dump_mode = "dump_update" if effective_mode == "incremental" else "dump_all"
    cmd = [
        PYTHON_BIN,
        str(DUMP_BIN),
        dump_mode,
        "--data_path",
        str(staging_dir.resolve()),
        "--qlib_dir",
        str(QLIB_DIR),
        "--include_fields",
        ",".join(QLIB_FIELDS),
        "--date_field_name",
        "date",
        "--file_suffix",
        ".csv",
        "--max_workers",
        str(max_workers),
    ]
    run = subprocess.run(cmd, cwd=str(PROJECT_ROOT), text=True, capture_output=True)
    log_dir = staging_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "dump_stdout.log").write_text(run.stdout or "", encoding="utf-8")
    (log_dir / "dump_stderr.log").write_text(run.stderr or "", encoding="utf-8")
    status = "SUCCESS" if run.returncode == 0 else "FAILED"
    error = "" if run.returncode == 0 else (run.stderr or run.stdout or f"exit {run.returncode}")[-2000:]
    _insert_qlib_version(
        conn,
        version=version,
        as_of=as_of,
        start=start,
        end=as_of,
        mode=effective_mode,
        status=status,
        staging_dir=str(staging_dir),
        row_count=export_result.total_bars,
        ticker_count=export_result.csv_count,
        command=" ".join(cmd),
        error_message=error,
    )
    return QlibRefreshResult(
        version,
        effective_mode,
        status,
        start,
        as_of,
        str(staging_dir),
        row_count=export_result.total_bars,
        ticker_count=export_result.csv_count,
        error_message=error,
    )


def _production_model_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT model_name FROM model_registry WHERE status = 'PRODUCTION' ORDER BY id"
    ).fetchall()
    return [str(row["model_name"]) for row in rows]


def _latest_qlib_version_for_as_of(conn: sqlite3.Connection, as_of: str) -> str | None:
    row = conn.execute(
        """
        SELECT version
        FROM qlib_dataset_versions
        WHERE status = 'SUCCESS' AND end_date >= ?
        ORDER BY end_date DESC, id DESC
        LIMIT 1
        """,
        (as_of,),
    ).fetchone()
    return str(row["version"]) if row else None


def _amount_coverage_pct(conn: sqlite3.Connection, as_of: str) -> float:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN p.amount IS NOT NULL AND p.amount > 0 THEN 1 ELSE 0 END) AS ok_count,
            COUNT(*) AS total_count
        FROM price_bars p
        LEFT JOIN instruments i ON i.market = p.market AND i.ticker = p.ticker
        WHERE p.market = 'CN_A' AND p.date = ?
          AND p.volume IS NOT NULL AND p.volume > 0
          AND COALESCE(i.tags_json, '') NOT LIKE '%index%'
          AND COALESCE(i.tags_json, '') NOT LIKE '%benchmark%'
          AND p.ticker NOT GLOB '399*.SZ'
          AND p.ticker NOT GLOB '000[0-9][0-9][0-9].SS'
          AND p.ticker != '899050.BJ'
        """,
        (as_of,),
    ).fetchone()
    total = int(row["total_count"] or 0) if row else 0
    if total <= 0:
        return 0.0
    return float(row["ok_count"] or 0) / total * 100.0


def model_predict(
    db_path: str,
    as_of: str,
    models: str = "production",
    output_dir: Path | None = None,
) -> PredictionResult:
    """Run production inference with saved Qlib models."""
    output_dir = output_dir or PROJECT_ROOT / "reports" / "model_prediction_logs" / as_of
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = _run_id("predict", as_of)
    started = now_utc()
    with connect(db_path) as conn:
        init_db(conn)
        qlib_version = _latest_qlib_version_for_as_of(conn, as_of)
        if not qlib_version:
            error = f"No successful qlib_dataset_version covers {as_of}"
            _write_prediction_report(output_dir, run_id, "FAILED", 0, 0, error)
            return PredictionResult(run_id, "FAILED", as_of, output_dir=str(output_dir), error_message=error)
        model_names = _production_model_names(conn)
        if not model_names:
            error = "No PRODUCTION models registered in model_registry"
            _write_prediction_report(output_dir, run_id, "FAILED", 0, 0, error)
            return PredictionResult(run_id, "FAILED", as_of, output_dir=str(output_dir), error_message=error)
        conn.execute(
            """
            INSERT INTO prediction_runs (
                run_id, as_of_date, market, models_scope, qlib_dataset_version,
                status, output_dir, started_at
            )
            VALUES (?, ?, 'CN_A', ?, ?, 'RUNNING', ?, ?)
            """,
            (run_id, as_of, models, qlib_version, str(output_dir), started),
        )
        placeholders = ",".join("?" for _ in model_names)
        existing = conn.execute(
            f"""
            SELECT COUNT(*) AS score_count, COUNT(DISTINCT model_name) AS model_count
            FROM model_scores
            WHERE market = 'CN_A' AND score_date = ? AND model_name IN ({placeholders})
            """,
            (as_of, *model_names),
        ).fetchone()
        score_count = int(existing["score_count"] or 0)
        model_count = int(existing["model_count"] or 0)
        if model_count == len(model_names) and score_count > 0:
            pred_row = conn.execute("SELECT id FROM prediction_runs WHERE run_id = ?", (run_id,)).fetchone()
            conn.execute(
                f"""
                UPDATE model_scores
                SET prediction_run_id = ?
                WHERE market = 'CN_A' AND score_date = ? AND model_name IN ({placeholders})
                """,
                (int(pred_row["id"]), as_of, *model_names),
            )
            conn.execute(
                """
                UPDATE prediction_runs
                SET status = 'SUCCESS', model_count = ?, score_count = ?,
                    error_message = '', finished_at = ?
                WHERE run_id = ?
                """,
                (model_count, score_count, now_utc(), run_id),
            )
            conn.commit()
            _write_prediction_report(output_dir, run_id, "SUCCESS", model_count, score_count, "")
            return PredictionResult(run_id, "SUCCESS", as_of, model_count, score_count, str(output_dir))
        conn.commit()

    script = PROJECT_ROOT / "scripts" / "predict_latest.py"
    cmd = [PYTHON_BIN, str(script), "--as-of", as_of, "--db", str(db_path), "--skip-data-update"]
    run = subprocess.run(cmd, cwd=str(PROJECT_ROOT), text=True, capture_output=True)
    (output_dir / "predict_stdout.log").write_text(run.stdout or "", encoding="utf-8")
    (output_dir / "predict_stderr.log").write_text(run.stderr or "", encoding="utf-8")

    with connect(db_path) as conn:
        init_db(conn)
        model_names = _production_model_names(conn)
        placeholders = ",".join("?" for _ in model_names)
        if model_names:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS score_count, COUNT(DISTINCT model_name) AS model_count
                FROM model_scores
                WHERE market = 'CN_A' AND score_date = ? AND model_name IN ({placeholders})
                """,
                (as_of, *model_names),
            ).fetchone()
            score_count = int(row["score_count"] or 0)
            model_count = int(row["model_count"] or 0)
            pred_row = conn.execute("SELECT id FROM prediction_runs WHERE run_id = ?", (run_id,)).fetchone()
            if pred_row:
                conn.execute(
                    f"""
                    UPDATE model_scores
                    SET prediction_run_id = ?
                    WHERE market = 'CN_A' AND score_date = ? AND model_name IN ({placeholders})
                    """,
                    (int(pred_row["id"]), as_of, *model_names),
                )
        else:
            score_count = 0
            model_count = 0
        status = "SUCCESS" if run.returncode == 0 and score_count > 0 else "FAILED"
        error = "" if status == "SUCCESS" else (run.stderr or run.stdout or f"exit {run.returncode}")[-2000:]
        conn.execute(
            """
            UPDATE prediction_runs
            SET status = ?, model_count = ?, score_count = ?, error_message = ?, finished_at = ?
            WHERE run_id = ?
            """,
            (status, model_count, score_count, error, now_utc(), run_id),
        )
        conn.commit()
    _write_prediction_report(output_dir, run_id, status, model_count, score_count, error)
    return PredictionResult(run_id, status, as_of, model_count, score_count, str(output_dir), error)


def _write_prediction_report(
    output_dir: Path,
    run_id: str,
    status: str,
    model_count: int,
    score_count: int,
    error_message: str,
) -> Path:
    lines = [
        "# Model Prediction Run",
        "",
        f"- run_id: `{run_id}`",
        f"- status: `{status}`",
        f"- model_count: `{model_count}`",
        f"- score_count: `{score_count}`",
    ]
    if error_message:
        lines.extend(["", "## Error", "", error_message])
    path = output_dir / "prediction_run.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def production_daily(
    db_path: str,
    as_of: str,
    *,
    allow_inline_data_update: bool = False,
    output_dir: Path | None = None,
    no_overwrite: bool = False,
    prepare_signals: bool = True,
) -> ProductionDailyResult:
    output_dir = output_dir or PRODUCTION_DAILY_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"daily_plan_{as_of}.md"
    if no_overwrite and report_path.exists():
        return ProductionDailyResult("FAILED", str(report_path), f"Report already exists: {report_path}")

    with connect(db_path) as conn:
        init_db(conn)
        if allow_inline_data_update:
            data_update(
                conn,
                as_of,
                "CN_A",
                adjust=None,
                fetch_events=False,
                fetch_intraday=False,
                price_mode="core",
            )
        audit = audit_data_coverage(
            conn,
            as_of,
            as_of,
            "CN_A",
            write=True,
            ignore_adjustment_for_short_term=True,
        )
        qlib_version = _latest_qlib_version_for_as_of(conn, as_of)
        pred_row = conn.execute(
            """
            SELECT *
            FROM prediction_runs
            WHERE as_of_date = ? AND status = 'SUCCESS'
            ORDER BY id DESC
            LIMIT 1
            """,
            (as_of,),
        ).fetchone()
        errors: list[str] = []
        amount_coverage = _amount_coverage_pct(conn, as_of)
        if amount_coverage < 95.0:
            errors.append(f"amount coverage below 95%: {amount_coverage:.1f}%")
        if not qlib_version:
            errors.append(f"No successful qlib_dataset_version covers {as_of}")
        if not pred_row:
            errors.append(f"No successful prediction_run for {as_of}")
        if errors:
            failure_path = output_dir / f"daily_plan_{as_of}_FAILED.md"
            failure_path.write_text(
                "# Production Daily Failed\n\n" + "\n".join(f"- {e}" for e in errors) + "\n",
                encoding="utf-8",
            )
            return ProductionDailyResult("FAILED", str(failure_path), "; ".join(errors))

        if prepare_signals:
            screen_all(conn, as_of)
        path = write_daily_plan(conn, as_of, report_path)
    return ProductionDailyResult("SUCCESS", str(path))


def _write_production_run_summary(
    output_dir: Path,
    *,
    run_id: str,
    as_of: str,
    status: str,
    steps: list[dict[str, str]],
    report_path: str = "",
    failed_step: str = "",
    error_message: str = "",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Production Run Summary",
        "",
        f"- run_id: `{run_id}`",
        f"- as_of: `{as_of}`",
        f"- status: `{status}`",
    ]
    if report_path:
        lines.append(f"- report: `{report_path}`")
    if failed_step:
        lines.append(f"- failed_step: `{failed_step}`")
    if error_message:
        lines.extend(["", "## Error", "", error_message])
    lines.extend(["", "## Steps", "", "| Step | Status | Detail |", "|---|---|---|"])
    for step in steps:
        lines.append(f"| {step.get('step', '')} | {step.get('status', '')} | {step.get('detail', '')} |")
    path = output_dir / "run_summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def production_run(
    db_path: str,
    as_of: str,
    *,
    skip_data_update: bool = False,
    no_overwrite: bool = False,
    output_root: Path | None = None,
) -> ProductionRunResult:
    """Run the single production pipeline entrypoint.

    The function intentionally uses the fast/core data path and stops on first
    failed formal step. It never trains models and does not overwrite existing
    daily reports when no_overwrite is set.
    """
    run_id = _run_id("production", as_of)
    output_dir = (output_root or PRODUCTION_RUNS_DIR) / as_of
    steps: list[dict[str, str]] = []

    def record(step: str, status: str, detail: str = "") -> None:
        steps.append({"step": step, "status": status, "detail": detail.replace("\n", " ")[:1000]})

    def fail(step: str, message: str, report_path: str = "") -> ProductionRunResult:
        record(step, "FAILED", message)
        summary = _write_production_run_summary(
            output_dir,
            run_id=run_id,
            as_of=as_of,
            status="FAILED",
            steps=steps,
            report_path=report_path,
            failed_step=step,
            error_message=message,
        )
        return ProductionRunResult(run_id, "FAILED", as_of, report_path, str(summary), step, message)

    with connect(db_path) as conn:
        init_db(conn)
        if skip_data_update:
            record("data-update", "SKIPPED", "user requested --skip-data-update")
        else:
            try:
                data_result = data_update(
                    conn,
                    as_of,
                    "CN_A",
                    throttle_seconds=0.15,
                    fetch_events=False,
                    fetch_intraday=False,
                    price_mode="core",
                    repair_coverage=True,
                    repair_scope="benchmarks",
                    adjust=None,
                )
            except Exception as exc:
                return fail("data-update", str(exc))
            record(
                "data-update",
                data_result.status,
                f"price_bars={data_result.price_bars}, errors={data_result.error_count}",
            )
            if data_result.status != "SUCCESS":
                return fail(
                    "data-update",
                    f"data-update {data_result.status}: {data_result.error_count} errors",
                )

        try:
            adjustment_breaks = detect_adjustment_breaks(conn, as_of, market="CN_A")
            qfq_repair = qfq_repair_daily(conn, as_of, market="CN_A")
        except Exception as exc:
            record("qfq-repair-daily", "FAILED", str(exc))
        else:
            repair_status = "SUCCESS" if qfq_repair.failed_count == 0 else "PARTIAL_SUCCESS"
            record(
                "qfq-repair-daily",
                repair_status,
                (
                    f"confirmed={adjustment_breaks.confirmed}, suspected={adjustment_breaks.suspected}, "
                    f"targets={qfq_repair.target_count}, repaired={qfq_repair.repaired_count}, "
                    f"failed={qfq_repair.failed_count}, updated_rows={qfq_repair.updated_rows}"
                ),
            )

        try:
            audit = audit_data_coverage(
                conn,
                as_of,
                as_of,
                "CN_A",
                write=True,
                ignore_adjustment_for_short_term=True,
            )
        except Exception as exc:
            return fail("data-audit", str(exc))
        record("data-audit", getattr(audit, "confidence_level", "SUCCESS"), "")

        try:
            qlib_result = qlib_refresh(conn, as_of, mode="incremental")
        except Exception as exc:
            return fail("qlib-refresh", str(exc))
        record(
            "qlib-refresh",
            qlib_result.status,
            f"version={qlib_result.version}, mode={qlib_result.mode}, rows={qlib_result.row_count}",
        )
        if qlib_result.status != "SUCCESS":
            return fail("qlib-refresh", qlib_result.error_message or qlib_result.status)

    pred_result = model_predict(db_path, as_of, models="production")
    record(
        "model-predict",
        pred_result.status,
        f"run_id={pred_result.run_id}, models={pred_result.model_count}, scores={pred_result.score_count}",
    )
    if pred_result.status != "SUCCESS":
        return fail("model-predict", pred_result.error_message or pred_result.status)

    with connect(db_path) as conn:
        init_db(conn)
        try:
            candidate_count = screen_all(conn, as_of)
            intraday_result = data_update(
                conn,
                as_of,
                "CN_A",
                throttle_seconds=0.15,
                fetch_events=False,
                fetch_intraday=True,
                intraday_period="1",
                price_mode="none",
                adjust=None,
            )
            refined_count = refine_candidates_with_intraday(conn, as_of)
        except Exception as exc:
            return fail("signal-intraday-context", str(exc))
        record(
            "signal-intraday-context",
            intraday_result.status,
            (
                f"intraday_period=1m, candidates={candidate_count}, "
                f"intraday_bars={getattr(intraday_result, 'intraday_bars', 0)}, "
                f"refined={refined_count}, errors={getattr(intraday_result, 'error_count', 0)}"
            ),
        )

    daily_result = production_daily(
        db_path,
        as_of,
        allow_inline_data_update=False,
        no_overwrite=no_overwrite,
        prepare_signals=False,
    )
    record("production-daily", daily_result.status, daily_result.report_path)
    if daily_result.status != "SUCCESS":
        return fail("production-daily", daily_result.error_message or daily_result.status, daily_result.report_path)

    summary = _write_production_run_summary(
        output_dir,
        run_id=run_id,
        as_of=as_of,
        status="SUCCESS",
        steps=steps,
        report_path=daily_result.report_path,
    )
    return ProductionRunResult(run_id, "SUCCESS", as_of, daily_result.report_path, str(summary))


def production_async(
    db_path: str,
    as_of: str,
    *,
    output_dir: Path | None = None,
) -> ProductionAsyncResult:
    """Run slow production-adjacent tasks without blocking the formal daily report."""
    output_dir = output_dir or PRODUCTION_ASYNC_DIR / as_of
    output_dir.mkdir(parents=True, exist_ok=True)
    tasks: list[dict[str, str]] = []

    def record(task: str, status: str, detail: str = "") -> None:
        tasks.append({"task": task, "status": status, "detail": detail.replace("\n", " ")[:1000]})

    with connect(db_path) as conn:
        init_db(conn)
        try:
            result = data_update(
                conn,
                as_of,
                "CN_A",
                throttle_seconds=0.15,
                fetch_events=True,
                fetch_intraday=False,
                price_mode="essential",
                adjust=None,
            )
            record(
                "events-financials-flows",
                result.status,
                (
                    f"events={result.corporate_events}, financials={result.financial_metrics}, "
                    f"money_flows={result.money_flows}, "
                    f"errors={result.error_count}"
                ),
            )
        except Exception as exc:
            record("events-financials-flows", "FAILED", str(exc))

    failed = sum(1 for task in tasks if task["status"] == "FAILED")
    partial = sum(1 for task in tasks if task["status"] == "PARTIAL_SUCCESS")
    status = "SUCCESS" if failed == 0 and partial == 0 else ("PARTIAL_SUCCESS" if failed < len(tasks) else "FAILED")
    lines = [
        "# Production Async Summary",
        "",
        f"- as_of: `{as_of}`",
        f"- status: `{status}`",
        "- note: async task failures do not mutate or invalidate the formal production daily report.",
        "",
        "| Task | Status | Detail |",
        "|---|---|---|",
    ]
    for task in tasks:
        lines.append(f"| {task['task']} | {task['status']} | {task['detail']} |")
    path = output_dir / "async_summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ProductionAsyncResult(status, str(path), len(tasks), failed)


def _production_model_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM model_registry
        WHERE status = 'PRODUCTION'
        ORDER BY id
        """
    ).fetchall()


def _latest_price_date(conn: sqlite3.Connection, market: str = "CN_A") -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM price_bars WHERE market = ?", (market,)).fetchone()
    return str(row["d"]) if row and row["d"] else None


def _mature_eval_end(conn: sqlite3.Connection, as_of: str, horizon: int) -> str | None:
    rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM price_bars
        WHERE market = 'CN_A' AND date <= ?
        ORDER BY date
        """,
        (as_of,),
    ).fetchall()
    dates = [str(row["date"]) for row in rows]
    if len(dates) <= horizon:
        return None
    return dates[-(horizon + 1)]


def _model_score_return_metrics(
    conn: sqlite3.Connection,
    model_name: str,
    model_version: str,
    horizon: int,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    max_score_dates: int | None = None,
) -> dict[str, Any]:
    where = [
        "ms.market = 'CN_A'",
        "ms.model_name = ?",
        "ms.model_version = ?",
        "ms.percentile IS NOT NULL",
    ]
    params: list[Any] = [model_name, model_version]
    if start_date:
        where.append("ms.score_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("ms.score_date <= ?")
        params.append(end_date)
    rows = conn.execute(
        f"""
        SELECT ms.score_date, ms.ticker, ms.score, ms.percentile
        FROM model_scores ms
        WHERE {' AND '.join(where)}
        ORDER BY ms.score_date, ms.percentile DESC
        """,
        params,
    ).fetchall()
    if not rows:
        return {
            "sample_count": 0,
            "score_date_count": 0,
            "ic": None,
            "rank_ic": None,
            "top_return": None,
            "bottom_return": None,
            "top_bottom_spread": None,
            "top_win_rate": None,
            "avg_return": None,
        }

    dates = _trading_dates(
        conn,
        str(rows[0]["score_date"]),
        (date.fromisoformat(str(rows[-1]["score_date"])) + timedelta(days=max(30, horizon + 10))).isoformat(),
    )
    date_to_idx = {d: idx for idx, d in enumerate(dates)}
    by_date: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_date.setdefault(str(row["score_date"]), []).append(row)
    score_dates = sorted(by_date)
    if max_score_dates is not None and len(score_dates) > max_score_dates:
        score_dates = score_dates[-max_score_dates:]

    all_scores: list[float] = []
    all_returns: list[float] = []
    top_returns: list[float] = []
    bottom_returns: list[float] = []
    for score_date in score_dates:
        idx = date_to_idx.get(score_date)
        if idx is None or idx + horizon >= len(dates) or idx + 1 >= len(dates):
            continue
        entry_date = dates[idx + 1]
        exit_date = dates[idx + horizon]
        scored_rows = sorted(by_date[score_date], key=lambda r: float(r["percentile"]), reverse=True)
        with_returns: list[tuple[sqlite3.Row, float]] = []
        for row in scored_rows:
            ret = _price_pair_return(conn, str(row["ticker"]), entry_date, exit_date)
            if ret is None:
                continue
            with_returns.append((row, ret))
            all_scores.append(float(row["percentile"]))
            all_returns.append(ret)
        if not with_returns:
            continue
        n = max(1, int(len(with_returns) * 0.1))
        top_returns.extend(ret for _row, ret in with_returns[:n])
        bottom_returns.extend(ret for _row, ret in with_returns[-n:])

    if not all_returns:
        return {
            "sample_count": 0,
            "score_date_count": len(score_dates),
            "ic": None,
            "rank_ic": None,
            "top_return": None,
            "bottom_return": None,
            "top_bottom_spread": None,
            "top_win_rate": None,
            "avg_return": None,
        }
    score_series = pd.Series(all_scores)
    return_series = pd.Series(all_returns)
    ic = float(score_series.corr(return_series)) if len(all_returns) > 1 else None
    rank_ic = float(score_series.rank().corr(return_series.rank())) if len(all_returns) > 1 else None
    top_avg = sum(top_returns) / len(top_returns) if top_returns else None
    bottom_avg = sum(bottom_returns) / len(bottom_returns) if bottom_returns else None
    return {
        "sample_count": len(all_returns),
        "score_date_count": len(score_dates),
        "ic": ic,
        "rank_ic": rank_ic,
        "top_return": top_avg,
        "bottom_return": bottom_avg,
        "top_bottom_spread": top_avg - bottom_avg if top_avg is not None and bottom_avg is not None else None,
        "top_win_rate": sum(1 for ret in top_returns if ret > 0) / len(top_returns) if top_returns else None,
        "avg_return": sum(all_returns) / len(all_returns),
    }


def _model_eval_conclusion(metrics: dict[str, Any], window_metrics: list[dict[str, Any]]) -> str:
    sample_count = int(metrics.get("sample_count") or 0)
    if sample_count < 200 or len(window_metrics) < 1:
        return "INSUFFICIENT_HISTORY"
    valid_windows = [m for m in window_metrics if int(m.get("sample_count") or 0) >= 30]
    if not valid_windows:
        return "INSUFFICIENT_HISTORY"
    positive = 0
    negative = 0
    for item in valid_windows:
        rank_ic = item.get("rank_ic")
        spread = item.get("top_bottom_spread")
        if rank_ic is not None and spread is not None and float(rank_ic) > 0 and float(spread) > 0:
            positive += 1
        elif rank_ic is not None and spread is not None and float(rank_ic) < 0 and float(spread) < 0:
            negative += 1
    if positive >= max(1, int(len(valid_windows) * 0.6)):
        return "PASS"
    if negative >= max(1, int(len(valid_windows) * 0.6)):
        return "FAIL"
    return "WATCH"


def _rolling_score_windows(
    conn: sqlite3.Connection,
    *,
    start_date: str | None,
    mature_end: str | None,
    window_days: int = 60,
) -> list[tuple[str, str]]:
    if not mature_end:
        return []
    start = start_date or _price_min_date(conn)
    dates = _trading_dates(conn, start, mature_end)
    if not dates:
        return []
    windows: list[tuple[str, str]] = []
    for offset in range(0, len(dates), window_days):
        chunk = dates[offset : offset + window_days]
        if chunk:
            windows.append((chunk[0], chunk[-1]))
    return windows


def model_validate(
    db_path: str,
    as_of: str,
    *,
    models: str = "production",
    mode: str = "lite-walk-forward",
    output_dir: Path | None = None,
) -> ModelValidationResult:
    if models != "production":
        raise ValueError("Only models=production is supported")
    if mode != "lite-walk-forward":
        raise ValueError("Only mode=lite-walk-forward is supported")
    output_dir = output_dir or MODEL_VALIDATION_DIR / as_of
    output_dir.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        init_db(conn)
        model_rows = _production_model_rows(conn)
        lines = [
            "# Model Validation",
            "",
            f"- as_of: `{as_of}`",
            f"- mode: `{mode}`",
            "- note: only mature label dates are evaluated; latest predictions without labels are excluded.",
            "",
            "## Mature Test Summary",
            "",
            "| Model | Label | Mature Eval End | Samples | Score Dates | Rank IC | IC | Top Ret | Bottom Ret | Spread | Top Win | Conclusion |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        window_sections: list[str] = []
        for row in model_rows:
            horizon = int(row["horizon_days"] or 2)
            mature_end = _mature_eval_end(conn, as_of, horizon)
            test_start = str(row["test_start"]) if row["test_start"] else None
            metrics = (
                _model_score_return_metrics(
                    conn,
                    str(row["model_name"]),
                    str(row["model_version"]),
                    horizon,
                    start_date=test_start,
                    end_date=mature_end,
                )
                if mature_end
                else {"sample_count": 0, "score_date_count": 0}
            )
            windows = _rolling_score_windows(conn, start_date=test_start, mature_end=mature_end)
            window_metrics: list[dict[str, Any]] = []
            for idx, (window_start, window_end) in enumerate(windows, start=1):
                wm = _model_score_return_metrics(
                    conn,
                    str(row["model_name"]),
                    str(row["model_version"]),
                    horizon,
                    start_date=window_start,
                    end_date=window_end,
                )
                wm["window_id"] = idx
                wm["start_date"] = window_start
                wm["end_date"] = window_end
                window_metrics.append(wm)
            conclusion = _model_eval_conclusion(metrics, window_metrics)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{row['model_name']}@{row['model_version']}",
                        str(row["label_name"]),
                        mature_end or "-",
                        str(metrics.get("sample_count", 0)),
                        str(metrics.get("score_date_count", 0)),
                        _fmt_metric(metrics.get("rank_ic")),
                        _fmt_metric(metrics.get("ic")),
                        _fmt_metric(metrics.get("top_return")),
                        _fmt_metric(metrics.get("bottom_return")),
                        _fmt_metric(metrics.get("top_bottom_spread")),
                        _fmt_metric(metrics.get("top_win_rate")),
                        conclusion,
                    ]
                )
                + " |"
            )
            section = [
                "",
                f"## Rolling Windows - {row['model_name']}@{row['model_version']}",
                "",
                f"- label: `{row['label_name']}`",
                f"- horizon_days: `{horizon}`",
                f"- mature_eval_end: `{mature_end or '-'}`",
                f"- conclusion: `{conclusion}`",
                "",
                "| Window | Start | End | Samples | Score Dates | Rank IC | IC | Top Ret | Bottom Ret | Spread | Top Win |",
                "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for wm in window_metrics:
                section.append(
                    "| "
                    + " | ".join(
                        [
                            str(wm["window_id"]),
                            str(wm["start_date"]),
                            str(wm["end_date"]),
                            str(wm.get("sample_count", 0)),
                            str(wm.get("score_date_count", 0)),
                            _fmt_metric(wm.get("rank_ic")),
                            _fmt_metric(wm.get("ic")),
                            _fmt_metric(wm.get("top_return")),
                            _fmt_metric(wm.get("bottom_return")),
                            _fmt_metric(wm.get("top_bottom_spread")),
                            _fmt_metric(wm.get("top_win_rate")),
                        ]
                    )
                    + " |"
                )
            window_sections.extend(section)
        lines.extend(window_sections)
        path = output_dir / "summary.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ModelValidationResult("SUCCESS", str(path), len(model_rows))


def model_governance_review(
    db_path: str,
    as_of: str,
    *,
    output_dir: Path | None = None,
) -> ModelGovernanceResult:
    output_dir = output_dir or MODEL_GOVERNANCE_DIR / as_of
    output_dir.mkdir(parents=True, exist_ok=True)
    needs_review = 0
    with connect(db_path) as conn:
        init_db(conn)
        model_rows = _production_model_rows(conn)
        lines = [
            "# Model Governance Review",
            "",
            f"- as_of: `{as_of}`",
            "- rule: recent negative top-bucket return or insufficient recent samples triggers manual review only.",
            "- action: no automatic downweight, pause, retire, or registry mutation.",
            "",
            "| Model | Label | Recent Samples | Recent Top Ret | Baseline Top Ret | Status |",
            "|---|---|---:|---:|---:|---|",
        ]
        for row in model_rows:
            horizon = int(row["horizon_days"] or 2)
            mature_end = _mature_eval_end(conn, as_of, horizon)
            recent = (
                _model_score_return_metrics(
                    conn,
                    str(row["model_name"]),
                    str(row["model_version"]),
                    horizon,
                    end_date=mature_end,
                    max_score_dates=20,
                )
                if mature_end
                else {"sample_count": 0, "top_return": None}
            )
            try:
                baseline = json.loads(str(row["metrics_json"] or "{}")).get("top_return")
            except Exception:
                baseline = None
            sample_count = int(recent.get("sample_count") or 0)
            recent_top = recent.get("top_return")
            status = "OK"
            if sample_count < 50:
                status = "INSUFFICIENT_RECENT_SAMPLE"
            elif recent_top is not None and float(recent_top) < 0:
                status = "NEEDS_REVIEW"
                needs_review += 1
            elif baseline is not None and recent_top is not None and float(recent_top) < float(baseline) * 0.5:
                status = "NEEDS_REVIEW"
                needs_review += 1
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{row['model_name']}@{row['model_version']}",
                        str(row["label_name"]),
                        str(sample_count),
                        _fmt_metric(recent_top),
                        _fmt_metric(baseline),
                        status,
                    ]
                )
                + " |"
            )
        path = output_dir / "review.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ModelGovernanceResult("SUCCESS", str(path), len(model_rows), needs_review)


def _safe_mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _safe_std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _safe_corr(x_values: list[float], y_values: list[float], rank: bool = False) -> float | None:
    if len(x_values) < 2 or len(y_values) < 2:
        return None
    x = pd.Series(x_values)
    y = pd.Series(y_values)
    if rank:
        x = x.rank()
        y = y.rank()
    value = x.corr(y)
    if value is None or math.isnan(float(value)):
        return None
    return float(value)


def _max_drawdown_from_returns(daily_returns: list[float]) -> float | None:
    if not daily_returns:
        return None
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in daily_returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, peak / equity - 1.0)
    return max_dd


def _max_consecutive_losses(daily_returns: list[float]) -> int:
    current = 0
    best = 0
    for ret in daily_returns:
        if ret < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _prediction_frame_from_artifact(row: sqlite3.Row) -> pd.DataFrame:
    artifact_path = Path(str(row["artifact_path"] or ""))
    if not artifact_path.exists():
        raise FileNotFoundError(f"prediction artifact not found: {artifact_path}")
    pred = pd.read_pickle(artifact_path)
    if isinstance(pred, pd.Series):
        pred = pred.to_frame("score")
    if not isinstance(pred, pd.DataFrame) or pred.empty or not isinstance(pred.index, pd.MultiIndex):
        raise ValueError(f"unsupported prediction artifact shape: {artifact_path}")
    df = pred.reset_index()
    if "datetime" not in df.columns or "instrument" not in df.columns:
        raise ValueError(f"prediction artifact missing datetime/instrument index: {artifact_path}")
    score_col = "score" if "score" in df.columns else str(df.columns[-1])
    out = pd.DataFrame(
        {
            "model_name": str(row["model_name"]),
            "model_version": str(row["model_version"]),
            "score_date": pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d"),
            "ticker": df["instrument"].map(lambda value: qlib_instrument_to_ticker(str(value))),
            "score": pd.to_numeric(df[score_col], errors="coerce"),
            "horizon_days": int(row["horizon_days"]),
        }
    )
    out = out[out["ticker"].notna() & out["score"].notna()].copy()
    if out.empty:
        raise ValueError(f"prediction artifact has no valid CN_A scores: {artifact_path}")
    out["rank"] = out.groupby("score_date")["score"].rank(method="first", ascending=False)
    counts = out.groupby("score_date")["score"].transform("count")
    out["percentile"] = (counts - out["rank"] + 1) / counts * 100.0
    return out


def _segment_dates(mature_dates: list[str]) -> tuple[list[str], list[str], str]:
    if len(mature_dates) < 2:
        return mature_dates, [], "INSUFFICIENT_SAMPLE"
    if len(mature_dates) >= 25:
        external = mature_dates[-20:]
        internal = mature_dates[:-20]
        return internal, external, "SAME_AS_TRAINING_TEST"
    external = mature_dates[-5:]
    internal = mature_dates[:-5]
    return internal, external, "SAME_AS_TRAINING_TEST_SHORT_EXTERNAL"


def _date_context(conn: sqlite3.Connection, score_date: str, horizon: int) -> tuple[str, str] | None:
    dates = _trading_dates(
        conn,
        score_date,
        (date.fromisoformat(score_date) + timedelta(days=max(30, horizon + 10))).isoformat(),
    )
    if not dates or dates[0] != score_date or len(dates) <= horizon:
        return None
    return dates[1], dates[horizon]


def _industry_for_ticker(conn: sqlite3.Connection, ticker: str) -> str:
    row = conn.execute(
        "SELECT industry_sw_l1 FROM instruments WHERE market='CN_A' AND ticker=?",
        (ticker,),
    ).fetchone()
    if row is None or not row["industry_sw_l1"]:
        return "UNKNOWN"
    return str(row["industry_sw_l1"])


def _evaluate_prediction_segment(
    conn: sqlite3.Connection,
    frame: pd.DataFrame,
    *,
    horizon: int,
    segment_dates: list[str],
) -> dict[str, dict[str, Any]]:
    buckets = ("TOP_1", "TOP_5", "TOP_10", "BOTTOM_10")
    bucket_trade_returns: dict[str, list[float]] = {bucket: [] for bucket in buckets}
    bucket_trade_excess: dict[str, list[float]] = {bucket: [] for bucket in buckets}
    bucket_daily_net: dict[str, list[float]] = {bucket: [] for bucket in buckets}
    bucket_daily_tickers: dict[str, list[set[str]]] = {bucket: [] for bucket in buckets}
    bucket_industries: dict[str, Counter[str]] = {bucket: Counter() for bucket in buckets}
    bucket_missing_benchmark: dict[str, int] = {bucket: 0 for bucket in buckets}
    all_scores: list[float] = []
    all_returns: list[float] = []
    daily_ics: list[float] = []
    daily_rank_ics: list[float] = []
    missing_price = 0
    prediction_count = 0
    valid_score_dates = 0
    cost_dec = trade_cost_pct("CN_A") / 100.0
    segment_set = set(segment_dates)
    date_contexts = {
        score_date: context
        for score_date in segment_dates
        if (context := _date_context(conn, score_date, horizon)) is not None
    }
    needed_dates = sorted({value for context in date_contexts.values() for value in context})
    price_lookup: dict[tuple[str, str], sqlite3.Row] = {}
    if needed_dates:
        placeholders = ",".join("?" for _ in needed_dates)
        rows = conn.execute(
            f"""
            SELECT *
            FROM price_bars
            WHERE market='CN_A' AND date IN ({placeholders})
            """,
            needed_dates,
        ).fetchall()
        price_lookup = {(str(row["ticker"]), str(row["date"])): row for row in rows}
    benchmark_cache: dict[tuple[str, str, str], float | None] = {}

    def _adjusted_from_row(bar: sqlite3.Row, raw_key: str) -> float | None:
        raw = bar[raw_key]
        if raw is None:
            return None
        close = bar["close"]
        adj_close = bar["adj_close"] if "adj_close" in bar.keys() and bar["adj_close"] is not None else close
        if close is None or float(close) <= 0 or adj_close is None:
            return float(raw)
        return float(raw) * (float(adj_close) / float(close))

    def _cached_return(ticker: str, entry_date: str, exit_date: str) -> float | None:
        entry = price_lookup.get((ticker, entry_date))
        exit_row = price_lookup.get((ticker, exit_date))
        if entry is None or exit_row is None:
            return None
        entry_price = _adjusted_from_row(entry, "open")
        exit_price = _adjusted_from_row(exit_row, "close")
        if entry_price is None or entry_price == 0 or exit_price is None:
            return None
        return exit_price / entry_price - 1.0

    def _cached_benchmark_return(ticker: str, entry_date: str, exit_date: str) -> float | None:
        benchmark_ticker = benchmark_for_asset("CN_A", ticker, "auto")
        if not benchmark_ticker:
            return None
        key = (benchmark_ticker, entry_date, exit_date)
        if key in benchmark_cache:
            return benchmark_cache[key]
        value = _cached_return(benchmark_ticker, entry_date, exit_date)
        benchmark_cache[key] = value
        return value

    for score_date, group in frame.groupby("score_date"):
        score_date = str(score_date)
        if score_date not in segment_set:
            continue
        context = date_contexts.get(score_date)
        if context is None:
            continue
        entry_date, exit_date = context
        sorted_group = group.sort_values("score", ascending=False)
        prediction_count += len(sorted_group)
        rows_with_returns: list[dict[str, Any]] = []
        for item in sorted_group.itertuples(index=False):
            ticker = str(item.ticker)
            ret = _cached_return(ticker, entry_date, exit_date)
            if ret is None:
                missing_price += 1
                continue
            benchmark = _cached_benchmark_return(ticker, entry_date, exit_date)
            rows_with_returns.append(
                {
                    "ticker": ticker,
                    "score": float(item.score),
                    "return": ret,
                    "benchmark": benchmark,
                    "excess": ret - benchmark if benchmark is not None else None,
                }
            )
        if not rows_with_returns:
            continue
        valid_score_dates += 1
        scores = [float(item["score"]) for item in rows_with_returns]
        returns = [float(item["return"]) for item in rows_with_returns]
        all_scores.extend(scores)
        all_returns.extend(returns)
        ic = _safe_corr(scores, returns)
        rank_ic = _safe_corr(scores, returns, rank=True)
        if ic is not None:
            daily_ics.append(ic)
        if rank_ic is not None:
            daily_rank_ics.append(rank_ic)

        n = len(rows_with_returns)
        selections = {
            "TOP_1": rows_with_returns[: max(1, int(n * 0.01))],
            "TOP_5": rows_with_returns[: max(1, int(n * 0.05))],
            "TOP_10": rows_with_returns[: max(1, int(n * 0.10))],
            "BOTTOM_10": rows_with_returns[-max(1, int(n * 0.10)) :],
        }
        for bucket, selected in selections.items():
            trade_returns = [float(item["return"]) for item in selected]
            excess_values = [float(item["excess"]) for item in selected if item["excess"] is not None]
            bucket_trade_returns[bucket].extend(trade_returns)
            bucket_trade_excess[bucket].extend(excess_values)
            bucket_missing_benchmark[bucket] += len(selected) - len(excess_values)
            daily_net = sum(ret - cost_dec for ret in trade_returns) / len(trade_returns)
            bucket_daily_net[bucket].append(daily_net)
            tickers = {str(item["ticker"]) for item in selected}
            bucket_daily_tickers[bucket].append(tickers)
            for ticker in tickers:
                bucket_industries[bucket][_industry_for_ticker(conn, ticker)] += 1

    overall_ic = _safe_mean(daily_ics)
    overall_rank_ic = _safe_mean(daily_rank_ics)
    top10_avg = _safe_mean(bucket_trade_returns["TOP_10"])
    bottom10_avg = _safe_mean(bucket_trade_returns["BOTTOM_10"])
    results: dict[str, dict[str, Any]] = {
        "OVERALL": {
            "sample_count": len(all_returns),
            "score_date_count": valid_score_dates,
            "prediction_count": prediction_count,
            "coverage": len(all_returns) / prediction_count if prediction_count else 0.0,
            "ic": overall_ic,
            "rank_ic": overall_rank_ic,
            "icir": (overall_ic / _safe_std(daily_ics)) if overall_ic is not None and _safe_std(daily_ics) else None,
            "rank_icir": (
                overall_rank_ic / _safe_std(daily_rank_ics)
                if overall_rank_ic is not None and _safe_std(daily_rank_ics)
                else None
            ),
            "avg_return": _safe_mean(all_returns),
            "top_10_return": top10_avg,
            "bottom_10_return": bottom10_avg,
            "top_bottom_spread": top10_avg - bottom10_avg if top10_avg is not None and bottom10_avg is not None else None,
            "missing_price_count": missing_price,
            "missing_benchmark_count": sum(bucket_missing_benchmark.values()),
            "industry_exposure": {},
        }
    }
    for bucket in buckets:
        trade_returns = bucket_trade_returns[bucket]
        excess_returns = bucket_trade_excess[bucket]
        daily_net = bucket_daily_net[bucket]
        ticker_sets = bucket_daily_tickers[bucket]
        turnover_values: list[float] = []
        previous: set[str] | None = None
        for current in ticker_sets:
            if previous is not None and previous:
                turnover_values.append(1.0 - len(previous & current) / len(previous))
            previous = current
        industry_total = sum(bucket_industries[bucket].values())
        exposure = {
            key: value / industry_total
            for key, value in bucket_industries[bucket].most_common(12)
        } if industry_total else {}
        results[bucket] = {
            "sample_count": len(trade_returns),
            "score_date_count": len(daily_net),
            "prediction_count": prediction_count,
            "coverage": len(trade_returns) / prediction_count if prediction_count else 0.0,
            "avg_return": _safe_mean(trade_returns),
            "win_rate": sum(1 for ret in trade_returns if ret > 0) / len(trade_returns) if trade_returns else None,
            "avg_excess_return": _safe_mean(excess_returns),
            "excess_win_rate": (
                sum(1 for ret in excess_returns if ret > 0) / len(excess_returns)
                if excess_returns
                else None
            ),
            "net_return": _safe_mean(daily_net),
            "max_drawdown": _max_drawdown_from_returns(daily_net),
            "worst_daily_return": min(daily_net) if daily_net else None,
            "volatility": _safe_std(daily_net),
            "consecutive_loss_days": _max_consecutive_losses(daily_net),
            "turnover": _safe_mean(turnover_values),
            "missing_price_count": missing_price,
            "missing_benchmark_count": bucket_missing_benchmark[bucket],
            "industry_exposure": exposure,
        }
    return results


def _validation_conclusion(internal: dict[str, dict[str, Any]], external: dict[str, dict[str, Any]]) -> tuple[str, str]:
    overall = internal.get("OVERALL", {})
    top1 = internal.get("TOP_1", {})
    top5 = internal.get("TOP_5", {})
    if int(overall.get("sample_count") or 0) < 50 or int(overall.get("score_date_count") or 0) < 2:
        return "INSUFFICIENT_SAMPLE", "内部测试段样本不足。"
    rank_ic = overall.get("rank_ic")
    spread = overall.get("top_bottom_spread")
    top1_net = top1.get("net_return")
    top5_net = top5.get("net_return")
    fail_reasons: list[str] = []
    if rank_ic is None or float(rank_ic) <= 0:
        fail_reasons.append("内部测试段 Rank IC <= 0")
    if top1_net is None or float(top1_net) <= 0:
        fail_reasons.append("前1%成本后收益 <= 0")
    if top5_net is None or float(top5_net) <= 0:
        fail_reasons.append("前5%成本后收益 <= 0")
    if spread is None or float(spread) <= 0:
        fail_reasons.append("前10%减后10%的多空差 <= 0")
    if fail_reasons:
        return "FAIL", "；".join(fail_reasons)

    watch_reasons: list[str] = []
    ext_overall = external.get("OVERALL", {})
    ext_top1 = external.get("TOP_1", {})
    ext_top5 = external.get("TOP_5", {})
    if int(ext_overall.get("sample_count") or 0) < 50:
        watch_reasons.append("外部验证段样本偏少")
    if ext_overall.get("rank_ic") is not None and float(ext_overall["rank_ic"]) < 0:
        watch_reasons.append("外部验证段 Rank IC 转负")
    if ext_top1.get("net_return") is not None and float(ext_top1["net_return"]) < 0:
        watch_reasons.append("外部验证段前1%成本后收益转负")
    if ext_top5.get("net_return") is not None and float(ext_top5["net_return"]) < 0:
        watch_reasons.append("外部验证段前5%成本后收益转负")
    if (top1.get("max_drawdown") is not None and float(top1["max_drawdown"]) > 0.08) or (
        top5.get("max_drawdown") is not None and float(top5["max_drawdown"]) > 0.08
    ):
        watch_reasons.append("前1% / 前5%最大回撤偏大")
    if int(top1.get("consecutive_loss_days") or 0) > 5 or int(top5.get("consecutive_loss_days") or 0) > 5:
        watch_reasons.append("连续亏损天数偏长")
    if watch_reasons:
        return "WATCH", "；".join(watch_reasons)
    return "PASS", "内部测试段和外部验证段均未明显失真。"


def _metric_value(metrics: dict[str, Any], key: str) -> Any:
    value = metrics.get(key)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _serialize_metrics(metrics: dict[str, Any]) -> str:
    return json.dumps(metrics, ensure_ascii=False, sort_keys=True, default=str)


def _write_validation_metric_rows(
    conn: sqlite3.Connection,
    run_id: str,
    model_name: str,
    model_version: str,
    segment: str,
    status: str,
    independence_level: str,
    metrics_by_bucket: dict[str, dict[str, Any]],
) -> None:
    now = now_utc()
    for bucket, metrics in metrics_by_bucket.items():
        conn.execute(
            """
            INSERT INTO model_validation_metrics (
                run_id, model_name, model_version, segment, bucket, status,
                independence_level, sample_count, score_date_count, coverage,
                ic, rank_ic, icir, rank_icir, avg_return, win_rate,
                avg_excess_return, excess_win_rate, net_return, max_drawdown,
                worst_daily_return, volatility, consecutive_loss_days, turnover,
                top_bottom_spread, missing_price_count, missing_benchmark_count,
                industry_exposure_json, metrics_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, model_name, model_version, segment, bucket) DO UPDATE SET
                status = excluded.status,
                independence_level = excluded.independence_level,
                sample_count = excluded.sample_count,
                score_date_count = excluded.score_date_count,
                coverage = excluded.coverage,
                ic = excluded.ic,
                rank_ic = excluded.rank_ic,
                icir = excluded.icir,
                rank_icir = excluded.rank_icir,
                avg_return = excluded.avg_return,
                win_rate = excluded.win_rate,
                avg_excess_return = excluded.avg_excess_return,
                excess_win_rate = excluded.excess_win_rate,
                net_return = excluded.net_return,
                max_drawdown = excluded.max_drawdown,
                worst_daily_return = excluded.worst_daily_return,
                volatility = excluded.volatility,
                consecutive_loss_days = excluded.consecutive_loss_days,
                turnover = excluded.turnover,
                top_bottom_spread = excluded.top_bottom_spread,
                missing_price_count = excluded.missing_price_count,
                missing_benchmark_count = excluded.missing_benchmark_count,
                industry_exposure_json = excluded.industry_exposure_json,
                metrics_json = excluded.metrics_json
            """,
            (
                run_id,
                model_name,
                model_version,
                segment,
                bucket,
                status,
                independence_level,
                int(metrics.get("sample_count") or 0),
                int(metrics.get("score_date_count") or 0),
                _metric_value(metrics, "coverage"),
                _metric_value(metrics, "ic"),
                _metric_value(metrics, "rank_ic"),
                _metric_value(metrics, "icir"),
                _metric_value(metrics, "rank_icir"),
                _metric_value(metrics, "avg_return"),
                _metric_value(metrics, "win_rate"),
                _metric_value(metrics, "avg_excess_return"),
                _metric_value(metrics, "excess_win_rate"),
                _metric_value(metrics, "net_return"),
                _metric_value(metrics, "max_drawdown"),
                _metric_value(metrics, "worst_daily_return"),
                _metric_value(metrics, "volatility"),
                metrics.get("consecutive_loss_days"),
                _metric_value(metrics, "turnover"),
                _metric_value(metrics, "top_bottom_spread"),
                int(metrics.get("missing_price_count") or 0),
                int(metrics.get("missing_benchmark_count") or 0),
                json.dumps(metrics.get("industry_exposure") or {}, ensure_ascii=False, sort_keys=True),
                _serialize_metrics(metrics),
                now,
            ),
        )


def _update_registry_validation_summary(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    model_version: str,
    run_id: str,
    status: str,
    report_path: str,
    internal: dict[str, dict[str, Any]],
    external: dict[str, dict[str, Any]],
) -> None:
    row = conn.execute(
        "SELECT metrics_json FROM model_registry WHERE model_name=? AND model_version=?",
        (model_name, model_version),
    ).fetchone()
    try:
        payload = json.loads(str(row["metrics_json"] or "{}")) if row else {}
    except Exception:
        payload = {}
    payload["latest_fixed_test_validation"] = {
        "run_id": run_id,
        "status": status,
        "report_path": report_path,
        "internal_rank_ic": internal.get("OVERALL", {}).get("rank_ic"),
        "external_rank_ic": external.get("OVERALL", {}).get("rank_ic"),
        "top1_net_return": internal.get("TOP_1", {}).get("net_return"),
        "top5_net_return": internal.get("TOP_5", {}).get("net_return"),
        "validated_at": now_utc(),
    }
    conn.execute(
        """
        UPDATE model_registry
        SET metrics_json = ?, updated_at = ?
        WHERE model_name = ? AND model_version = ?
        """,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), now_utc(), model_name, model_version),
    )


def _format_industry_exposure(exposure: dict[str, float]) -> str:
    if not exposure:
        return "-"
    return "；".join(f"{name}:{value:.1%}" for name, value in list(exposure.items())[:5])


def _segment_label(segment_name: str) -> str:
    return {
        "internal": "内部测试段",
        "external": "外部验证段",
        "internal_test": "内部测试段",
        "external_validation": "外部验证段",
    }.get(segment_name, segment_name)


def _bucket_label(bucket: str) -> str:
    return {
        "TOP_1": "前1%",
        "TOP_5": "前5%",
        "TOP_10": "前10%",
        "BOTTOM_10": "后10%",
        "OVERALL": "全样本",
    }.get(bucket, bucket)


def _write_model_validation_report(
    output_dir: Path,
    run_id: str,
    as_of: str,
    model_results: list[dict[str, Any]],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    models_dir = output_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    ranked = sorted(
        model_results,
        key=lambda item: (
            {"PASS": 3, "WATCH": 2, "FAIL": 1, "INSUFFICIENT_SAMPLE": 0}.get(str(item["status"]), 0),
            item["internal"].get("OVERALL", {}).get("rank_ic") or -999.0,
            item["internal"].get("TOP_5", {}).get("net_return") or -999.0,
        ),
        reverse=True,
    )
    counts = Counter(str(item["status"]) for item in model_results)
    recommended = [item for item in ranked if item["status"] in {"PASS", "WATCH"}]
    lines = [
        "# 常规模型验证报告",
        "",
        f"- run_id: `{run_id}`",
        f"- 数据截止日: `{as_of}`",
        f"- 验证模型数: `{len(model_results)}`",
        f"- 通过/观察/失败/样本不足: `{counts.get('PASS', 0)}/{counts.get('WATCH', 0)}/{counts.get('FAIL', 0)}/{counts.get('INSUFFICIENT_SAMPLE', 0)}`",
        f"- 建议进入 Candidate 观察的模型: `{recommended[0]['model_name'] if recommended else '-'}`",
        "",
        "## 指标说明",
        "",
        "- `内部测试段`: 使用模型训练任务已产出的 test prediction artifact 中较早、且 label 已成熟的预测日期；它不是完全独立样本，主要用于统一回测模型能否赚钱。",
        "- `外部验证段`: 使用最近一段 label 已成熟的预测日期，检验模型在更靠近当前市场的短窗口里有没有明显衰退。",
        "- `IC`: 模型分数与未来收益的线性相关性；越高代表分数越能解释收益。",
        "- `Rank IC`: 模型分数排名与未来收益排名的相关性；选股模型更重视这个指标，正数代表高分股票整体更靠前。",
        "- `ICIR / Rank ICIR`: IC 或 Rank IC 除以其波动，衡量稳定性；越高越稳定。",
        "- `Top-Bottom Spread`: 前10%股票平均收益减后10%股票平均收益；正数代表模型排序有区分度。",
        "- `成本后收益`: 该分组按日等权模拟后的平均收益，已扣除 A 股交易成本。",
        "- `最大回撤`: 该分组每日组合收益曲线从高点到低点的最大跌幅，用来衡量亏损风险。",
        "- `最差单日`: 该分组在单个验证日出现的最差组合收益。",
        "- `换手率`: 相邻两个预测日入选股票变化比例；越高代表交易更频繁、成本和执行压力更大。",
        "- `行业暴露`: 前1%或前5%入选股票按一级行业统计的出现次数占比。它不是收益指标，而是用来判断模型是否过度集中在某几个行业；例如 `电子:30%` 表示该分组入选样本约 30% 来自电子行业。",
        "",
        "## 模型排名",
        "",
        "| 排名 | 模型 | 标签 | 结论 | 内部Rank IC | 内部多空差 | 前1%成本后收益 | 前5%成本后收益 | 外部Rank IC | 原因 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for idx, item in enumerate(ranked, start=1):
        internal = item["internal"]
        external = item["external"]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    str(item["model_name"]),
                    str(item["label_name"]),
                    str(item["status"]),
                    _fmt_metric(internal.get("OVERALL", {}).get("rank_ic")),
                    _fmt_metric(internal.get("OVERALL", {}).get("top_bottom_spread")),
                    _fmt_metric(internal.get("TOP_1", {}).get("net_return")),
                    _fmt_metric(internal.get("TOP_5", {}).get("net_return")),
                    _fmt_metric(external.get("OVERALL", {}).get("rank_ic")),
                    str(item["reason"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 内部测试段排序能力",
            "",
            "| 模型 | 有效日期 | 样本数 | 覆盖率 | IC | Rank IC | ICIR | Rank ICIR | 前10%收益 | 后10%收益 | 多空差 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in ranked:
        overall = item["internal"].get("OVERALL", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item["model_name"]),
                    str(overall.get("score_date_count", 0)),
                    str(overall.get("sample_count", 0)),
                    _fmt_metric(overall.get("coverage")),
                    _fmt_metric(overall.get("ic")),
                    _fmt_metric(overall.get("rank_ic")),
                    _fmt_metric(overall.get("icir")),
                    _fmt_metric(overall.get("rank_icir")),
                    _fmt_metric(overall.get("top_10_return")),
                    _fmt_metric(overall.get("bottom_10_return")),
                    _fmt_metric(overall.get("top_bottom_spread")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 前1% / 前5% 可赚钱能力与风险",
            "",
            "| 模型 | 区间 | 分组 | 样本数 | 平均收益 | 胜率 | 平均超额收益 | 超额胜率 | 成本后收益 | 最大回撤 | 最差单日 | 波动率 | 连续亏损天数 | 换手率 |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in ranked:
        for segment_name in ("internal", "external"):
            for bucket in ("TOP_1", "TOP_5"):
                metrics = item[segment_name].get(bucket, {})
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(item["model_name"]),
                            _segment_label(segment_name),
                            _bucket_label(bucket),
                            str(metrics.get("sample_count", 0)),
                            _fmt_metric(metrics.get("avg_return")),
                            _fmt_metric(metrics.get("win_rate")),
                            _fmt_metric(metrics.get("avg_excess_return")),
                            _fmt_metric(metrics.get("excess_win_rate")),
                            _fmt_metric(metrics.get("net_return")),
                            _fmt_metric(metrics.get("max_drawdown")),
                            _fmt_metric(metrics.get("worst_daily_return")),
                            _fmt_metric(metrics.get("volatility")),
                            str(metrics.get("consecutive_loss_days") or 0),
                            _fmt_metric(metrics.get("turnover")),
                        ]
                    )
                    + " |"
                )
    lines.extend(
        [
            "",
            "## 行业暴露",
            "",
            "行业暴露统计内部测试段中，模型前1%和前5%高分股票分别集中在哪些一级行业。该指标用于识别模型是否只是押中了某个行业行情，而不是稳定具备跨行业选股能力。",
            "",
            "| 模型 | 分组 | 主要行业暴露 |",
            "|---|---|---|",
        ]
    )
    for item in ranked:
        for bucket in ("TOP_1", "TOP_5"):
            exposure = item["internal"].get(bucket, {}).get("industry_exposure") or {}
            lines.append(f"| {item['model_name']} | {_bucket_label(bucket)} | {_format_industry_exposure(exposure)} |")
    lines.extend(["", "## 治理写入摘要", ""])
    lines.append("- 已写入 `model_validation_runs`、`model_validation_metrics`。")
    lines.append("- 已向 `model_registry.metrics_json.latest_fixed_test_validation` 追加摘要。")
    lines.append("- 未写入 `model_scores`，未修改模型状态。")
    summary_path = output_dir / "summary.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    serializable = {"run_id": run_id, "as_of": as_of, "models": model_results}
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")

    for item in ranked:
        model_lines = [
            f"# {item['model_name']} 常规模型验证",
            "",
            f"- model_version: `{item['model_version']}`",
            f"- 特征集: `{item['feature_set']}`",
            f"- 标签: `{item['label_name']}`",
            f"- 预测周期: `T+{item['horizon_days']}`",
            f"- artifact_path: `{item['artifact_path']}`",
            f"- 训练区间: `{item['train_start']}` 至 `{item['train_end']}`",
            f"- 验证区间: `{item['valid_start']}` 至 `{item['valid_end']}`",
            f"- 测试区间: `{item['test_start']}` 至 `{item['test_end']}`",
            f"- 内部测试段: `{item['internal_window'][0] if item['internal_window'] else '-'}` 至 `{item['internal_window'][-1] if item['internal_window'] else '-'}`",
            f"- 外部验证段: `{item['external_window'][0] if item['external_window'] else '-'}` 至 `{item['external_window'][-1] if item['external_window'] else '-'}`",
            f"- 独立性级别: `{item['independence_level']}`",
            f"- 结论: `{item['status']}`",
            f"- 原因: {item['reason']}",
            "",
            "## 分组收益与风险",
            "",
            "| 区间 | 分组 | 样本数 | 平均收益 | 胜率 | 平均超额收益 | 成本后收益 | 最大回撤 | 最差单日 | 换手率 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for segment_name in ("internal", "external"):
            for bucket in ("TOP_1", "TOP_5", "TOP_10", "BOTTOM_10"):
                metrics = item[segment_name].get(bucket, {})
                model_lines.append(
                    "| "
                    + " | ".join(
                        [
                            _segment_label(segment_name),
                            _bucket_label(bucket),
                            str(metrics.get("sample_count", 0)),
                            _fmt_metric(metrics.get("avg_return")),
                            _fmt_metric(metrics.get("win_rate")),
                            _fmt_metric(metrics.get("avg_excess_return")),
                            _fmt_metric(metrics.get("net_return")),
                            _fmt_metric(metrics.get("max_drawdown")),
                            _fmt_metric(metrics.get("worst_daily_return")),
                            _fmt_metric(metrics.get("turnover")),
                        ]
                    )
                    + " |"
                )
        model_lines.extend(["", "## 内部测试段行业暴露", ""])
        model_lines.append("行业暴露表示该模型高分股票在一级行业上的分布比例，用来识别是否过度集中押注某些行业。")
        for bucket in ("TOP_1", "TOP_5"):
            model_lines.append(f"- {_bucket_label(bucket)}: {_format_industry_exposure(item['internal'].get(bucket, {}).get('industry_exposure') or {})}")
        (models_dir / f"{item['model_name']}.md").write_text("\n".join(model_lines) + "\n", encoding="utf-8")
    return summary_path, metrics_path


def model_evaluate(
    db_path: str,
    *,
    pool: str,
    model_version: str,
    as_of: str,
    mode: str = "fixed-test",
    output_dir: Path | None = None,
) -> ModelEvaluateResult:
    if pool != "baseline18":
        raise ValueError("Only pool=baseline18 is supported")
    if mode != "fixed-test":
        raise ValueError("Only mode=fixed-test is supported")
    run_id = _run_id("fixed_eval", as_of)
    output_dir = output_dir or MODEL_VALIDATION_DIR / f"fixed_test_{_compact_date(as_of)}_{pool}"
    started = now_utc()
    with connect(db_path) as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM model_registry
            WHERE model_version = ?
              AND model_name LIKE 'arena_%'
            ORDER BY model_name
            """,
            (model_version,),
        ).fetchall()
        conn.execute(
            """
            INSERT INTO model_validation_runs (
                run_id, as_of_date, pool, model_version, mode, status,
                model_count, started_at
            )
            VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (run_id, as_of, pool, model_version, mode, len(rows), started),
        )
        conn.commit()

    model_results: list[dict[str, Any]] = []
    try:
        with connect(db_path) as conn:
            init_db(conn)
            for row in rows:
                frame = _prediction_frame_from_artifact(row)
                horizon = int(row["horizon_days"])
                trading_dates = _trading_dates(
                    conn,
                    str(frame["score_date"].min()),
                    as_of,
                )
                mature_dates: list[str] = []
                score_dates = sorted(set(str(value) for value in frame["score_date"].unique()))
                score_date_set = set(score_dates)
                for idx, day in enumerate(trading_dates):
                    if day in score_date_set and idx + horizon < len(trading_dates):
                        mature_dates.append(day)
                internal_dates, external_dates, independence = _segment_dates(mature_dates)
                internal = _evaluate_prediction_segment(conn, frame, horizon=horizon, segment_dates=internal_dates)
                external = _evaluate_prediction_segment(conn, frame, horizon=horizon, segment_dates=external_dates)
                status, reason = _validation_conclusion(internal, external)
                item = {
                    "model_name": str(row["model_name"]),
                    "model_version": str(row["model_version"]),
                    "feature_set": str(row["feature_set"]),
                    "label_name": str(row["label_name"]),
                    "horizon_days": horizon,
                    "artifact_path": str(row["artifact_path"] or ""),
                    "train_start": str(row["train_start"] or ""),
                    "train_end": str(row["train_end"] or ""),
                    "valid_start": str(row["valid_start"] or ""),
                    "valid_end": str(row["valid_end"] or ""),
                    "test_start": str(row["test_start"] or ""),
                    "test_end": str(row["test_end"] or ""),
                    "internal_window": internal_dates,
                    "external_window": external_dates,
                    "independence_level": independence,
                    "status": status,
                    "reason": reason,
                    "internal": internal,
                    "external": external,
                }
                model_results.append(item)

            summary_path, metrics_path = _write_model_validation_report(output_dir, run_id, as_of, model_results)
            for item in model_results:
                _write_validation_metric_rows(
                    conn,
                    run_id,
                    item["model_name"],
                    item["model_version"],
                    "internal_test",
                    item["status"],
                    item["independence_level"],
                    item["internal"],
                )
                _write_validation_metric_rows(
                    conn,
                    run_id,
                    item["model_name"],
                    item["model_version"],
                    "external_validation",
                    item["status"],
                    item["independence_level"],
                    item["external"],
                )
                _update_registry_validation_summary(
                    conn,
                    model_name=item["model_name"],
                    model_version=item["model_version"],
                    run_id=run_id,
                    status=item["status"],
                    report_path=str(summary_path),
                    internal=item["internal"],
                    external=item["external"],
                )
            counts = Counter(str(item["status"]) for item in model_results)
            status = "SUCCESS" if model_results else "FAILED"
            conn.execute(
                """
                UPDATE model_validation_runs
                SET status = ?, model_count = ?, pass_count = ?, watch_count = ?,
                    fail_count = ?, insufficient_count = ?, report_path = ?,
                    metrics_path = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    len(model_results),
                    counts.get("PASS", 0),
                    counts.get("WATCH", 0),
                    counts.get("FAIL", 0),
                    counts.get("INSUFFICIENT_SAMPLE", 0),
                    str(summary_path),
                    str(metrics_path),
                    now_utc(),
                    run_id,
                ),
            )
            conn.commit()
        return ModelEvaluateResult(
            run_id,
            status,
            str(summary_path),
            str(metrics_path),
            len(model_results),
            counts.get("PASS", 0),
            counts.get("WATCH", 0),
            counts.get("FAIL", 0),
            counts.get("INSUFFICIENT_SAMPLE", 0),
        )
    except Exception as exc:
        with connect(db_path) as conn:
            init_db(conn)
            conn.execute(
                """
                UPDATE model_validation_runs
                SET status = 'FAILED', error_message = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (str(exc), now_utc(), run_id),
            )
            conn.commit()
        return ModelEvaluateResult(run_id, "FAILED", "", "", 0, 0, 0, 0, 0, str(exc))


def baseline18_specs(as_of: str) -> list[ArenaModelSpec]:
    compact = _compact_date(as_of)
    specs: list[ArenaModelSpec] = []
    windows = [
        ("2024", "2024-01-02", "2026-03-31", "2026-04-01", "2026-04-30", "2026-05-01", as_of),
        ("2025", "2025-01-01", "2026-03-31", "2026-04-01", "2026-04-30", "2026-05-01", as_of),
        ("2026", "2026-01-01", "2026-04-30", "2026-05-01", "2026-05-15", "2026-05-16", as_of),
    ]
    for window_id, train_start, train_end, valid_start, valid_end, test_start, test_end in windows:
        for feature_set in ("Alpha158", "Alpha360"):
            for horizon in (2, 5, 10):
                model_name = f"arena_{feature_set.lower()}_{window_id}_t{horizon}"
                model_version = f"baseline18_{compact}"
                specs.append(
                    ArenaModelSpec(
                        model_name=model_name,
                        model_version=model_version,
                        feature_set=feature_set,
                        horizon_days=horizon,
                        train_start=train_start,
                        train_end=train_end,
                        valid_start=valid_start,
                        valid_end=valid_end,
                        test_start=test_start,
                        test_end=test_end,
                    )
                )
    return specs


def model_arena(
    db_path: str,
    as_of: str,
    pool: str = "baseline18",
    max_workers: int = 1,
    dry_run: bool = False,
    output_dir: Path | None = None,
    resume_run_id: str | None = None,
) -> ArenaResult:
    if pool != "baseline18":
        raise ValueError("Only pool=baseline18 is supported")
    if max_workers < 1 or max_workers > 2:
        raise ValueError("max_workers must be 1 or 2")
    specs = baseline18_specs(as_of)
    run_id = resume_run_id or _run_id("arena", as_of)
    if resume_run_id:
        with connect(db_path) as conn:
            init_db(conn)
            row = conn.execute("SELECT * FROM model_training_runs WHERE run_id = ?", (resume_run_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown training run: {resume_run_id}")
            output_dir = output_dir or Path(str(row["output_dir"]))
    else:
        output_dir = output_dir or PROJECT_ROOT / "reports" / "model_arena" / f"baseline18_{_compact_date(as_of)}"
    runs_dir = PROJECT_ROOT / "outputs" / "qlib_runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    started = now_utc()
    with connect(db_path) as conn:
        init_db(conn)
        if resume_run_id:
            conn.execute(
                """
                UPDATE model_training_runs
                SET status = 'RUNNING', max_workers = ?, total_models = ?,
                    output_dir = ?, error_message = '', finished_at = NULL
                WHERE run_id = ?
                """,
                (max_workers, len(specs), str(output_dir), run_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO model_training_runs (
                    run_id, as_of_date, pool, max_workers, status, total_models,
                    output_dir, started_at
                )
                VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, ?)
                """,
                (run_id, as_of, pool, max_workers, len(specs), str(output_dir), started),
            )
        conn.commit()

    completed_results = _load_successful_arena_results(db_path, run_id, specs) if resume_run_id else []
    completed_names = {result.spec.model_name for result in completed_results}
    pending_specs = [spec for spec in specs if spec.model_name not in completed_names]
    if dry_run:
        results = [
            ArenaModelResult(spec, "DRY_RUN", str(runs_dir / spec.model_name / "workflow.yaml"))
            for spec in pending_specs
        ]
    elif max_workers == 1:
        results = [_run_arena_spec(db_path, run_id, spec, runs_dir) for spec in pending_specs]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_run_arena_spec, db_path, run_id, spec, runs_dir): spec for spec in pending_specs}
            for future in as_completed(futures):
                results.append(future.result())
    results = completed_results + results

    completed = sum(1 for r in results if r.status == "SUCCESS")
    failed = sum(1 for r in results if r.status not in {"SUCCESS", "DRY_RUN"})
    status = "DRY_RUN" if dry_run else ("SUCCESS" if failed == 0 else "PARTIAL_SUCCESS" if completed else "FAILED")
    report_path = _write_arena_report(output_dir, run_id, results)
    recommended = _recommended_model(results)
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            UPDATE model_training_runs
            SET status = ?, completed_models = ?, failed_models = ?,
                report_path = ?, finished_at = ?
            WHERE run_id = ?
            """,
            (status, completed, failed, str(report_path), now_utc(), run_id),
        )
        if recommended:
            best = next((r for r in results if r.spec.model_name == recommended), None)
            if best:
                conn.execute(
                    """
                    UPDATE model_registry
                    SET status = 'CANDIDATE', updated_at = ?
                    WHERE model_name = ? AND model_version = ?
                    """,
                    (now_utc(), best.spec.model_name, best.spec.model_version),
                )
        conn.commit()
    return ArenaResult(
        run_id,
        status,
        str(report_path),
        str(output_dir),
        len(specs),
        completed,
        failed,
        recommended,
        tuple(results),
    )


def _load_successful_arena_results(
    db_path: str,
    run_id: str,
    specs: list[ArenaModelSpec],
) -> list[ArenaModelResult]:
    spec_map = {spec.model_name: spec for spec in specs}
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM model_evaluation_runs
            WHERE training_run_id = ? AND status = 'SUCCESS'
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
    results: list[ArenaModelResult] = []
    for row in rows:
        spec = spec_map.get(str(row["model_name"]))
        if spec is None:
            continue
        results.append(
            ArenaModelResult(
                spec=spec,
                status="SUCCESS",
                workflow_path=str(row["workflow_path"] or ""),
                artifact_path=str(row["artifact_path"] or ""),
                ic=float(row["ic"]) if row["ic"] is not None else None,
                rank_ic=float(row["rank_ic"]) if row["rank_ic"] is not None else None,
                top_return=float(row["top_return"]) if row["top_return"] is not None else None,
                bottom_return=float(row["bottom_return"]) if row["bottom_return"] is not None else None,
                top_bottom_spread=float(row["top_bottom_spread"]) if row["top_bottom_spread"] is not None else None,
                top_win_rate=float(row["top_win_rate"]) if row["top_win_rate"] is not None else None,
                avg_return=float(row["avg_return"]) if row["avg_return"] is not None else None,
                sample_count=int(row["sample_count"] or 0),
            )
        )
    return results


def _write_workflow(spec: ArenaModelSpec, run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    workflow = f"""qlib_init:
    provider_uri: "{QLIB_DIR}"
    region: cn
market: &market all
benchmark: &benchmark SH000300
data_handler_config: &data_handler_config
    start_time: {spec.train_start}
    end_time: {spec.test_end}
    fit_start_time: {spec.train_start}
    fit_end_time: {spec.train_end}
    instruments: *market
    label: ["{spec.label_expr}"]
task:
    model:
        class: LGBModel
        module_path: qlib.contrib.model.gbdt
        kwargs:
            loss: mse
            colsample_bytree: 0.8879
            learning_rate: 0.05
            subsample: 0.8789
            lambda_l1: 0.01
            lambda_l2: 0.1
            max_depth: 8
            num_leaves: 256
            early_stopping_rounds: 100
            num_boost_round: 2000
            num_threads: 8
            verbosity: -1
    dataset:
        class: DatasetH
        module_path: qlib.data.dataset
        kwargs:
            handler:
                class: {spec.feature_set}
                module_path: qlib.contrib.data.handler
                kwargs: *data_handler_config
            segments:
                train: [{spec.train_start}, {spec.train_end}]
                valid: [{spec.valid_start}, {spec.valid_end}]
                test: [{spec.test_start}, {spec.test_end}]
    record:
        - class: SignalRecord
          module_path: qlib.workflow.record_temp
          kwargs:
            model: <MODEL>
            dataset: <DATASET>
        - class: SigAnaRecord
          module_path: qlib.workflow.record_temp
          kwargs:
            ana_long_short: False
            ann_scaler: 252
"""
    path = run_dir / "workflow.yaml"
    path.write_text(workflow, encoding="utf-8")
    return path


def _pred_snapshot() -> dict[Path, int]:
    mlruns = PROJECT_ROOT / "mlruns"
    if not mlruns.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in mlruns.rglob("pred.pkl")}


def _newest_pred(before: dict[Path, int]) -> Path | None:
    candidates = []
    for path, mtime_ns in _pred_snapshot().items():
        if path not in before or mtime_ns > before[path]:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)


def _run_arena_spec(db_path: str, training_run_id: str, spec: ArenaModelSpec, runs_dir: Path) -> ArenaModelResult:
    model_dir = runs_dir / spec.model_name
    workflow_path = _write_workflow(spec, model_dir)
    stdout_path = model_dir / "stdout.log"
    stderr_path = model_dir / "stderr.log"
    started = now_utc()
    with connect(db_path) as conn:
        init_db(conn)
        _upsert_model_registry(conn, spec, "RESEARCH")
        conn.execute(
            """
            INSERT INTO model_evaluation_runs (
                training_run_id, model_name, model_version, feature_set, label_name,
                horizon_days, status, workflow_path, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            ON CONFLICT(training_run_id, model_name, model_version) DO UPDATE SET
                status = 'RUNNING',
                workflow_path = excluded.workflow_path,
                started_at = excluded.started_at,
                error_message = ''
            """,
            (
                training_run_id,
                spec.model_name,
                spec.model_version,
                spec.feature_set,
                spec.label_name,
                spec.horizon_days,
                str(workflow_path),
                started,
            ),
        )
        conn.commit()

    before = _pred_snapshot()
    t0 = time.time()
    run = subprocess.run(
        [PYTHON_BIN, "-m", "qlib.cli.run", str(workflow_path)],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=7200,
    )
    elapsed = time.time() - t0
    stdout_path.write_text(run.stdout or "", encoding="utf-8")
    stderr_path.write_text(run.stderr or "", encoding="utf-8")
    pred = _newest_pred(before) if run.returncode == 0 else None
    artifact_path = str(pred) if pred else ""
    result = ArenaModelResult(
        spec,
        "SUCCESS" if run.returncode == 0 and pred else "FAILED",
        str(workflow_path),
        artifact_path=artifact_path,
        elapsed_seconds=elapsed,
        error_message="" if run.returncode == 0 else (run.stderr or run.stdout or f"exit {run.returncode}")[-2000:],
    )
    if pred:
        ic, rank_ic = _read_sig_analysis(pred)
        metrics = _prediction_return_metrics(db_path, pred, spec.horizon_days)
        result.ic = ic
        result.rank_ic = rank_ic
        result.top_return = metrics.get("top_return")
        result.bottom_return = metrics.get("bottom_return")
        result.top_bottom_spread = metrics.get("top_bottom_spread")
        result.top_win_rate = metrics.get("top_win_rate")
        result.avg_return = metrics.get("avg_return")
        result.sample_count = int(metrics.get("sample_count") or 0)

    with connect(db_path) as conn:
        init_db(conn)
        _upsert_model_registry(conn, spec, "RESEARCH", artifact_path, _result_metrics_json(result))
        conn.execute(
            """
            UPDATE model_evaluation_runs
            SET status = ?, artifact_path = ?, ic = ?, rank_ic = ?,
                top_return = ?, bottom_return = ?, top_bottom_spread = ?,
                top_win_rate = ?, avg_return = ?, sample_count = ?,
                metrics_json = ?, finished_at = ?, error_message = ?
            WHERE training_run_id = ? AND model_name = ? AND model_version = ?
            """,
            (
                result.status,
                result.artifact_path,
                result.ic,
                result.rank_ic,
                result.top_return,
                result.bottom_return,
                result.top_bottom_spread,
                result.top_win_rate,
                result.avg_return,
                result.sample_count,
                json.dumps(_result_metrics_json(result), ensure_ascii=False, sort_keys=True),
                now_utc(),
                result.error_message,
                training_run_id,
                spec.model_name,
                spec.model_version,
            ),
        )
        conn.commit()
    return result


def _upsert_model_registry(
    conn: sqlite3.Connection,
    spec: ArenaModelSpec,
    status: str,
    artifact_path: str = "",
    metrics: dict[str, Any] | None = None,
) -> None:
    now = now_utc()
    conn.execute(
        """
        INSERT INTO model_registry (
            model_name, model_version, model_family, feature_set, label_name,
            label_expr, horizon_days, train_start, train_end, valid_start,
            valid_end, test_start, test_end, status, artifact_path,
            metrics_json, created_at, updated_at
        )
        VALUES (?, ?, 'qlib_lgbm', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(model_name, model_version) DO UPDATE SET
            feature_set = excluded.feature_set,
            label_name = excluded.label_name,
            label_expr = excluded.label_expr,
            horizon_days = excluded.horizon_days,
            train_start = excluded.train_start,
            train_end = excluded.train_end,
            valid_start = excluded.valid_start,
            valid_end = excluded.valid_end,
            test_start = excluded.test_start,
            test_end = excluded.test_end,
            status = excluded.status,
            artifact_path = excluded.artifact_path,
            metrics_json = excluded.metrics_json,
            updated_at = excluded.updated_at
        """,
        (
            spec.model_name,
            spec.model_version,
            spec.feature_set,
            spec.label_name,
            spec.label_expr,
            spec.horizon_days,
            spec.train_start,
            spec.train_end,
            spec.valid_start,
            spec.valid_end,
            spec.test_start,
            spec.test_end,
            status,
            artifact_path,
            json.dumps(metrics or {}, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    conn.commit()


def _read_sig_analysis(pred_path: Path) -> tuple[float | None, float | None]:
    sig_dir = pred_path.parent / "sig_analysis"
    values: list[float | None] = []
    for filename in ("ic.pkl", "ric.pkl"):
        path = sig_dir / filename
        if not path.exists():
            values.append(None)
            continue
        try:
            with path.open("rb") as handle:
                series = pickle.load(handle)
            values.append(float(series.mean()))
        except Exception:
            values.append(None)
    return values[0], values[1]


def _trading_dates(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM price_bars
        WHERE market = 'CN_A' AND date >= ? AND date <= ?
        ORDER BY date
        """,
        (start, end),
    ).fetchall()
    return [str(row["date"]) for row in rows]


def _price_pair_return(
    conn: sqlite3.Connection,
    ticker: str,
    entry_date: str,
    exit_date: str,
) -> float | None:
    entry = conn.execute(
        "SELECT open FROM price_bars WHERE market='CN_A' AND ticker=? AND date=?",
        (ticker, entry_date),
    ).fetchone()
    exit_row = conn.execute(
        "SELECT close FROM price_bars WHERE market='CN_A' AND ticker=? AND date=?",
        (ticker, exit_date),
    ).fetchone()
    if not entry or not exit_row or not entry["open"] or not exit_row["close"]:
        return None
    return float(exit_row["close"]) / float(entry["open"]) - 1.0


def _prediction_return_metrics(db_path: str, pred_path: Path, horizon: int) -> dict[str, float | int | None]:
    try:
        pred = pd.read_pickle(pred_path)
    except Exception:
        return {}
    if isinstance(pred, pd.Series):
        pred = pred.to_frame("score")
    if not isinstance(pred.index, pd.MultiIndex) or pred.empty:
        return {}
    df = pred.reset_index()
    if "datetime" not in df.columns or "instrument" not in df.columns:
        return {}
    score_col = "score" if "score" in df.columns else df.columns[-1]
    df["score_date"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d")
    df["ticker"] = df["instrument"].map(lambda x: qlib_instrument_to_ticker(str(x)))
    df = df[df["ticker"].notna()].copy()
    if df.empty:
        return {}
    with connect(db_path) as conn:
        min_date = str(df["score_date"].min())
        max_date = str(df["score_date"].max())
        dates = _trading_dates(conn, min_date, (date.fromisoformat(max_date) + timedelta(days=30)).isoformat())
        date_to_idx = {d: idx for idx, d in enumerate(dates)}
        returns: list[float] = []
        top_returns: list[float] = []
        bottom_returns: list[float] = []
        for score_date, group in df.groupby("score_date"):
            idx = date_to_idx.get(str(score_date))
            if idx is None or idx + horizon >= len(dates) or idx + 1 >= len(dates):
                continue
            entry_date = dates[idx + 1]
            exit_date = dates[idx + horizon]
            sorted_group = group.sort_values(str(score_col), ascending=False)
            n = max(1, int(len(sorted_group) * 0.1))
            selections = [
                ("top", sorted_group.head(n)),
                ("bottom", sorted_group.tail(n)),
            ]
            for bucket, selected in selections:
                for ticker in selected["ticker"]:
                    ret = _price_pair_return(conn, str(ticker), entry_date, exit_date)
                    if ret is None:
                        continue
                    returns.append(ret)
                    if bucket == "top":
                        top_returns.append(ret)
                    else:
                        bottom_returns.append(ret)
    top_avg = sum(top_returns) / len(top_returns) if top_returns else None
    bottom_avg = sum(bottom_returns) / len(bottom_returns) if bottom_returns else None
    avg = sum(returns) / len(returns) if returns else None
    win_rate = sum(1 for ret in top_returns if ret > 0) / len(top_returns) if top_returns else None
    spread = top_avg - bottom_avg if top_avg is not None and bottom_avg is not None else None
    return {
        "top_return": top_avg,
        "bottom_return": bottom_avg,
        "top_bottom_spread": spread,
        "top_win_rate": win_rate,
        "avg_return": avg,
        "sample_count": len(returns),
    }


def _result_metrics_json(result: ArenaModelResult) -> dict[str, Any]:
    return {
        "ic": result.ic,
        "rank_ic": result.rank_ic,
        "top_return": result.top_return,
        "bottom_return": result.bottom_return,
        "top_bottom_spread": result.top_bottom_spread,
        "top_win_rate": result.top_win_rate,
        "avg_return": result.avg_return,
        "sample_count": result.sample_count,
        "elapsed_seconds": result.elapsed_seconds,
    }


def _recommended_model(results: list[ArenaModelResult]) -> str | None:
    successful = [r for r in results if r.status == "SUCCESS"]
    if not successful:
        return None
    best = max(successful, key=lambda r: r.score_for_ranking())
    if best.score_for_ranking() <= 0:
        return None
    return best.spec.model_name


def _write_arena_report(output_dir: Path, run_id: str, results: list[ArenaModelResult]) -> Path:
    ranked = sorted(results, key=lambda r: r.score_for_ranking(), reverse=True)
    recommended = _recommended_model(results)
    lines = [
        "# Model Arena Baseline18 Report",
        "",
        f"- run_id: `{run_id}`",
        f"- total_models: `{len(results)}`",
        f"- completed: `{sum(1 for r in results if r.status == 'SUCCESS')}`",
        f"- failed: `{sum(1 for r in results if r.status not in {'SUCCESS', 'DRY_RUN'})}`",
        f"- recommended_candidate: `{recommended or '-'}`",
        "",
        "## Ranking",
        "",
        "| Rank | Model | Feature | Label | Status | Rank IC | IC | Top Ret | Bottom Ret | Spread | Top Win | Samples |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for idx, result in enumerate(ranked, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    result.spec.model_name,
                    result.spec.feature_set,
                    result.spec.label_name,
                    result.status,
                    _fmt_metric(result.rank_ic),
                    _fmt_metric(result.ic),
                    _fmt_metric(result.top_return),
                    _fmt_metric(result.bottom_return),
                    _fmt_metric(result.top_bottom_spread),
                    _fmt_metric(result.top_win_rate),
                    str(result.sample_count),
                ]
            )
            + " |"
        )
    failed = [r for r in results if r.status not in {"SUCCESS", "DRY_RUN"}]
    if failed:
        lines.extend(["", "## Failed Models", ""])
        for result in failed:
            lines.append(f"- `{result.spec.model_name}`: {result.error_message[:500]}")
    lines.extend(["", "## Candidate Guidance", ""])
    if recommended:
        lines.append(f"- 推荐进入 Candidate 观察：`{recommended}`。")
    else:
        lines.append("- 暂无模型达到自动推荐条件，需要人工复核。")
    path = output_dir / "summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fmt_metric(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def copy_report_no_overwrite(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
