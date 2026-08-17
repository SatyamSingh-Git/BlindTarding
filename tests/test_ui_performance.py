

class TestOvernightPlans:
    """A 2-hour plan holds for days; nothing may force it flat at a cutoff."""

    def test_pair_trades_accepts_no_square_off(self):
        # Regression: making square_off_time optional left `ts.time() >= None`
        # on this path, which took the whole dashboard down with a TypeError.
        from orbot.ui.performance import pair_trades

        class _R:
            timestamps = []
            closes = []
            buy = []
            sell = []

        assert pair_trades(_R(), square_off=None) == []
