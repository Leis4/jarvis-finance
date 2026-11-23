from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jarvis.finance.db import FinanceDB
from jarvis.finance.security import (
    PBKDF2_SHA256_ITERATIONS,
    assert_safe_bind_host,
    clear_first_run_setup_token,
    configure_local_auth,
    ensure_first_run_setup_token,
    generate_csrf_token,
    hash_password,
    is_auth_enabled,
    is_loopback_bind_host,
    issue_session_token,
    verify_csrf_token,
    verify_first_run_setup_token,
    verify_local_auth,
    verify_session_token,
)


class SecurityTests(unittest.TestCase):
    def test_loopback_bind_policy(self) -> None:
        self.assertTrue(is_loopback_bind_host("127.0.0.1"))
        self.assertTrue(is_loopback_bind_host("localhost"))
        self.assertFalse(is_loopback_bind_host("0.0.0.0"))

    def test_pbkdf2_default_is_owasp_current_floor(self) -> None:
        stored = hash_password("strong-password")
        if stored.algorithm == "argon2id":
            self.assertTrue(stored.encoded.startswith("$argon2id$"))
        else:
            self.assertEqual(stored.iterations, PBKDF2_SHA256_ITERATIONS)
            self.assertGreaterEqual(stored.iterations, 600_000)

    def test_first_run_setup_token_is_required_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = FinanceDB(Path(td) / "finance.db")
            db.init()
            token = ensure_first_run_setup_token(db)
            self.assertTrue(verify_first_run_setup_token(db, token))
            self.assertFalse(verify_first_run_setup_token(db, "wrong"))
            clear_first_run_setup_token(db)
            self.assertFalse(verify_first_run_setup_token(db, token))

    def test_sessions_and_csrf_are_signed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = FinanceDB(Path(td) / "finance.db")
            db.init()
            configure_local_auth(db, "admin", "strong-password")
            session = issue_session_token(db, "admin", "admin")
            ok, user, role = verify_session_token(db, session)
            self.assertTrue(ok)
            self.assertEqual(user, "admin")
            self.assertEqual(role, "admin")
            self.assertFalse(verify_session_token(db, session + "tamper")[0])
            csrf = generate_csrf_token(db, session_token=session)
            self.assertTrue(verify_csrf_token(db, csrf, session_token=session))
            self.assertFalse(verify_csrf_token(db, csrf, session_token="other-session"))
            self.assertFalse(verify_csrf_token(db, csrf + "tamper", session_token=session))

    def test_auth_configuration_persists_and_invalid_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixture.sqlite"
            db = FinanceDB(path)
            result = configure_local_auth(db, "admin", "strong-password")
            self.assertEqual(result, {"enabled": True, "users": ["admin"]})
            reopened = FinanceDB(path)
            self.assertTrue(is_auth_enabled(reopened))
            self.assertEqual(verify_local_auth(reopened, "admin", "strong-password"), (True, "admin"))
            self.assertEqual(verify_local_auth(reopened, "admin", "wrong"), (False, ""))
            saved = reopened.get_setting("security")
            self.assertNotIn("strong-password", str(saved))
            reopened.set_setting("security", {"enabled": True, "users": "invalid"})
            self.assertEqual(verify_local_auth(reopened, "admin", "strong-password"), (False, ""))
            self.assertFalse(is_auth_enabled(reopened))
            reopened.set_setting("security", {"enabled": True, "users": {"admin": {"password_hash": {}}}})
            self.assertFalse(is_auth_enabled(reopened))
            reopened.set_setting("security", {"enabled": False, "users": {}})
            self.assertEqual(verify_local_auth(reopened, "admin", "strong-password"), (False, ""))

    def test_fastapi_wildcard_bind_is_rejected_even_with_local_auth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = FinanceDB(Path(td) / "fixture.sqlite")
            configure_local_auth(db, "admin", "strong-password")
            assert_safe_bind_host(db, "127.0.0.1", service="FastAPI API")
            with self.assertRaisesRegex(RuntimeError, "loopback"):
                assert_safe_bind_host(db, "0.0.0.0", service="FastAPI API")


if __name__ == "__main__":
    unittest.main()
