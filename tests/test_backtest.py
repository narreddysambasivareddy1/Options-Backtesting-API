from app.services.backtest import atm_strike, calculate_report


def test_atm_strike():
    assert atm_strike(24123, 50) == 24100
    assert atm_strike(24126, 50) == 24150


def test_report():
    report = calculate_report(
        [{"net_pnl": 100}, {"net_pnl": -40}], starting_capital=1000
    )
    assert report["trade_count"] == 2
    assert report["gross_net_pnl"] == 60
    assert report["ending_capital"] == 1060
    assert report["win_rate_percent"] == 50
