from __future__ import annotations

import os

SERVICE_NAME = "DocumentIntelligenceConsole"


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def get_secret(kind: str, account: str, env_name: str = "") -> str:
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE_NAME, f"{kind}:{account}")
            if value:
                return value
        except Exception:
            pass
    return os.getenv(env_name, "") if env_name else ""


def set_secret(kind: str, account: str, value: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        username = f"{kind}:{account}"
        if value:
            kr.set_password(SERVICE_NAME, username, value)
        else:
            try:
                kr.delete_password(SERVICE_NAME, username)
            except Exception:
                pass
        return True
    except Exception:
        return False
