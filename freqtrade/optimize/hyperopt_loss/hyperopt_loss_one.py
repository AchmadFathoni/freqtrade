import numpy as np
from pandas import DataFrame

from freqtrade.data.metrics import calculate_max_drawdown
from freqtrade.optimize.hyperopt import IHyperOptLoss


# -----------------------------
# Tunable Targets
# -----------------------------
MIN_TRADE_COUNT = 500
MAX_ACCEPTABLE_DRAWDOWN = 0.18   # 18%
PROFIT_FACTOR_CAP = 3.0
EPSILON = 1e-6


class OneHyperOptLoss(IHyperOptLoss):

    @staticmethod
    def hyperopt_loss_function(
        *,
        results: DataFrame,
        trade_count: int,
        starting_balance: float,
        **kwargs,
    ) -> float:
        """
        Lower loss is better.

        Rewards:
        - Higher Profit Factor
        - Enough trade count

        Penalties:
        - Drawdown above threshold
        """

        # ---------------------------------
        # 1. Profit Factor Reward
        # ---------------------------------
        gross_profit = results.loc[
            results["profit_abs"] > 0, "profit_abs"
        ].sum()

        gross_loss = abs(
            results.loc[
                results["profit_abs"] < 0, "profit_abs"
            ].sum()
        )

        profit_factor = gross_profit / (gross_loss + EPSILON)
        capped_profit_factor = min(profit_factor, PROFIT_FACTOR_CAP)

        profit_factor_reward = np.log1p(capped_profit_factor)

        # ---------------------------------
        # 2. Trade Count Reward
        # ---------------------------------
        trade_count_reward = 1.0

        if trade_count < MIN_TRADE_COUNT:
            trade_count_reward = trade_count / MIN_TRADE_COUNT
            trade_count_reward = max(trade_count_reward, 0.10)

        # ---------------------------------
        # 3. Drawdown Penalty
        # ---------------------------------
        try:
            drawdown_stats = calculate_max_drawdown(
                results,
                starting_balance=starting_balance,
                value_col="profit_abs",
            )
            relative_drawdown = drawdown_stats.relative_account_drawdown

        except ValueError:
            relative_drawdown = 0.0

        drawdown_excess = max(
            0.0,
            relative_drawdown - MAX_ACCEPTABLE_DRAWDOWN
        )

        drawdown_penalty = (
            drawdown_excess / MAX_ACCEPTABLE_DRAWDOWN
        ) ** 2

        # ---------------------------------
        # 4. Final Score
        # ---------------------------------
        total_reward = (
            profit_factor_reward *
            trade_count_reward
        )

        loss = -total_reward + drawdown_penalty

        return loss
