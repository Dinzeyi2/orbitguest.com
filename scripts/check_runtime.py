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
    if os.getenv("ORBIT_ENABLE_LIVE_MESSAGING", "").lower() == "true":
        required = ("TELNYX_API_KEY", "TELNYX_FROM_NUMBER", "TELNYX_MESSAGING_PROFILE_ID", "TELNYX_PUBLIC_KEY", "TELNYX_ALLOWED_COUNTRY_PREFIXES", "RESEND_API_KEY", "ORBIT_EMAIL_FROM", "RESEND_WEBHOOK_SECRET")
        missing = [name for name in required if not os.getenv(name)]
        if missing: raise SystemExit(f"FATAL: live messaging is enabled but these variables are missing: {', '.join(missing)}")
    print("Orbit runtime check passed: cryptography and Fernet encryption are available.")

if __name__ == "__main__":
    main()
