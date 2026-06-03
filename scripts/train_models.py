#!/usr/bin/env python3
"""训练 Qlib 模型并保存到 Recorder。

训练完成后，每个模型的 recorder 会被打上标签（model_name, model_version），
推理脚本通过标签找到对应的 recorder。

用法:
    # 生产模型训练
    python scripts/train_models.py --as-of 2026-06-02
    python scripts/train_models.py --as-of 2026-06-02 --only M1

    # Handler × Label 对比（串行，不并行）
    python scripts/train_models.py --as-of 2026-06-03 --compare
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PYTHON_BIN = "/opt/anaconda3/bin/python3"

# (模型标识, workflow config, label T+N, handler class, 训练起始日)
TRAIN_CONFIGS = [
    {
        "id": "M1",
        "config": "workflow_config_alpha360_m1_prod.yaml",
        "label": "Ref($close, -2) / $close - 1",
        "handler": "Alpha360",
        "train_start": "2024-09-01",
        "model_name": "qlib_alpha360",
        "model_version_prefix": "t2_18m",
    },
    {
        "id": "M2",
        "config": "workflow_config_alpha360_t2_m2w.yaml",
        "label": "Ref($close, -2) / $close - 1",
        "handler": "Alpha360",
        "train_start": "2025-01-01",
        "model_name": "qlib_alpha360_20250101",
        "model_version_prefix": "t2_15m",
    },
    {
        "id": "M3",
        "config": "workflow_config_alpha360_20260101_prod.yaml",
        "label": "Ref($close, -2) / $close - 1",
        "handler": "Alpha360",
        "train_start": "2026-01-01",
        "model_name": "qlib_alpha360_20260101",
        "model_version_prefix": "t2_4m",
    },
]

# Handler × Label 对比配置（串行训练，不并行）
COMPARE_CONFIGS = [
    {"id": "alpha158_t2",  "config": "workflow_config_alpha158_t2.yaml",  "handler": "Alpha158", "label": "T+2",  "model_name": "compare_alpha158_t2"},
    {"id": "alpha158_t5",  "config": "workflow_config_alpha158_t5.yaml",  "handler": "Alpha158", "label": "T+5",  "model_name": "compare_alpha158_t5"},
    {"id": "alpha158_t10", "config": "workflow_config_alpha158_t10.yaml", "handler": "Alpha158", "label": "T+10", "model_name": "compare_alpha158_t10"},
    {"id": "alpha360_t2",  "config": "workflow_config_alpha360_t2.yaml",  "handler": "Alpha360", "label": "T+2",  "model_name": "compare_alpha360_t2"},
    {"id": "alpha360_t5",  "config": "workflow_config_alpha360_t5.yaml",  "handler": "Alpha360", "label": "T+5",  "model_name": "compare_alpha360_t5"},
    {"id": "alpha360_t10", "config": "workflow_config_alpha360_t10.yaml", "handler": "Alpha360", "label": "T+10", "model_name": "compare_alpha360_t10"},
]

# M2 窗口（2025-01-01 起）对比配置
COMPARE_M2_CONFIGS = [
    {"id": "m2w_alpha158_t2",  "config": "workflow_config_alpha158_t2_m2w.yaml",  "handler": "Alpha158", "label": "T+2",  "model_name": "compare_m2w_alpha158_t2"},
    {"id": "m2w_alpha158_t5",  "config": "workflow_config_alpha158_t5_m2w.yaml",  "handler": "Alpha158", "label": "T+5",  "model_name": "compare_m2w_alpha158_t5"},
    {"id": "m2w_alpha158_t10", "config": "workflow_config_alpha158_t10_m2w.yaml", "handler": "Alpha158", "label": "T+10", "model_name": "compare_m2w_alpha158_t10"},
    {"id": "m2w_alpha360_t2",  "config": "workflow_config_alpha360_t2_m2w.yaml",  "handler": "Alpha360", "label": "T+2",  "model_name": "compare_m2w_alpha360_t2"},
    {"id": "m2w_alpha360_t5",  "config": "workflow_config_alpha360_t5_m2w.yaml",  "handler": "Alpha360", "label": "T+5",  "model_name": "compare_m2w_alpha360_t5"},
    {"id": "m2w_alpha360_t10", "config": "workflow_config_alpha360_t10_m2w.yaml", "handler": "Alpha360", "label": "T+10", "model_name": "compare_m2w_alpha360_t10"},
]

# M3 窗口（2026-01-01 起）对比配置
COMPARE_M3_CONFIGS = [
    {"id": "m3w_alpha158_t2",  "config": "workflow_config_alpha158_t2_m3w.yaml",  "handler": "Alpha158", "label": "T+2",  "model_name": "compare_m3w_alpha158_t2"},
    {"id": "m3w_alpha158_t5",  "config": "workflow_config_alpha158_t5_m3w.yaml",  "handler": "Alpha158", "label": "T+5",  "model_name": "compare_m3w_alpha158_t5"},
    {"id": "m3w_alpha158_t10", "config": "workflow_config_alpha158_t10_m3w.yaml", "handler": "Alpha158", "label": "T+10", "model_name": "compare_m3w_alpha158_t10"},
    {"id": "m3w_alpha360_t2",  "config": "workflow_config_alpha360_t2_m3w.yaml",  "handler": "Alpha360", "label": "T+2",  "model_name": "compare_m3w_alpha360_t2"},
    {"id": "m3w_alpha360_t5",  "config": "workflow_config_alpha360_t5_m3w.yaml",  "handler": "Alpha360", "label": "T+5",  "model_name": "compare_m3w_alpha360_t5"},
    {"id": "m3w_alpha360_t10", "config": "workflow_config_alpha360_t10_m3w.yaml", "handler": "Alpha360", "label": "T+10", "model_name": "compare_m3w_alpha360_t10"},
]


def _update_config(config_path: Path, as_of_date: str) -> None:
    """更新 workflow config 的 end_time 和 test 段日期。"""
    text = config_path.read_text(encoding="utf-8")
    updated = re.sub(
        r"(^\s*end_time:\s*)\d{4}-\d{2}-\d{2}",
        rf"\g<1>{as_of_date}", text, count=1, flags=re.MULTILINE,
    )
    updated = re.sub(
        r"(^\s*test:\s*\[[^,\]]+,\s*)\d{4}-\d{2}-\d{2}(\])",
        rf"\g<1>{as_of_date}\2", updated, count=1, flags=re.MULTILINE,
    )
    config_path.write_text(updated, encoding="utf-8")


def _tag_latest_recorder(exp_name: str, model_name: str, model_version: str) -> None:
    """给最新 recorder 打标签（用 MLflow API）。"""
    import mlflow
    mlruns = PROJECT_ROOT / "mlruns"
    # 找实验 ID
    exp_id = None
    for d in mlruns.iterdir():
        meta = d / "meta.yaml"
        if meta.exists() and f"name: {exp_name}" in meta.read_text():
            exp_id = d.name
            break
    if not exp_id:
        return
    # 找最新 recorder（按修改时间）
    latest_rec = None
    latest_mtime = 0
    for d in (mlruns / exp_id).iterdir():
        if d.is_dir() and (d / "artifacts").exists():
            mtime = d.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_rec = d.name
    if not latest_rec:
        return
    mlflow.set_tracking_uri(f"file://{mlruns}")
    mlflow.set_experiment(exp_name)
    with mlflow.start_run(run_id=latest_rec):
        mlflow.set_tag("model_name", model_name)
        mlflow.set_tag("model_version", model_version)
    print(f"  Tagged {exp_name}/{latest_rec[:8]}: {model_name}@{model_version}")


def train_one(cfg: dict, as_of_date: str) -> bool:
    """训练单个模型。"""
    config_path = PROJECT_ROOT / cfg["config"]
    if not config_path.exists():
        print(f"  Config not found: {cfg['config']}")
        return False

    _update_config(config_path, as_of_date)
    print(f"Training {cfg['id']}: {cfg['config']} (end={as_of_date})...")

    result = subprocess.run(
        [PYTHON_BIN, "-m", "qlib.cli.run", cfg["config"]],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=1800,
    )

    if result.returncode != 0:
        stderr = result.stderr[-300:] if result.stderr else ""
        print(f"  FAILED (exit {result.returncode}): {stderr}")
        return False

    model_version = f"{cfg['model_version_prefix']}_{as_of_date.replace('-', '')}"
    _tag_latest_recorder("workflow", cfg["model_name"], model_version)
    return True


def _read_recorder_ic(exp_name: str, model_name: str) -> dict | None:
    """从最新 recorder 读取 IC 和 Rank IC 均值。"""
    import pickle
    import mlflow

    mlruns = PROJECT_ROOT / "mlruns"
    mlflow.set_tracking_uri(f"file://{mlruns}")

    # 找实验
    exp_id = None
    for d in mlruns.iterdir():
        meta = d / "meta.yaml"
        if meta.exists() and f"name: {exp_name}" in meta.read_text():
            exp_id = d.name
            break
    if not exp_id:
        return None

    # 找匹配 model_name 的 recorder
    target_rec = None
    for d in (mlruns / exp_id).iterdir():
        tag_file = d / "tags" / "mlflow.runName"
        name_tag = d / "tags" / "model_name"
        if name_tag.exists() and name_tag.read_text().strip() == model_name:
            target_rec = d
            break
    if not target_rec:
        # fallback: 用最新 recorder
        latest_mtime = 0
        for d in (mlruns / exp_id).iterdir():
            if d.is_dir() and (d / "artifacts").exists():
                mtime = d.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    target_rec = d

    if not target_rec:
        return None

    sig_dir = target_rec / "artifacts" / "sig_analysis"
    result = {}
    for name, fname in [("IC", "ic.pkl"), ("Rank_IC", "ric.pkl")]:
        pkl_path = sig_dir / fname
        if pkl_path.exists():
            try:
                with open(pkl_path, "rb") as f:
                    series = pickle.load(f)
                result[name] = float(series.mean())
            except Exception:
                result[name] = None
        else:
            result[name] = None
    return result


def train_compare(as_of_date: str, configs: list[dict] | None = None, window_label: str = "M1") -> int:
    """串行训练所有对比模型并输出汇总。"""
    import time

    if configs is None:
        configs = COMPARE_CONFIGS
    results = []
    total = len(configs)

    for i, cfg in enumerate(configs, 1):
        config_path = PROJECT_ROOT / cfg["config"]
        if not config_path.exists():
            print(f"\n[{i}/{total}] SKIP {cfg['id']}: config not found")
            results.append({"id": cfg["id"], "handler": cfg["handler"], "label": cfg["label"],
                            "status": "SKIP", "IC": None, "Rank_IC": None})
            continue

        print(f"\n[{i}/{total}] Training {cfg['id']} ({cfg['handler']} {cfg['label']})...")
        _update_config(config_path, as_of_date)
        t0 = time.time()

        result = subprocess.run(
            [PYTHON_BIN, "-m", "qlib.cli.run", cfg["config"]],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=1800,
        )

        elapsed = time.time() - t0
        if result.returncode != 0:
            stderr = result.stderr[-300:] if result.stderr else ""
            print(f"  FAILED ({elapsed:.0f}s, exit {result.returncode}): {stderr}")
            results.append({"id": cfg["id"], "handler": cfg["handler"], "label": cfg["label"],
                            "status": "FAIL", "IC": None, "Rank_IC": None})
            continue

        # 打标签
        _tag_latest_recorder("workflow", cfg["model_name"], f"compare_{as_of_date.replace('-', '')}")

        # 读 IC
        ic_data = _read_recorder_ic("workflow", cfg["model_name"])
        ic_val = ic_data.get("IC") if ic_data else None
        ric_val = ic_data.get("Rank_IC") if ic_data else None

        ic_str = f"{ic_val:.4f}" if ic_val is not None else "N/A"
        ric_str = f"{ric_val:.4f}" if ric_val is not None else "N/A"
        print(f"  Done ({elapsed:.0f}s) — IC={ic_str}, Rank IC={ric_str}")

        results.append({"id": cfg["id"], "handler": cfg["handler"], "label": cfg["label"],
                        "status": "OK", "IC": ic_val, "Rank_IC": ric_val, "time": elapsed})

    # 汇总
    print("\n" + "=" * 70)
    print(f"COMPARISON RESULTS — {window_label} Window")
    print("=" * 70)
    print(f"{'ID':<20} {'Handler':<12} {'Label':<8} {'Status':<8} {'IC':>10} {'Rank IC':>10} {'Time':>8}")
    print("-" * 70)
    for r in results:
        ic_s = f"{r['IC']:.4f}" if r['IC'] is not None else "N/A"
        ric_s = f"{r['Rank_IC']:.4f}" if r['Rank_IC'] is not None else "N/A"
        t_s = f"{r.get('time', 0):.0f}s" if r.get('time') else ""
        print(f"{r['id']:<20} {r['handler']:<12} {r['label']:<8} {r['status']:<8} {ic_s:>10} {ric_s:>10} {t_s:>8}")

    # 写报告
    report_path = PROJECT_ROOT / "reports" / f"model_compare_{window_label}_{as_of_date.replace('-', '')}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(f"# Model Comparison Report — {as_of_date} ({window_label} Window)\n\n")
        f.write("## Results\n\n")
        f.write(f"| ID | Handler | Label | Status | IC | Rank IC | Time |\n")
        f.write(f"|---|---|---|---|---|---|---|\n")
        for r in results:
            ic_s = f"{r['IC']:.4f}" if r['IC'] is not None else "N/A"
            ric_s = f"{r['Rank_IC']:.4f}" if r['Rank_IC'] is not None else "N/A"
            t_s = f"{r.get('time', 0):.0f}s" if r.get('time') else ""
            f.write(f"| {r['id']} | {r['handler']} | {r['label']} | {r['status']} | {ic_s} | {ric_s} | {t_s} |\n")

        # 对比分析
        ok_results = [r for r in results if r["status"] == "OK" and r["IC"] is not None]
        if ok_results:
            f.write("\n## Analysis\n\n")
            # Handler 对比
            for handler in ["Alpha158", "Alpha360"]:
                handler_results = [r for r in ok_results if r["handler"] == handler]
                if handler_results:
                    avg_ic = sum(r["IC"] for r in handler_results) / len(handler_results)
                    avg_ric = sum(r["Rank_IC"] for r in handler_results) / len(handler_results)
                    f.write(f"### {handler}\n")
                    f.write(f"- Avg IC: {avg_ic:.4f}, Avg Rank IC: {avg_ric:.4f}\n")
                    for r in handler_results:
                        f.write(f"  - {r['label']}: IC={r['IC']:.4f}, Rank IC={r['Rank_IC']:.4f}\n")
                    f.write("\n")

            # 最优
            best = max(ok_results, key=lambda r: r["Rank_IC"])
            f.write(f"### Best by Rank IC\n")
            f.write(f"- **{best['id']}** ({best['handler']} {best['label']}): IC={best['IC']:.4f}, Rank IC={best['Rank_IC']:.4f}\n")

    print(f"\nReport: {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train Qlib models")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--only", nargs="*", default=["M1", "M2", "M3"])
    parser.add_argument("--compare", action="store_true",
                        help="串行训练 6 个 Handler×Label 对比模型（M1 窗口: 2024-01-02 起）")
    parser.add_argument("--compare-m2", action="store_true",
                        help="串行训练 6 个 Handler×Label 对比模型（M2 窗口: 2025-01-01 起）")
    parser.add_argument("--compare-m3", action="store_true",
                        help="串行训练 6 个 Handler×Label 对比模型（M3 窗口: 2026-01-01 起）")
    args = parser.parse_args(argv)

    if args.compare:
        return train_compare(args.as_of, COMPARE_CONFIGS, "M1")
    if args.compare_m2:
        return train_compare(args.as_of, COMPARE_M2_CONFIGS, "M2")
    if args.compare_m3:
        return train_compare(args.as_of, COMPARE_M3_CONFIGS, "M3")

    ok = 0
    fail = 0
    for cfg in TRAIN_CONFIGS:
        if cfg["id"] not in args.only:
            continue
        if train_one(cfg, args.as_of):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} trained, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
