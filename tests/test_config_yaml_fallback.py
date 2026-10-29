from pathlib import Path

import pytest

from backend.app.core import config


def test_fallback_parser_reads_the_real_config_file():
    text = (Path(__file__).resolve().parents[1] / "config" / "neuravex.yaml").read_text()
    parsed = config._parse_simple_yaml(text)
    assert parsed["trading"] == {"mode": "paper"}
    assert parsed["risk"]["max_drawdown"] == 0.10
    assert parsed["risk"]["max_open_positions"] == 5
    assert parsed["strategy"]["minimum_confidence"] == 0.70
    assert parsed["execution"] == {"slippage_limit": 0.005}
    if config.yaml is not None:
        assert parsed == config.yaml.safe_load(text)


def test_fallback_parser_scalars_and_nesting():
    parsed = config._parse_simple_yaml(
        "a:\n  b:\n    c: true\n  d: 'x y'\ne: 3\n# comment\nf: ~\n"
    )
    assert parsed == {"a": {"b": {"c": True}, "d": "x y"}, "e": 3, "f": None}


def test_fallback_parser_refuses_lists_instead_of_misreading():
    with pytest.raises(ValueError):
        config._parse_simple_yaml("symbols:\n  - BTC\n")


def test_settings_load():
    settings = config.load_settings()
    assert settings.risk.max_drawdown == 0.10
