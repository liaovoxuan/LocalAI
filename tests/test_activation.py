from local_ai import (
    DEFAULT_CONFIG,
    EDITION_PRO,
    EDITION_STANDARD,
    EDITION_ULTRA,
    apply_edition_config,
    validate_activation_code,
)


def test_activation_code_rule():
    assert validate_activation_code("9999990")
    assert validate_activation_code("8888886")
    assert not validate_activation_code("9999999")
    assert not validate_activation_code("999999")
    assert not validate_activation_code("abcdefg")


def test_invalid_saved_activation_falls_back():
    config = DEFAULT_CONFIG.copy()
    config.update({"edition": EDITION_ULTRA, "activation_code": "1234567"})
    apply_edition_config(config)
    assert config["edition"] == EDITION_STANDARD


def test_valid_pro_and_ultra_activation():
    for edition, code in ((EDITION_PRO, "8888886"), (EDITION_ULTRA, "9999990")):
        config = DEFAULT_CONFIG.copy()
        config.update({"edition": edition, "activation_code": code})
        apply_edition_config(config)
        assert config["edition"] == edition
