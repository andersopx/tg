from types import SimpleNamespace
from polymarket_client import PolymarketClient


def test_parse_orderbook_object_asks_bids():
    book = SimpleNamespace(
        asks=[SimpleNamespace(price='0.40', size='2.5'), SimpleNamespace(price='0.35', size='1')],
        bids=[SimpleNamespace(price='0.30', size='3')],
    )
    asks, bids = PolymarketClient._parse_orderbook_levels(book)
    assert asks == [(0.35, 1.0), (0.40, 2.5)]
    assert bids == [(0.30, 3.0)]


def test_parse_orderbook_dict_sells_buys_aliases():
    book = {
        'sells': [{'price': '0.42', 'size': '2'}, {'price': '0.41', 'size': '1'}],
        'buys': [{'price': '0.39', 'size': '3'}],
    }
    asks, bids = PolymarketClient._parse_orderbook_levels(book)
    assert asks == [(0.41, 1.0), (0.42, 2.0)]
    assert bids == [(0.39, 3.0)]


def test_parse_orderbook_missing_asks_returns_empty_not_exception():
    book = {'bids': [{'price': '0.1', 'size': '5'}]}
    asks, bids = PolymarketClient._parse_orderbook_levels(book)
    assert asks == []
    assert bids == [(0.1, 5.0)]
