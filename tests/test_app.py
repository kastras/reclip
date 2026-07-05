import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = False

    def start(self):
        if self.target:
            self.target(*self.args, **self.kwargs)


class ReclipAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)

        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["ADMIN_PASSWORD"] = "admin-pass"
        os.environ["ACL_PATH"] = str(data_dir / "acl.json")
        os.environ["WEB_CODES_PATH"] = str(data_dir / "web_codes.json")
        os.environ["DOWNLOAD_LOG_PATH"] = str(data_dir / "download_log.json")

        sys.modules.pop("app", None)
        self.app_module = importlib.import_module("app")
        self.app_module.app.config.update(TESTING=True)
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def login_admin(self):
        response = self.client.post(
            "/admin",
            data={"action": "login", "password": "admin-pass"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def grant_web_session(self, code="TESTCODE", label="Tester"):
        with self.client.session_transaction() as sess:
            sess["web_access"] = {"code": code, "label": label}

    def test_root_requires_access_code(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/access"))

        api_response = self.client.post("/api/info", json={"url": "https://example.com/video"})
        self.assertEqual(api_response.status_code, 401)

    def test_admin_can_generate_code_and_user_can_enter_web(self):
        self.login_admin()
        response = self.client.post(
            "/admin",
            data={"action": "generate_web_code", "label": "Marta"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Código web generado correctamente.".encode(), response.data)

        with open(os.environ["WEB_CODES_PATH"], "r", encoding="utf-8") as handle:
            codes = json.load(handle)

        self.assertEqual(len(codes), 1)
        code = next(iter(codes))

        user_client = self.app_module.app.test_client()
        access_response = user_client.post("/access", data={"code": code}, follow_redirects=True)
        self.assertEqual(access_response.status_code, 200)
        self.assertIn(b"Re<em>Clip</em>", access_response.data)

        refreshed_codes = self.app_module.web_codes_load()
        self.assertEqual(refreshed_codes[code]["use_count"], 1)

    def test_revoked_code_cannot_be_used(self):
        self.login_admin()
        code = self.app_module.create_web_access_code("Oficina")

        response = self.client.post(
            "/admin",
            data={"action": "revoke_web_code", "code": code},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        user_client = self.app_module.app.test_client()
        denied = user_client.post("/access", data={"code": code}, follow_redirects=True)
        self.assertEqual(denied.status_code, 200)
        self.assertIn("Código inválido o revocado.".encode(), denied.data)

    def test_web_download_is_logged_and_file_is_served_once(self):
        self.grant_web_session(code="ALICE01", label="Alice")
        download_path = Path(self.temp_dir.name) / "sample.mp4"
        download_path.write_bytes(b"video-bytes")

        with mock.patch.object(self.app_module, "download_sync", return_value=(str(download_path), "sample.mp4")):
            with mock.patch.object(self.app_module.threading, "Thread", ImmediateThread):
                response = self.client.post(
                    "/api/download",
                    json={
                        "url": "https://example.com/watch?v=123",
                        "format": "video",
                        "title": "Clip demo",
                    },
                )

        self.assertEqual(response.status_code, 200)
        job_id = response.get_json()["job_id"]

        status_response = self.client.get(f"/api/status/{job_id}")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.get_json()["status"], "done")

        log_entries = self.app_module.download_log_load()
        self.assertEqual(len(log_entries), 1)
        self.assertEqual(log_entries[0]["url"], "https://example.com/watch?v=123")
        self.assertEqual(log_entries[0]["actor"], "Alice")
        self.assertEqual(log_entries[0]["source"], "web")

        file_response = self.client.get(f"/api/file/{job_id}")
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.data, b"video-bytes")
        self.assertFalse(download_path.exists())

        missing_again = self.client.get(f"/api/file/{job_id}")
        self.assertEqual(missing_again.status_code, 404)

    def test_admin_page_shows_recent_download_urls(self):
        self.login_admin()
        self.app_module.log_download_event(
            source="telegram",
            url="https://example.com/telegram",
            title="Telegram clip",
            format_choice="audio",
            actor="bot-user",
            filename="track.mp3",
        )

        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"https://example.com/telegram", response.data)
        self.assertIn(b"Telegram clip", response.data)


if __name__ == "__main__":
    unittest.main()