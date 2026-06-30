from trading_agent.config import Settings
from trading_agent.risk import OrderDraft, RiskManager


def test_blocks_live_orders_when_switch_off():
    settings = Settings(kis_env="prod", allow_live_orders=False, max_order_krw=50_000)
    check = RiskManager(settings).check_order(OrderDraft("005930", "buy", 1, 50_000, dry_run=False))
    assert not check.allowed
    assert any("실주문 잠금" in item for item in check.blocks)


def test_paper_order_not_blocked_by_live_lock():
    # paper = fake money → no lock needed (only prod real orders are lock-gated).
    settings = Settings(kis_env="paper", allow_live_orders=False, max_order_krw=50_000)
    check = RiskManager(settings).check_order(OrderDraft("005930", "buy", 1, 50_000, dry_run=False))
    assert check.allowed
    assert not any("실주문 잠금" in item for item in check.blocks)


def test_dry_run_not_blocked_by_real_order_lock():
    settings = Settings(kis_env="paper", allow_live_orders=False, max_order_krw=50_000)
    check = RiskManager(settings).check_order(OrderDraft("005930", "buy", 1, 50_000, dry_run=True))
    assert not any("실주문 잠금" in item for item in check.blocks)


def test_blocks_order_above_limit():
    settings = Settings(kis_env="mock", max_order_krw=50_000)
    check = RiskManager(settings).check_order(OrderDraft("005930", "buy", 2, 50_000))
    assert not check.allowed
    assert any("1회 한도" in item for item in check.blocks)
