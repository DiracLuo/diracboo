#!/usr/bin/env python3
"""Legacy/research Qlib prediction updater.

WARNING:
    This script is not the production path. It can mutate workflow YAML files
    and run dump_all-style refreshes. Use the production pipeline instead:

        python -m alpha_ledger production-run --as-of YYYY-MM-DD

    Keep this only for maintenance/research migration work.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alpha_ledger.db import DEFAULT_DB_PATH, connect, init_db  # noqa: E402
from alpha_ledger.screener import screen_all  # noqa: E402


PYTHON_BIN = "/opt/anaconda3/bin/python3"
QLIB_EXPORT_DIR = PROJECT_ROOT / "data" / "qlib_export_full"
QLIB_DIR = Path(os.path.expanduser("~/.qlib/qlib_data/alpha_ledger_full"))
DUMP_BIN = Path(os.path.expanduser("~/code/external/qlib/scripts/dump_bin.py"))


@dataclass(frozen=True)
class WorkflowSpec:
    config: str
    model_name: str | None = None
    model_version: str | None = None

    @property
    def should_import(self) -> bool:
        return self.model_name is not None and self.model_version is not None


def _date_suffix(as_of_date: str) -> str:
    return as_of_date.replace("-", "")


def workflow_specs(as_of_date: str) -> list[WorkflowSpec]:
    suffix = _date_suffix(as_of_date)
    return [
        WorkflowSpec(
            "workflow_config_alpha158.yaml",
            "qlib_alpha158",
            f"t5_full_{suffix}",
        ),
        WorkflowSpec("workflow_config_alpha158_20250101_t5.yaml"),
        WorkflowSpec(
            "workflow_config_alpha158_20250101_t10.yaml",
            "qlib_alpha158_20250101",
            f"t10_v3_{suffix}",
        ),
        WorkflowSpec("workflow_config_alpha158_20260101_t5.yaml"),
        WorkflowSpec(
            "workflow_config_alpha158_20260101_t10.yaml",
            "qlib_alpha158_20260101",
            f"t10_v3_{suffix}",
        ),
    ]


def _run(cmd: list[str], *, cwd: Path = PROJECT_ROOT, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        check=False,
        text=True,
        capture_output=capture,
    )


def _update_workflow_config(path: Path, as_of_date: str) -> bool:
    text = path.read_text(encoding="utf-8")
    updated = re.sub(
        r"(^\s*end_time:\s*)\d{4}-\d{2}-\d{2}",
        rf"\g<1>{as_of_date}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    updated = re.sub(
        r"(^\s*test:\s*\[[^,\]]+,\s*)\d{4}-\d{2}-\d{2}(\])",
        rf"\g<1>{as_of_date}\2",
        updated,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def update_workflow_configs(as_of_date: str) -> list[Path]:
    changed: list[Path] = []
    for path in sorted(PROJECT_ROOT.glob("workflow_config_alpha158*.yaml")):
        if _update_workflow_config(path, as_of_date):
            changed.append(path)
    return changed


def _pred_snapshot() -> dict[Path, int]:
    mlruns = PROJECT_ROOT / "mlruns"
    if not mlruns.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in mlruns.rglob("pred.pkl")}


def _newest_pred(before: dict[Path, int]) -> Path | None:
    candidates: list[Path] = []
    for path, mtime_ns in _pred_snapshot().items():
        if path not in before or mtime_ns > before[path]:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)


def export_qlib_csv(as_of_date: str) -> bool:
    result = _run([
        PYTHON_BIN,
        "-m",
        "alpha_ledger",
        "export-qlib-csv",
        "--start",
        "2024-01-02",
        "--end",
        as_of_date,
        "--output",
        str(QLIB_EXPORT_DIR),
        "--markets",
        "CN_A",
    ])
    return result.returncode == 0


def dump_qlib_bin() -> bool:
    result = _run([
        PYTHON_BIN,
        str(DUMP_BIN),
        "dump_all",
        "--data_path",
        str(QLIB_EXPORT_DIR.resolve()),
        "--qlib_dir",
        str(QLIB_DIR),
        "--include_fields",
        "open,close,high,low,volume,vwap,money,factor,change",
        "--date_field_name",
        "date",
        "--file_suffix",
        ".csv",
    ])
    return result.returncode == 0


def run_workflow(config: str, log_dir: Path) -> tuple[Path | None, str | None]:
    before = _pred_snapshot()
    result = _run([PYTHON_BIN, "-m", "qlib.cli.run", config], capture=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(config).stem
    (log_dir / f"{stem}.stdout.log").write_text(result.stdout or "", encoding="utf-8")
    (log_dir / f"{stem}.stderr.log").write_text(result.stderr or "", encoding="utf-8")
    if result.returncode != 0:
        return None, f"{config} failed with exit code {result.returncode}; see {log_dir / (stem + '.stderr.log')}"

    pred = _newest_pred(before)
    if pred is None:
        return None, f"{config} completed but no new pred.pkl was found"
    return pred, None


def import_predictions(db_path: Path, pred: Path, spec: WorkflowSpec) -> bool:
    if not spec.should_import:
        return False
    result = _run([
        PYTHON_BIN,
        "-m",
        "alpha_ledger",
        "--db",
        str(db_path),
        "import-qlib-predictions",
        "--artifact",
        str(pred),
        "--model-name",
        str(spec.model_name),
        "--model-version",
        str(spec.model_version),
        "--market",
        "CN_A",
    ])
    return result.returncode == 0


def rerun_screen(db_path: Path, as_of_date: str) -> int:
    with connect(db_path) as conn:
        init_db(conn)
        return screen_all(conn, as_of_date)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh and import Alpha Ledger Qlib model predictions")
    parser.add_argument("--as-of", required=True, help="Prediction date, e.g. 2026-06-02")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--skip-export", action="store_true", help="Skip export-qlib-csv")
    parser.add_argument("--skip-dump-bin", action="store_true", help="Skip Qlib dump_bin.py")
    parser.add_argument("--skip-workflow-config-update", action="store_true", help="Do not update workflow end dates")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    warnings: list[str] = []
    imported = 0

    if not args.skip_export and not export_qlib_csv(args.as_of):
        warnings.append("export-qlib-csv failed; continuing with existing Qlib data")
    if not args.skip_dump_bin and not dump_qlib_bin():
        warnings.append("dump_bin.py failed; continuing with existing Qlib binary data")

    if not args.skip_workflow_config_update:
        changed = update_workflow_configs(args.as_of)
        if changed:
            print("Updated workflow configs: " + ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in changed))

    log_dir = PROJECT_ROOT / "reports" / "model_prediction_logs" / args.as_of
    for spec in workflow_specs(args.as_of):
        pred, warning = run_workflow(spec.config, log_dir)
        if warning:
            warnings.append(warning)
            print(f"WARNING: {warning}")
            continue
        print(f"{spec.config}: {pred}")
        if spec.should_import and pred is not None:
            if import_predictions(db_path, pred, spec):
                imported += 1
            else:
                warnings.append(f"import failed for {spec.model_name} {spec.model_version}")

    if imported:
        candidate_count = rerun_screen(db_path, args.as_of)
        print(f"Re-screened {candidate_count} candidates for {args.as_of} after importing model predictions.")

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0 if imported else 1


if __name__ == "__main__":
    raise SystemExit(main())
