"""Compatibility tests for the legacy RC4 transformation."""

import pytest

from net_connector.network import rc4_hex


def test_rc4_hex_matches_legacy_password_ciphertext():
    assert rc4_hex("password", "1700000000000") == "78cdea3da257edba"


def test_rc4_hex_rejects_an_empty_key():
    with pytest.raises(ValueError, match="key"):
        rc4_hex("password", "")
