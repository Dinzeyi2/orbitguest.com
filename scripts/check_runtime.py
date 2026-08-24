"""Fail deployment early when a required production dependency is unavailable."""

import os
import sys

def main():
    try:
        from cryptography.fernet import Fernet
    except ImportError as error:
        raise SystemExit(
            "FATAL: cryptography is not installed. Railway must install requirements.txt before starting Orbit."
        ) from error

    key = os.getenv("TOKEN_ENCRYPTION_KEY")
    if os.getenv("SQUARE_APPLICATION_ID") and not key:
        raise SystemExit("FATAL: TOKEN_ENCRYPTION_KEY is required when Square is configured.")
    try:
        fernet = Fernet((key or Fernet.generate_key().decode()).encode())
        test_value = b"orbit-runtime-encryption-check"
        if fernet.decrypt(fernet.encrypt(test_value)) != test_value:
            raise ValueError("round-trip mismatch")
    except Exception as error:
        raise SystemExit(f"FATAL: Fernet token encryption check failed: {error}") from error
    print("Orbit runtime check passed: cryptography and Fernet encryption are available.")

if __name__ == "__main__":
    main()
