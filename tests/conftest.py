import logging
import sys

import pytest

from dex.engine import MatchingEngine


@pytest.fixture(autouse=True)
def configure_logging():
    """单测日志：打印关键输入、订单状态迁移与判定依据。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root = logging.getLogger("dex")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    yield
    root.removeHandler(handler)


@pytest.fixture
def engine(tmp_path):
    e = MatchingEngine(data_path=str(tmp_path / "state.json"),
                       admin_token="test-admin")
    e.deposit("alice", "USD", "100000")
    e.deposit("alice", "BTC", "10")
    e.deposit("bob", "USD", "100000")
    e.deposit("bob", "BTC", "10")
    e.set_risk_params("test-admin", "BTC-USD", reference_price="100")
    return e
