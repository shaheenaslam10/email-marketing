import base64
import logging
from cryptography.fernet import Fernet
from django.conf import settings

logger = logging.getLogger(__name__)

# Fallback 32-byte key for local dev if not configured
DEFAULT_KEY = b"9V4HkG7xJ6wQ2Z1yP8mN3tL5rS0vF4eD2aB8cE1fH3g="

def get_fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not key:
        key = DEFAULT_KEY
    if isinstance(key, str):
        key = key.encode()
    try:
        return Fernet(key)
    except Exception as e:
        logger.warning("Invalid FIELD_ENCRYPTION_KEY provided, falling back to default dev key: %s", e)
        return Fernet(DEFAULT_KEY)


def encrypt_value(plain_text: str) -> str:
    """Encrypts a string value at rest using AES-256 Fernet."""
    if not plain_text:
        return ""
    try:
        fernet = get_fernet()
        return fernet.encrypt(plain_text.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error("Encryption failed: %s", e)
        return ""


def decrypt_value(cipher_text: str) -> str:
    """Decrypts an encrypted string value. Never writes decrypted values to logs."""
    if not cipher_text:
        return ""
    try:
        fernet = get_fernet()
        return fernet.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error("Decryption failed: %s", e)
        return ""
