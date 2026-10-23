"""不调用回测引擎，按逐笔成交重新建账并核查历史状态和容量。"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .run import DATA, digest, load_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    data, coverage, _ = load_data()
    checks = 0
    failures = []

    def check(name, condition, detail=""):
        nonlocal checks
        checks += 1
        if not bool(condition):
            failures.append({"check": name, "detail": str(detail)})

    columns = data.symbols
    amount = data.frames["amt"].where(data.frames["amt"] > 0).rolling(20, min_periods=20).mean().shift(1)
    experiments = pd.read_csv(args.results / "experiments.csv").experiment.tolist()
    experiments += ["train_"+method for method in json.loads((args.results/"selection.json").read_text())["candidates"]]
    summaries = []
    for experiment in experiments:
        directory = args.results / experiment
        config = json.loads((directory / "config.json").read_text())
        daily = pd.read_csv(directory / "equity.csv", parse_dates=["date"]).set_index("date")
        trades = pd.read_csv(directory / "trades.csv", parse_dates=["date", "signal_date"])
        selected = pd.read_csv(directory / "selections.csv", parse_dates=["signal_date"])
        config_stocks = json.loads((directory/"strategy.json").read_text())
        cash = config["initial_cash"]
        shares = pd.Series(0., index=columns)
        last_factor = pd.Series(np.nan, index=columns)
        marks = pd.Series(np.nan, index=columns)
        max_ratio = 0.
        for row, record in daily.iterrows():
            factors = data.frames["cumadj"].loc[row]
            good_factor = factors.gt(0) & factors.notna()
            ratio = (factors / last_factor).where(good_factor & last_factor.notna(), 1)
            shares *= ratio
            marks /= ratio
            last_factor.loc[good_factor] = factors.loc[good_factor]
            today = trades.loc[trades.date == row]
            sellable = shares.copy()
            fees = slip = 0.
            buys_today = set()
            for trade in today.itertuples():
                symbol = trade.symbol
                opening = data.frames["open"].at[row, symbol]
                isbuy = trade.side == "buy"
                direction = 1 if isbuy else -1
                quantity = trade.quantity
                expected_price = opening * (1 + direction * config["slippage"])
                check("strict_prior_signal", trade.signal_date < row, trade)
                check("raw_open", np.isclose(trade.open_price, opening, atol=1e-8, rtol=0), trade)
                check("fill_slippage", np.isclose(trade.fill_price, expected_price, atol=1e-8, rtol=0), trade)
                check("commission", np.isclose(trade.commission, trade.notional*.0001, atol=1e-5, rtol=0), trade)
                check("sell_only_stamp", np.isclose(trade.sell_tax, 0 if isbuy else trade.notional*.0005, atol=1e-5, rtol=0), trade)
                check("trade_status", data.frames["trade_status"].at[row, symbol] == 1, trade)
                reference = data.frames["preclose"].at[row, symbol]
                rate = .2 if symbol.startswith(("300", "301", "302", "688", "689")) else .1
                if rate == .1 and row < pd.Timestamp("2026-07-06") and data.frames["is_st"].at[row, symbol] == 1:
                    rate = .05
                bound = np.floor(reference*(1+rate if isbuy else 1-rate)*100+.5)/100
                check("correct_direction_price_limit", opening < bound-1e-9 if isbuy else opening > bound+1e-9, trade)
                if isbuy:
                    check("buy_historical_nonst", data.frames["is_st"].at[row, symbol] == 0, trade)
                    if config.get("board_lots") and symbol.startswith(("688", "689")):
                        check("star_buy_min_200_and_integer", quantity >= 200 and np.isclose(quantity, round(quantity), atol=1e-6, rtol=0), trade)
                    else:
                        check("buy_lot_100", np.isclose(quantity/100, round(quantity/100), atol=1e-6, rtol=0), trade)
                    buys_today.add(symbol)
                else:
                    check("no_short_and_tplus1", quantity <= sellable[symbol]+1e-5 and symbol not in buys_today, trade)
                    sellable[symbol] -= quantity
                ratio_adv = quantity * opening / amount.at[row, symbol]
                check("prior_ADV_capacity", ratio_adv <= config["max_participation"]+1e-9, trade)
                max_ratio = max(max_ratio, ratio_adv)
                cash -= direction*trade.notional + trade.commission + trade.sell_tax
                shares[symbol] += direction*quantity
                check("cash_after_trade", np.isclose(cash, trade.cash_after, atol=.02, rtol=0), trade)
                check("nonnegative_cash", cash >= -.02, trade)
                fees += trade.commission+trade.sell_tax
                slip += quantity*abs(trade.fill_price-opening)
            opening = data.frames["open"].loc[row]
            valid_open = opening.gt(0) & good_factor & data.frames["trade_status"].loc[row].eq(1)
            marks.loc[valid_open] = opening.loc[valid_open]
            closing = data.frames["close"].loc[row]
            valid_close = closing.gt(0) & good_factor
            marks.loc[valid_close] = closing.loc[valid_close]
            held = shares.abs() > 1e-10
            check("held_mark_is_known", np.isfinite(marks.loc[held]).all(), (experiment, row))
            value = float((shares.loc[held] * marks.loc[held]).sum())
            check("daily_equity", np.isclose(cash+value, record.equity, atol=.05, rtol=0), (experiment, row))
            check("daily_cash", np.isclose(cash, record.cash, atol=.05, rtol=0), (experiment, row))
            check("daily_fees", np.isclose(fees, record.fees, atol=.001, rtol=0), (experiment, row))
            check("daily_slippage", np.isclose(slip, record.slippage_cost, atol=.001, rtol=0), (experiment, row))
            check("daily_holdings_count", (shares > 1e-8).sum() == record.positions, (experiment, row))
        for signal, picks in selected.groupby("signal_date"):
            symbols = picks.symbol.tolist()
            check("signal_nonst", data.frames["is_st"].loc[signal, symbols].eq(0).all(), (experiment, signal))
            check("signal_liquidity", data.frames["amt"].loc[:signal, symbols].tail(20).mean().ge(config_stocks["min_adv"]-1e-6).all(), (experiment, signal))
            check("signal_equal_weight", np.allclose(picks.weight, 1/len(picks)), (experiment, signal))
            raw = data.frames["close"].loc[:signal]
            p = (raw.tail(121) * data.frames["cumadj"].loc[raw.tail(121).index]).where(lambda frame: frame > 0)
            daily_returns = p.pct_change(fill_method=None).tail(60)
            vol = daily_returns.std(ddof=1)
            momentum = p.iloc[-21]/p.iloc[-121]-1
            a = data.frames["amt"].loc[:signal].tail(20)
            eligible = (data.frames["is_st"].loc[signal].eq(0) & raw.iloc[-1].gt(0)
                & data.frames["cumadj"].loc[signal].gt(0) & raw.gt(0).sum().ge(120)
                & daily_returns.notna().all() & np.isfinite(daily_returns).all() & np.isfinite(vol)
                & np.isfinite(momentum) & a.gt(0).all() & a.mean().ge(config_stocks["min_adv"]))
            pool = columns[eligible]
            method = config_stocks["method"]
            if method == "equal_weight":
                expected_symbols = pool.tolist()
            else:
                if method == "lowvol60":
                    scores = -vol.loc[pool]
                elif method == "momentum120":
                    scores = momentum.loc[pool]
                else:
                    scores = ((-vol.loc[pool]).rank(pct=True)+momentum.loc[pool].rank(pct=True))/2
                expected_symbols = scores.sort_index().sort_values(ascending=False, kind="stable").head(config_stocks["k"]).index.tolist()
                check("factor_values", np.allclose(picks.score, scores.loc[symbols], atol=1e-12), (experiment, signal))
            check("independent_full_market_ranking", symbols == expected_symbols, (experiment, signal))
        check("first_day_build", ((trades.date == daily.index[0]) & trades.side.eq("buy")).any(), experiment)
        # 清仓是否完成独立记录，不把不可清仓账户当成数据审计失败或强制抹去。
        summaries.append({"experiment": experiment, "trades": len(trades),
            "end_positions": int((shares > 1e-8).sum()), "max_trade_over_prior_adv": float(max_ratio),
            "reconstructed_final_cash": float(cash), "reconstructed_final_equity": float(cash+value)})
        print("audited", experiment, "checks", checks, "failures", len(failures), flush=True)
    raw_manifest = json.loads((args.results/"manifest.json").read_text())
    from .run import BASE
    for file, expected in raw_manifest["source_code"].items():
        check("source_sha256", digest(BASE/file) == expected, file)
    for field, expected in raw_manifest["raw_inputs"].items():
        check("input_sha256", digest(BASE/"feathers"/(field+".feather")) == expected, field)
    for file, expected in raw_manifest["external_inputs"].items():
        check("external_input_sha256", digest(DATA/file) == expected, file)
    for entry in pd.read_csv(args.results/"status_file_hashes.csv").itertuples():
        check("status_file_sha256", digest(DATA/"historical_status"/(entry.symbol+".feather")) == entry.sha256, entry.symbol)
    result = {"checks": checks, "passed": not failures, "failures": failures[:100], "failure_count": len(failures),
        "experiments": summaries, "audit_source_sha256": digest(Path(__file__))}
    (args.results / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != "experiments"}, ensure_ascii=False), flush=True)
    if failures:
        raise SystemExit("独立审计失败，请检查 audit.json")


if __name__ == "__main__":
    main()
