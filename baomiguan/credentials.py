"""Persist session tokens only in Windows Credential Manager."""

import sys


class CredentialVault:
    SERVICE = "Baomiguan"
    ACCOUNT = "official-session"

    def __init__(self):
        self.last_error = ""

    def _backend(self):
        if sys.platform != "win32":
            raise RuntimeError("当前系统不支持 Windows 凭据管理器；本次登录仅保留在内存中")
        # Select the Windows backend explicitly. Never accept plaintext or chained fallbacks.
        from keyring.backends.Windows import WinVaultKeyring

        backend = WinVaultKeyring()
        if backend.priority <= 0:
            raise RuntimeError("Windows 凭据管理器不可用；本次登录仅保留在内存中")
        return backend

    def save(self, token):
        self.last_error = ""
        if not isinstance(token, str) or not token:
            self.last_error = "没有可保存的登录凭据"
            return False
        try:
            self._backend().set_password(self.SERVICE, self.ACCOUNT, token)
            return True
        except Exception:
            self.last_error = "无法使用 Windows 凭据管理器保存登录；本次登录仅保留在内存中"
            return False

    def load(self):
        self.last_error = ""
        try:
            token = self._backend().get_password(self.SERVICE, self.ACCOUNT)
            return token if isinstance(token, str) and token else None
        except Exception:
            self.last_error = "无法读取 Windows 凭据管理器，请重新登录"
            return None

    def clear(self):
        self.last_error = ""
        try:
            backend = self._backend()
            if backend.get_password(self.SERVICE, self.ACCOUNT) is not None:
                backend.delete_password(self.SERVICE, self.ACCOUNT)
        except Exception:
            self.last_error = "无法清除 Windows 凭据管理器中的登录，请重试或手动删除对应凭据"
