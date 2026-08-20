

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


class TestConfiguredParams:
    """The parameter playground fetches /api/params/<plan> on every page load.
    The function behind it was never written, so the endpoint raised NameError
    and took the whole dashboard request down with it."""

    def test_it_returns_what_the_plan_is_running(self):
        from orbot.ui.server import configured_params
        got = configured_params("goldm-2h")
        assert got["ut_key"] == 3.0          # the chart legend value, not 2.0
        assert got["ut_atr_period"] == 1
        assert got["hma_length"] == 31
        assert got["timeframe"] == "2h"

    def test_every_key_the_ui_reads_is_present(self):
        """index.html indexes these directly; a missing one is undefined in a
        form field rather than an error anyone sees."""
        from orbot.ui.server import configured_params
        got = configured_params("silver100-2h")
        for key in ("ut_key", "ut_atr_period", "hma_length", "orb_session",
                    "timeframe", "use_heikin_ashi"):
            assert key in got, f"the playground reads {key} and it is missing"

    def test_an_unknown_plan_returns_an_error_not_an_exception(self):
        from orbot.ui.server import configured_params
        assert "error" in configured_params("does-not-exist")
