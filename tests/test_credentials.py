from unittest.mock import Mock, patch

from baomiguan.credentials import CredentialVault


def test_only_token_is_saved_to_dedicated_credential_entry():
    vault = CredentialVault()
    backend = Mock()
    with patch.object(vault, "_backend", return_value=backend):
        assert vault.save("session-token") is True
    backend.set_password.assert_called_once_with("Baomiguan", "official-session", "session-token")
    assert vault.last_error == ""


def test_unavailable_backend_never_falls_back_to_a_file():
    vault = CredentialVault()
    with patch.object(vault, "_backend", side_effect=RuntimeError("private backend details")):
        with patch("builtins.open") as open_file:
            assert vault.save("private-token") is False
            assert vault.load() is None
            vault.clear()
    open_file.assert_not_called()
    assert "private-token" not in vault.last_error
    assert "private backend details" not in vault.last_error
    assert vault.last_error


def test_load_and_clear_use_same_entry():
    vault = CredentialVault()
    backend = Mock()
    backend.get_password.return_value = "token"
    with patch.object(vault, "_backend", return_value=backend):
        assert vault.load() == "token"
        vault.clear()
    backend.delete_password.assert_called_once_with(vault.SERVICE, vault.ACCOUNT)


def test_clearing_absent_token_succeeds():
    vault = CredentialVault()
    backend = Mock()
    backend.get_password.return_value = None
    with patch.object(vault, "_backend", return_value=backend):
        vault.clear()
    backend.delete_password.assert_not_called()
    assert vault.last_error == ""


def test_empty_tokens_are_not_persisted():
    vault = CredentialVault()
    with patch.object(vault, "_backend") as backend:
        assert vault.save("") is False
    backend.assert_not_called()


def test_non_windows_platform_fails_safely():
    vault = CredentialVault()
    with patch("baomiguan.credentials.sys.platform", "linux"):
        assert vault.save("private") is False
    assert "Windows" in vault.last_error
