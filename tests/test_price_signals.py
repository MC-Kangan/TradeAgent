from datetime import UTC, datetime, timedelta

import pytest

from trade_research.providers import PricePoint
from trade_research.signals import moving_average_crossover


def _prices(values: list[float]) -> tuple[PricePoint, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return tuple(
        PricePoint(
            observed_at=start + timedelta(days=index),
            close=value,
            source="yahoo",
            provenance={"reference": f"row-{index}"},
        )
        for index, value in enumerate(values)
    )


def test_moving_average_crossover_emits_only_transition_signals() -> None:
    prices = _prices([10, 10, 10, 9, 8, 12, 14, 8, 6])

    instructions = moving_average_crossover(prices, fast_window=2, slow_window=3)

    assert [(item.observed_at, item.direction) for item in instructions] == [
        (prices[3].observed_at, "short"),
        (prices[5].observed_at, "long"),
        (prices[7].observed_at, "short"),
    ]


def test_moving_average_crossover_is_causal() -> None:
    prices = _prices([10, 10, 9, 8, 12, 14, 8])

    before_last_bar = moving_average_crossover(
        prices[:-1], fast_window=2, slow_window=3
    )
    after_last_bar = moving_average_crossover(prices, fast_window=2, slow_window=3)

    assert after_last_bar[: len(before_last_bar)] == before_last_bar


@pytest.mark.parametrize(
    ("fast_window", "slow_window"),
    [(0, 3), (2, 2), (4, 3)],
)
def test_moving_average_crossover_rejects_invalid_windows(
    fast_window: int, slow_window: int
) -> None:
    with pytest.raises(ValueError, match="0 < fast_window < slow_window"):
        moving_average_crossover(
            _prices([10, 11, 12, 13]),
            fast_window=fast_window,
            slow_window=slow_window,
        )
