#!/usr/bin/env python
"""Walkforward validation of freqtrade hyperopt — training-bias estimation.

Per fold: hyperopt on the in-sample (IS) window, backtest the exported best
params on the following out-of-sample (OOS) window, slide both by --oos-days.
Reports mean IS / mean OOS and the training bias (mean IS - mean OOS) for
profit_per_day, profit_factor and sharpe — the three metrics of Timothy
Masters' TrnBias.CPP (mean return / profit factor / Sharpe ratio).

IS performance comes free from the best epoch of the fold's .fthypt file;
OOS performance from the backtest-result zip. Both are produced by the same
backtesting engine, so the numbers are comparable.

Usage (venv active, from the freqtrade repo):
  python scripts/walkforward.py \
    -c ../freqtrade_data/c/LTHM.json --userdir ../freqtrade_data \
    --strategy LTHM --timerange 20210101-20260813 \
    --is-days 365 --oos-days 365 -e 500

Outputs (default <userdir>/walkforward_results/<strategy>/):
  folds.csv               one row per fold, written incrementally (resume-safe)
  summary.txt             per-fold table + bias summary
  fold_NNN_params.json    params hyperopt exported for that fold (audit trail)
  params_backup.json      pre-run copy of strategies/<S>.json (restored on exit)
  fold_NNN_hyperopt.log   raw child-process logs
  fold_NNN_backtest.log
  backtest-result-*.zip   per-fold IS/OOS backtest results (--export-directory)

Additionally, at the end of the run all folds' OOS trades are stitched into one
standard backtest result zip in <userdir>/backtest_results/
(backtest-result-<ts>_WF.zip + .meta.json) — viewable in the webserver (freqUI)
as a single backtest of the whole walkforward. No extra flag; re-running a
finished walkforward re-stitches from the per-fold zips kept in the output dir.

Caveats:
  - Hyperopt overwrites strategies/<S>.json on every run; this script snapshots
    and restores it, but do not run other hyperopts concurrently.
  - FreqAI strategies: if a fold aborts mid-train, delete
    <userdir>/models/<identifier>/backtesting_predictions/ before rerunning —
    crashed runs leave poisoned prediction caches that get silently reused.
  - Sequential by design: the params file is shared state between the hyperopt
    and the backtest of one fold.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

METRICS = ("profit_per_day", "profit_factor", "sharpe")
MIN_OOS_DAYS = 30
CSV_FIELDS = [
    "fold", "is_start", "is_end", "oos_start", "oos_end",
    *[f"is_{m}" for m in METRICS], "is_trades", "is_loss",
    *[f"oos_{m}" for m in METRICS], "oos_trades", "oos_force", "params", "oos_zip",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", required=True, help="freqtrade config (e.g. ../freqtrade_data/c/LTHM.json)")
    p.add_argument("--userdir", required=True)
    p.add_argument("-s", "--strategy", required=True)
    p.add_argument("--timerange", required=True, help="full range YYYYMMDD-YYYYMMDD")
    p.add_argument("--is-days", type=int, default=365, help="in-sample window length in days")
    p.add_argument("--oos-days", type=int, default=365, help="out-of-sample window length / slide step in days")
    p.add_argument("-e", "--epochs", type=int, default=500)
    p.add_argument("--hyperopt-loss", default="MultiMetricHyperOptLoss")
    p.add_argument("--spaces", default=None, help="hyperopt spaces (passed to freqtrade; default = freqtrade default space)")
    p.add_argument("--jobs", type=int, default=None,
                   help="hyperopt parallel workers (-j): -1=all CPUs, -2=all but one, -3=all but two, ...")
    p.add_argument("--no-optimize", action="store_true",
                   help="skip hyperopt: validate the strategy's pinned params as-is (rolling "
                        "IS/OOS backtests). For fully frozen strategies with an empty hyperopt space.")
    p.add_argument("--out", default=None, help="output dir (default <userdir>/walkforward_results/<strategy>)")
    return p.parse_args()


def parse_timerange(tr: str) -> tuple[datetime, datetime]:
    a, _, b = tr.partition("-")
    if not a or not b or not a.isdigit() or not b.isdigit():
        raise SystemExit(f"--timerange must be YYYYMMDD-YYYYMMDD, got {tr!r}")
    return datetime.strptime(a, "%Y%m%d"), datetime.strptime(b, "%Y%m%d")


def build_folds(start: datetime, end: datetime, is_days: int, oos_days: int) -> list[tuple[datetime, datetime, datetime]]:
    folds = []
    t = start
    while (end - (t + timedelta(days=is_days))).days >= MIN_OOS_DAYS:
        is_end = t + timedelta(days=is_days)
        folds.append((t, is_end, min(is_end + timedelta(days=oos_days), end)))
        t += timedelta(days=oos_days)
    return folds


def run_cmd(cmd: list[str], log_path: Path) -> None:
    print(f"  $ {' '.join(cmd)}", flush=True)
    with log_path.open("w") as log:
        p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        tail = log_path.read_text().splitlines()[-30:]
        raise SystemExit(f"command failed ({p.returncode}); last {len(tail)} log lines of {log_path}:\n" + "\n".join(tail))


def newest(path: Path, pattern: str, not_before: float) -> Path:
    cands = [p for p in path.glob(pattern) if p.stat().st_mtime >= not_before]
    if not cands:
        raise SystemExit(f"no {pattern} newer than fold start found in {path}")
    return max(cands, key=lambda p: p.stat().st_mtime)


def best_epoch(fthypt: Path) -> dict:
    # min finite loss, not the is_best flag: with 0-trade epochs (loss sentinel
    # 100000) freqtrade marks is_best=False on every line
    cands = []
    for line in fthypt.read_text().splitlines():
        d = json.loads(line)
        loss = d.get("loss")
        if loss is not None and math.isfinite(loss):
            cands.append(d)
    if not cands:
        raise SystemExit(f"no finite-loss epochs in {ftthypt}")
    return min(cands, key=lambda d: d["loss"])


def backtest_stats(zip_path: Path, strategy: str) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        d = json.load(z.open(f"{zip_path.stem}.json"))
    st = d["strategy"][strategy]
    # trades open at window end are force-closed at the last candle — their
    # realized P&L is truncated at the boundary price, so these exits must be
    # excluded from fold-quality judgments (see summarize() clean-fold rules)
    st["_force_exit"] = sum(1 for t in st.get("trades", []) if t.get("exit_reason") == "force_exit")
    return st


def metrics_from(stats: dict) -> dict:
    # profit_per_day normalizes across folds of different lengths (Masters' mean-return analogue)
    days = max(float(stats.get("backtest_days") or 0), 1.0)
    out = {"profit_per_day": float(stats.get("profit_total") or 0.0) / days}
    for m in ("profit_factor", "sharpe"):
        v = stats.get(m)
        out[m] = float(v) if v is not None else math.nan
    return out


def fmt(v: float) -> str:
    return f"{v:16.4f}" if v == v else f"{'nan':>16}"


def main() -> None:
    args = parse_args()
    start, end = parse_timerange(args.timerange)
    folds = build_folds(start, end, args.is_days, args.oos_days)
    if not folds:
        raise SystemExit("no folds fit the timerange (need is_days + 30d of OOS)")
    out = Path(args.out or (Path(args.userdir) / "walkforward_results" / args.strategy))
    out.mkdir(parents=True, exist_ok=True)

    print(f"walkforward {args.strategy}: {len(folds)} folds, is={args.is_days}d oos={args.oos_days}d -> {out}")
    for i, (a, b, c) in enumerate(folds):
        print(f"  fold {i}: IS {a:%Y%m%d}-{b:%Y%m%d}  OOS {b:%Y%m%d}-{c:%Y%m%d}")

    folds_csv = out / "folds.csv"
    done: set[int] = set()
    if folds_csv.exists():
        with folds_csv.open() as f:
            done = {int(r["fold"]) for r in csv.DictReader(f) if r["fold"]}

    params_file = Path(args.userdir) / "strategies" / f"{args.strategy}.json"
    backup = out / "params_backup.json"
    had_params = params_file.exists()
    if had_params and not backup.exists():
        shutil.copy2(params_file, backup)

    try:
        for i, (is_start, is_end, oos_end) in enumerate(folds):
            if i in done:
                print(f"fold {i}: done, skipping")
                continue
            tag = f"fold_{i:03d}"
            t0 = time.time()
            is_tr = f"{is_start:%Y%m%d}-{is_end:%Y%m%d}"
            oos_tr = f"{is_end:%Y%m%d}-{oos_end:%Y%m%d}"
            print(f"fold {i}: IS {is_tr}  OOS {oos_tr}")

            if args.no_optimize:
                # pinned-params rolling validation: IS metrics come from a plain backtest
                bt_is = ["freqtrade", "backtesting", "-c", args.config, "--userdir", args.userdir,
                         "--strategy", args.strategy, "--timerange", is_tr, "--cache", "none",
                         "--export-directory", str(out.resolve())]
                run_cmd(bt_is, out / f"{tag}_is.log")
                st = backtest_stats(newest(out, "backtest-result-*.zip", t0), args.strategy)
                is_m = metrics_from(st)
                is_trades = st.get("total_trades")
                is_loss = 0.0
                params_dict = {}
            else:
                hyper = ["freqtrade", "hyperopt", "-c", args.config, "--userdir", args.userdir,
                         "--strategy", args.strategy, "--hyperopt-loss", args.hyperopt_loss,
                         "--timerange", is_tr, "-e", str(args.epochs)]
                if args.spaces:
                    hyper += ["--spaces", args.spaces]
                if args.jobs is not None:
                    hyper += ["-j", str(args.jobs)]
                run_cmd(hyper, out / f"{tag}_hyperopt.log")

                # files are named strategy_<Strategy>_<ts>.fthypt; the literal underscore in
                # "_*." keeps this from matching sibling names like strategy_LTHMFreqai_*
                ep = best_epoch(newest(Path(args.userdir) / "hyperopt_results", f"*{args.strategy}_*.fthypt", t0))
                is_m = metrics_from(ep["results_metrics"])
                is_trades = ep["results_metrics"].get("total_trades")
                is_loss = ep["loss"]
                params_dict = ep.get("params_dict", {})
                if not is_trades:
                    print(f"  warning: best IS epoch has 0 trades (loss={ep['loss']:.0f}) — "
                          f"hyperopt found nothing tradeable in this IS window")
            if params_file.exists():
                shutil.copy2(params_file, out / f"{tag}_params.json")

            bt = ["freqtrade", "backtesting", "-c", args.config, "--userdir", args.userdir,
                  "--strategy", args.strategy, "--timerange", oos_tr, "--cache", "none",
                  "--export-directory", str(out.resolve())]
            run_cmd(bt, out / f"{tag}_backtest.log")
            oos_zip = newest(out, "backtest-result-*.zip", t0)
            st = backtest_stats(oos_zip, args.strategy)
            oos_m = metrics_from(st)

            row = {
                "fold": i, "is_start": f"{is_start:%Y%m%d}", "is_end": f"{is_end:%Y%m%d}",
                "oos_start": f"{is_end:%Y%m%d}", "oos_end": f"{oos_end:%Y%m%d}",
                **{f"is_{m}": is_m[m] for m in METRICS},
                "is_trades": is_trades, "is_loss": is_loss,
                **{f"oos_{m}": oos_m[m] for m in METRICS},
                "oos_trades": st.get("total_trades"),
                "oos_force": st.get("_force_exit"),
                "params": json.dumps(params_dict),
                "oos_zip": oos_zip.name,
            }
            new_file = not folds_csv.exists()
            with folds_csv.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if new_file:
                    w.writeheader()
                w.writerow(row)
            print(f"fold {i} done in {(time.time() - t0) / 60:.1f} min: "
                  f"IS pf={is_m['profit_factor']:.3f} OOS pf={oos_m['profit_factor']:.3f}")
    finally:
        if had_params and params_file.exists():
            shutil.copy2(backup, params_file)
            print("restored original params file")
        elif not had_params and params_file.exists():
            print("note: no params file existed before the run; last fold's export left in place")

    summarize(folds_csv, out)
    write_combined_backtest(args, out)


def write_combined_backtest(args: argparse.Namespace, out: Path) -> None:
    """Stitch all folds' OOS trades into one standard backtest result zip in
    <userdir>/backtest_results/ (backtest-result-<ts>_WF.zip + .meta.json) so
    the whole walkforward shows up in the webserver as a single backtest.
    Stats are recomputed by freqtrade's own generate_strategy_stats, so the
    result is indistinguishable from a normal backtest export."""
    # freqtrade modules need the repo root on sys.path (script may run from
    # anywhere); imports are local so the walkforward runner itself stays
    # stdlib-only until the combined output is requested.
    ft_root = Path(__file__).resolve().parent.parent
    if str(ft_root) not in sys.path:
        sys.path.insert(0, str(ft_root))
    from freqtrade.data.btanalysis import load_backtest_data
    from freqtrade.enums import RunMode
    from freqtrade.optimize.optimize_reports.bt_storage import store_backtest_results
    from freqtrade.optimize.optimize_reports.optimize_reports import (
        generate_strategy_comparison,
        generate_strategy_stats,
    )

    folds_csv = out / "folds.csv"
    with folds_csv.open() as f:
        rows = list(csv.DictReader(f))

    dfs = []
    n_missing = 0
    for r in rows:
        name = r.get("oos_zip")
        zp = out / name if name else None
        if not name or not zp.is_file():
            n_missing += 1
            print(f"combined: fold {r['fold']}: OOS zip {name or '(none recorded)'} missing, skipped")
            continue
        dfs.append(load_backtest_data(zp, args.strategy))
    if not dfs or not any(not d.empty for d in dfs):
        print("combined: no fold OOS trades available, skipping combined backtest")
        return

    import pandas as pd

    trades = pd.concat([d for d in dfs if not d.empty], ignore_index=True)
    trades = trades.sort_values("open_date").reset_index(drop=True)
    trades["trade_id"] = range(1, len(trades) + 1)

    from freqtrade.configuration import setup_utils_configuration
    from freqtrade.resolvers import StrategyResolver

    config = setup_utils_configuration(
        {"config": [args.config], "user_data_dir": args.userdir}, RunMode.UTIL_NO_EXCHANGE
    )
    config["strategy"] = args.strategy
    StrategyResolver.load_strategy(config)

    start_balance = config["dry_run_wallet"] * config.get("tradable_balance_ratio", 1.0)
    min_date = datetime.strptime(rows[0]["oos_start"], "%Y%m%d")
    max_date = datetime.strptime(rows[-1]["oos_end"], "%Y%m%d")
    now_ms = int(time.time() * 1000)
    content = {
        "results": trades,
        "config": config,
        "locks": [],
        "final_balance": start_balance + float(trades["profit_abs"].sum()),
        "backtest_start_time": now_ms,
        "backtest_end_time": now_ms,
        "rejected_signals": 0,
        "timedout_entry_orders": 0,
        "timedout_exit_orders": 0,
        "canceled_trade_entries": 0,
        "canceled_entry_orders": 0,
        "replaced_entry_orders": 0,
        "run_id": f"walkforward-{now_ms}",
    }
    strat_stats = generate_strategy_stats(
        sorted(trades["pair"].unique()), args.strategy, content,
        min_date, max_date, market_change=0.0,
    )
    comparison = generate_strategy_comparison({args.strategy: strat_stats})
    notes = f"Walkforward OOS: {len(dfs)} folds stitched, IS {args.is_days}d / OOS {args.oos_days}d"
    if n_missing:
        notes += f", {n_missing} folds missing"
    metadata = {
        args.strategy: {
            "run_id": content["run_id"],
            "backtest_start_time": now_ms,
            "timeframe": config["timeframe"],
            "timeframe_detail": config.get("timeframe_detail", None),
            "backtest_start_ts": int(min_date.timestamp()),
            "backtest_end_ts": int(max_date.timestamp()),
            "notes": notes,
        }
    }
    strategy_file = Path(args.userdir) / "strategies" / f"{args.strategy}.py"
    zip_path = store_backtest_results(
        config,
        {
            "strategy": {args.strategy: strat_stats},
            "strategy_comparison": comparison,
            "metadata": metadata,
        },
        f"{datetime.now():%Y-%m-%d_%H-%M-%S}_WF",
        strategy_files={args.strategy: str(strategy_file)} if strategy_file.is_file() else None,
    )
    print(f"combined: {len(dfs)} folds, {len(trades)} trades -> {zip_path} (webserver)")


def is_clean_fold(r: dict, min_is_trades: int = 5, max_force_share: float = 0.25) -> bool:
    """A fold only carries signal if hyperopt had enough IS trades to pick
    params meaningfully and the OOS backtest wasn't mostly window-truncated
    (force_exit trades are closed at boundary price, not by strategy logic)."""
    try:
        it = int(r.get("is_trades") or 0)
        ot = int(r.get("oos_trades") or 0)
        fe = int(r.get("oos_force") or 0)
    except (ValueError, TypeError):
        return False
    return it >= min_is_trades and ot > 0 and fe <= max_force_share * ot


def summarize(folds_csv: Path, out: Path) -> None:
    with folds_csv.open() as f:
        rows = [r for r in csv.DictReader(f)]

    lines = [f"{'fold':>4}  {'IS':<21} {'OOS':<21}"
             + "".join(f" {'IS_' + m:>16}" for m in METRICS)
             + "".join(f" {'OOS_' + m:>16}" for m in METRICS)
             + f" {'IS_tr':>6} {'OOS_tr':>6} {'loss':>9}"]
    for r in rows:
        is_r = f"{r['is_start']}-{r['is_end']}"
        oos_r = f"{r['oos_start']}-{r['oos_end']}"
        lines.append(f"{r['fold']:>4}  {is_r:<21} {oos_r:<21}"
                     + "".join(f" {fmt(float(r['is_' + m]))}" for m in METRICS)
                     + "".join(f" {fmt(float(r['oos_' + m]))}" for m in METRICS)
                     + f" {r['is_trades']:>6} {r['oos_trades']:>6} {float(r['is_loss']):>9.4f}")

    def means(rows: list[dict], metric: str, side: str) -> float:
        vals = [float(r[f"{side}_{metric}"]) for r in rows if r[f"{side}_{metric}"] not in ("", "nan")]
        return sum(vals) / len(vals) if vals else math.nan

    for m in METRICS:
        mi = means(rows, m, "is")
        mo = means(rows, m, "oos")
        bias = mi - mo
        ratio = mo / mi if mi else math.nan
        lines.append(f"Mean IS {m}={mi:.4f}  OOS={mo:.4f}  Bias(IS-OOS)={bias:.4f}  OOS/IS={ratio:.3f}")

    clean = [r for r in rows if is_clean_fold(r)]
    excluded = len(rows) - len(clean)
    if clean and excluded:
        for m in METRICS:
            mi = means(clean, m, "is")
            mo = means(clean, m, "oos")
            bias = mi - mo
            ratio = mo / mi if mi else math.nan
            lines.append(f"Clean {m} (folds missing <5 IS trades / 0 OOS trades / >25% "
                         f"force_exit excluded; {excluded} of {len(rows)}): "
                         f"IS={mi:.4f}  OOS={mo:.4f}  Bias={bias:.4f}  OOS/IS={ratio:.3f}  OOS ppd={mo:.4f}")

    text = "\n".join(lines)
    print("\n" + text)
    (out / "summary.txt").write_text(text + "\n")


if __name__ == "__main__":
    main()
