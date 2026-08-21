"""
ProfitFactorHyperOptLoss

Objective is the profit factor (winning profit / losing profit) alone,
with a penalty when the trade count falls below TARGET_TRADE_AMOUNT
so the optimizer cannot degenerate to a handful of lucky trades.
"""

import numpy as np
from pandas import DataFrame

from freqtrade.optimize.hyperopt import IHyperOptLoss


# Minimum number of trades before the penalty kicks in
TARGET_TRADE_AMOUNT = 50


class ProfitFactorHyperOptLoss(IHyperOptLoss):
    @staticmethod
    def hyperopt_loss_function(
        *,
        results: DataFrame,
        trade_count: int,
        starting_balance: float,
        **kwargs,
    ) -> float:
        winning_profit = results.loc[results["profit_abs"] > 0, "profit_abs"].sum()
        losing_profit = results.loc[results["profit_abs"] < 0, "profit_abs"].sum()
        profit_factor = winning_profit / (abs(losing_profit) + 1e-6)

        trade_count_penalty = 1.0
        if trade_count < TARGET_TRADE_AMOUNT:
            trade_count_penalty = 1 - (abs(trade_count - TARGET_TRADE_AMOUNT) / TARGET_TRADE_AMOUNT)
            trade_count_penalty = max(trade_count_penalty, 0.1)

        return -np.log(profit_factor + 1e-6) * trade_count_penalty
