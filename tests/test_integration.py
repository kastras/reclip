import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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


class ApiIntegrationTests(unittest.TestCase):
    maxDiff = None

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

        self.app_module.pending_codes.clear()
        self.app_module.chat_sessions.clear()
        self.app_module.download_tokens.clear()
        self.app_module.jobs.clear()
        self.app_module.login_attempts.clear()

    def tearDown(self):
        self.temp_dir.cleanup()

    def login_admin(self):
        return self.client.post(
            "/admin",
            data={"action": "login", "password": "admin-pass"},
            follow_redirects=True,
        )

    def grant_web_session(self, code="TESTCODE", label="Tester"):
        with self.client.session_transaction() as sess:
            sess["web_access"] = {"code": code, "label": label}

    # -- Middleware tests --

    def test_middleware_redirects_unauthenticated_to_access(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/access"))

    def test_middleware_rejects_api_without_auth(self):
        response = self.client.post("/api/info", json={"url": "https://example.com/v"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Unauthorized")

    def test_middleware_allows_access_and_admin_paths(self):
        access_get = self.client.get("/access")
        admin_get = self.client.get("/admin")
        self.assertEqual(access_get.status_code, 200)
        self.assertEqual(admin_get.status_code, 200)

    # -- Access code flow --

    def test_access_code_full_flow(self):
        self.login_admin()
        response = self.client.post(
            "/admin",
            data={"action": "generate_web_code", "label": "Marta"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Código web generado correctamente.".encode(), response.data)

        with open(os.environ["WEB_CODES_PATH"], "r") as handle:
            codes = json.load(handle)

        self.assertEqual(len(codes), 1)
        code = next(iter(codes))

        user_client = self.app_module.app.test_client()
        access_response = user_client.post("/access", data={"code": code}, follow_redirects=True)
        self.assertEqual(access_response.status_code, 200)
        self.assertIn(b"Re<em>Clip</em>", access_response.data)

        refreshed_codes = self.app_module.web_codes_load()
        self.assertEqual(refreshed_codes[code]["use_count"], 1)

    def test_access_code_empty_rejected(self):
        response = self.client.post("/access", data={"code": ""}, follow_redirects=True)
        self.assertIn("Introduce un código de acceso.".encode(), response.data)

    def test_access_code_invalid_rejected(self):
        response = self.client.post("/access", data={"code": "INVALID"}, follow_redirects=True)
        self.assertIn("Código inválido o revocado.".encode(), response.data)

    def test_access_code_revoked_rejected(self):
        self.login_admin()
        code = self.app_module.create_web_access_code("Oficina")
        self.client.post(
            "/admin",
            data={"action": "revoke_web_code", "code": code},
            follow_redirects=True,
        )
        user_client = self.app_module.app.test_client()
        denied = user_client.post("/access", data={"code": code}, follow_redirects=True)
        self.assertIn("Código inválido o revocado.".encode(), denied.data)

    def test_access_logout_clears_session(self):
        self.grant_web_session()
        response = self.client.post("/access/logout", follow_redirects=True)
        self.assertTrue(response.request.path.endswith("/access"))

    # -- Admin login --

    def test_admin_login_success(self):
        response = self.client.post(
            "/admin",
            data={"action": "login", "password": "admin-pass"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cerrar sesión".encode(), response.data)

    def test_admin_login_failure(self):
        response = self.client.post(
            "/admin",
            data={"action": "login", "password": "wrong"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contrase", response.data)

    def test_admin_login_rate_limiting(self):
        for _ in range(5):
            self.client.post(
                "/admin",
                data={"action": "login", "password": "wrong"},
                follow_redirects=True,
            )

        response = self.client.post(
            "/admin",
            data={"action": "login", "password": "admin-pass"},
            follow_redirects=True,
        )
        self.assertIn("Demasiados intentos".encode(), response.data)

    def test_admin_actions_require_login(self):
        response = self.client.post(
            "/admin",
            data={"action": "generate_web_code", "label": "X"},
            follow_redirects=True,
        )
        self.assertNotIn("Código web generado".encode(), response.data)

    # -- Admin cookie management --

    def test_admin_save_and_clear_cookies(self):
        self.login_admin()
        cookies_file = os.path.join(self.temp_dir.name, "cookies_admin.txt")
        self.app_module.ADMIN_COOKIES_PATH = cookies_file
        save = self.client.post(
            "/admin",
            data={"action": "save_cookies", "cookies_content": ".domain.com\tTRUE\t/\tFALSE\t0\tname\tvalue"},
            follow_redirects=True,
        )
        self.assertIn("Cookies guardadas correctamente.".encode(), save.data)

        with open(cookies_file, "r") as f:
            content = f.read()
        self.assertIn("name\tvalue", content)

        clear = self.client.post(
            "/admin",
            data={"action": "clear_cookies"},
            follow_redirects=True,
        )
        self.assertIn("Cookies eliminadas.".encode(), clear.data)
        self.assertFalse(os.path.exists(cookies_file))

    def test_admin_save_cookies_invalid_format(self):
        self.login_admin()
        save = self.client.post(
            "/admin",
            data={"action": "save_cookies", "cookies_content": "notab"},
            follow_redirects=True,
        )
        self.assertIn("Formato inválido".encode(), save.data)

    # -- Admin ACL actions (pending codes) --

    def test_admin_approve_pending_code(self):
        self.app_module.pending_codes["ABC123"] = {
            "chat_id": 12345,
            "username": "testuser",
            "first_name": "Test",
            "created_at": time.time(),
        }
        self.login_admin()
        approve = self.client.post(
            "/admin",
            data={"action": "approve", "code": "ABC123"},
            follow_redirects=True,
        )
        self.assertIn("Usuario de Telegram aprobado.".encode(), approve.data)
        acl = self.app_module.acl_load()
        self.assertIn("12345", acl)
        self.assertTrue(acl["12345"]["approved"])

    def test_admin_approve_expired_code(self):
        self.app_module.pending_codes["OLDCODE"] = {
            "chat_id": 999,
            "username": "old",
            "first_name": "Old",
            "created_at": time.time() - 2000,
        }
        self.login_admin()
        approve = self.client.post(
            "/admin",
            data={"action": "approve", "code": "OLDCODE"},
            follow_redirects=True,
        )
        self.assertIn("Código inválido o expirado.".encode(), approve.data)

    # -- Admin block/unblock --

    def test_admin_block_unblock_user(self):
        self.app_module.acl_save({
            "111": {"chat_id": 111, "approved": True, "blocked": False, "downloads": 0},
        })
        self.login_admin()

        block = self.client.post(
            "/admin",
            data={"action": "block", "chat_id": "111"},
            follow_redirects=True,
        )
        self.assertIn("Usuario bloqueado.".encode(), block.data)
        self.assertTrue(self.app_module.acl_load()["111"]["blocked"])

        unblock = self.client.post(
            "/admin",
            data={"action": "unblock", "chat_id": "111"},
            follow_redirects=True,
        )
        self.assertIn("Usuario desbloqueado.".encode(), unblock.data)
        self.assertFalse(self.app_module.acl_load()["111"]["blocked"])

    # -- API /api/info --

    @patch("app.subprocess.run")
    def test_api_info_success(self, mock_run):
        self.grant_web_session()
        fake_stdout = json.dumps({
            "title": "Test Video",
            "thumbnail": "https://example.com/thumb.jpg",
            "duration": 125,
            "uploader": "TestUploader",
            "formats": [
                {"format_id": "137", "height": 1080, "vcodec": "avc1", "tbr": 5000},
                {"format_id": "247", "height": 720, "vcodec": "vp9", "tbr": 2000},
            ],
        })
        mock_run.return_value = Mock(returncode=0, stdout=fake_stdout, stderr="")

        response = self.client.post("/api/info", json={"url": "https://youtube.com/watch?v=test"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["title"], "Test Video")
        self.assertEqual(data["uploader"], "TestUploader")
        self.assertEqual(len(data["formats"]), 2)

    @patch("app.subprocess.run")
    def test_api_info_empty_url(self, mock_run):
        self.grant_web_session()
        response = self.client.post("/api/info", json={"url": ""})
        self.assertEqual(response.status_code, 400)

    # -- API /api/playlist --

    @patch("app.subprocess.run")
    def test_api_playlist(self, mock_run):
        self.grant_web_session()
        fake_stdout = json.dumps({
            "entries": [
                {"url": "https://youtube.com/watch?v=1"},
                {"url": "https://youtube.com/watch?v=2"},
            ]
        })
        mock_run.return_value = Mock(returncode=0, stdout=fake_stdout, stderr="")

        response = self.client.post("/api/playlist", json={"url": "https://youtube.com/playlist?list=test"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["urls"]), 2)

    # -- API /api/download --

    @patch("app.download_sync")
    def test_download_api_success(self, mock_download):
        self.grant_web_session()
        download_path = Path(self.temp_dir.name) / "sample.mp4"
        download_path.write_bytes(b"video-bytes")
        mock_download.return_value = (str(download_path), "sample.mp4")

        with patch.object(self.app_module.threading, "Thread", ImmediateThread):
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

        file_response = self.client.get(f"/api/file/{job_id}")
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.data, b"video-bytes")
        self.assertFalse(download_path.exists())

        missing = self.client.get(f"/api/file/{job_id}")
        self.assertEqual(missing.status_code, 404)

    def test_download_api_no_url(self):
        self.grant_web_session()
        response = self.client.post("/api/download", json={})
        self.assertEqual(response.status_code, 400)

    def test_status_unknown_job(self):
        self.grant_web_session()
        response = self.client.get("/api/status/nonexistent")
        self.assertEqual(response.status_code, 404)

    # -- Download token flow --

    def test_download_token_flow(self):
        download_path = Path(self.temp_dir.name) / "bigfile.mp4"
        download_path.write_bytes(b"big-video-content")

        token = "testtoken123"
        self.app_module.download_tokens[token] = {
            "path": str(download_path),
            "filename": "bigfile.mp4",
        }

        page = self.client.get(f"/dl/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Descargar archivo", page.data)

        dl = self.client.get(f"/dl/{token}/download")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.data, b"big-video-content")
        self.assertIn('attachment; filename="bigfile.mp4"', dl.headers["Content-Disposition"])
        self.assertFalse(download_path.exists())

        page_again = self.client.get(f"/dl/{token}")
        self.assertEqual(page_again.status_code, 404)

        dl_again = self.client.get(f"/dl/{token}/download")
        self.assertEqual(dl_again.status_code, 404)

    def test_download_token_expired_file(self):
        token = "expiredtoken"
        self.app_module.download_tokens[token] = {
            "path": "/nonexistent/file.mp4",
            "filename": "gone.mp4",
        }
        page = self.client.get(f"/dl/{token}")
        self.assertIn("ya no esta disponible".encode(), page.data)
        self.assertNotIn(token, self.app_module.download_tokens)

    # -- API ACL endpoints --

    def test_acl_approve_api(self):
        self.app_module.pending_codes["PEND01"] = {
            "chat_id": 777,
            "username": "pending_user",
            "first_name": "Pending",
            "created_at": time.time(),
        }
        self.login_admin()
        response = self.client.post("/api/acl/approve", json={"code": "PEND01"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        acl = self.app_module.acl_load()
        self.assertIn("777", acl)

    def test_acl_list_users_api(self):
        self.app_module.acl_save({
            "111": {"chat_id": 111, "approved": True, "blocked": False},
        })
        self.login_admin()
        response = self.client.get("/api/acl/users")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("111", data)

    def test_acl_block_unblock_api(self):
        self.app_module.acl_save({
            "222": {"chat_id": 222, "approved": True, "blocked": False},
        })
        self.login_admin()

        block = self.client.post("/api/acl/block", json={"chat_id": 222})
        self.assertEqual(block.status_code, 200)
        self.assertTrue(self.app_module.acl_load()["222"]["blocked"])

        unblock = self.client.post("/api/acl/unblock", json={"chat_id": 222})
        self.assertEqual(unblock.status_code, 200)
        self.assertFalse(self.app_module.acl_load()["222"]["blocked"])

    # -- Misc API endpoints --

    def test_admin_page_shows_download_log(self):
        self.login_admin()
        self.app_module.log_download_event(
            source="telegram", url="https://example.com/vid",
            title="Test clip", format_choice="audio",
            actor="bot-user", filename="track.mp3",
        )
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"https://example.com/vid", response.data)

    def test_admin_disabled_without_password(self):
        original = os.environ.pop("ADMIN_PASSWORD", None)
        try:
            sys.modules.pop("app", None)
            app2 = importlib.import_module("app")
            app2.app.config.update(TESTING=True)
            client = app2.app.test_client()
            response = client.get("/admin")
            self.assertIn("deshabilitado".encode(), response.data)
        finally:
            if original is not None:
                os.environ["ADMIN_PASSWORD"] = original

    def test_access_code_multiple_uses(self):
        self.login_admin()
        code = self.app_module.create_web_access_code("Multi")
        for i in range(3):
            user_client = self.app_module.app.test_client()
            resp = user_client.post("/access", data={"code": code}, follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
        refreshed = self.app_module.web_codes_load()
        self.assertEqual(refreshed[code]["use_count"], 3)

    def test_web_download_logs_actor_and_source(self):
        self.grant_web_session(code="ALICE01", label="Alice")
        download_path = Path(self.temp_dir.name) / "sample.mp4"
        download_path.write_bytes(b"video-bytes")

        with patch.object(self.app_module, "download_sync", return_value=(str(download_path), "sample.mp4")):
            with patch.object(self.app_module.threading, "Thread", ImmediateThread):
                response = self.client.post(
                    "/api/download",
                    json={"url": "https://example.com/v", "format": "video", "title": "Clip"},
                )

        self.assertEqual(response.status_code, 200)
        logs = self.app_module.download_log_load()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["actor"], "Alice")
        self.assertEqual(logs[0]["source"], "web")

    def test_download_log_capped_at_200(self):
        for i in range(210):
            self.app_module.log_download_event(
                source="test", url=f"https://example.com/{i}",
                title=f"Clip {i}", format_choice="video",
                actor="tester", filename=f"{i}.mp4",
            )
        logs = self.app_module.download_log_load()
        self.assertEqual(len(logs), 200)
        self.assertEqual(logs[0]["url"], "https://example.com/209")


class TelegramIntegrationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)

        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["ADMIN_PASSWORD"] = "admin-pass"
        os.environ["ACL_PATH"] = str(data_dir / "acl.json")
        os.environ["WEB_CODES_PATH"] = str(data_dir / "web_codes.json")
        os.environ["DOWNLOAD_LOG_PATH"] = str(data_dir / "download_log.json")
        os.environ["COOKIES_FILE"] = str(data_dir / "cookies.txt")

        sys.modules.pop("app", None)
        self.app_module = importlib.import_module("app")
        self.app_module.app.config.update(TESTING=True)

        self.app_module.pending_codes.clear()
        self.app_module.chat_sessions.clear()
        self.app_module.download_tokens.clear()
        self.app_module.pending_cookies_setup.clear()
        self.app_module.login_attempts.clear()
        self.app_module.ADMIN_COOKIES_PATH = str(data_dir / "cookies_admin.txt")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_message_mock(self, text=None):
        msg = Mock()
        msg.text = text
        loading_msg = Mock()
        loading_msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=loading_msg)
        msg.reply_html = AsyncMock()
        return msg

    def _make_callback_query_mock(self, data, chat_id, username):
        query = Mock()
        query.data = data
        query.message.chat_id = chat_id
        query.from_user.username = username
        query.from_user.first_name = "Test"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        return query

    def _mock_update(self, message_text=None, chat_id=1001, username="testuser", first_name="Test"):
        update = Mock()
        update.effective_chat.id = chat_id
        update.effective_user.username = username
        update.effective_user.first_name = first_name
        update.message = self._make_message_mock(message_text)
        update.callback_query = None
        return update

    def _mock_callback_update(self, callback_data, chat_id=1001, username="testuser"):
        update = Mock()
        update.callback_query = self._make_callback_query_mock(callback_data, chat_id, username)
        update.effective_chat.id = chat_id
        update.message = None
        return update

    def _mock_context(self):
        context = Mock()
        context.bot.send_message = AsyncMock()
        context.bot.send_document = AsyncMock()
        return context

    def _run_async(self, coro):
        return asyncio.run(coro)

    # -- First_url_from_text --

    def test_first_url_from_text_extracts_url(self):
        result = self.app_module.first_url_from_text("check this https://example.com/video ok")
        self.assertEqual(result, "https://example.com/video")

    def test_first_url_from_text_none_for_no_url(self):
        self.assertIsNone(self.app_module.first_url_from_text("just text"))

    def test_first_url_from_text_none_for_empty(self):
        self.assertIsNone(self.app_module.first_url_from_text(""))
        self.assertIsNone(self.app_module.first_url_from_text(None))

    def test_first_url_from_text_multiple_urls(self):
        result = self.app_module.first_url_from_text(
            "https://first.com https://second.com"
        )
        self.assertEqual(result, "https://first.com")

    # -- Build_telegram_menu --

    def test_build_telegram_menu_creates_buttons(self):
        formats = [
            {"id": "137", "label": "1080p", "height": 1080},
            {"id": "247", "label": "720p", "height": 720},
        ]
        menu = self.app_module.build_telegram_menu(formats)
        self.assertIsNotNone(menu)
        self.assertEqual(len(menu.inline_keyboard), 4)

    def test_build_telegram_menu_limits_to_8(self):
        formats = [{"id": str(i), "label": f"{i}p", "height": i} for i in range(200, 0, -10)]
        menu = self.app_module.build_telegram_menu(formats)
        format_buttons = [row for row in menu.inline_keyboard if row[0].callback_data.startswith("dl|f|")]
        self.assertLessEqual(len(format_buttons), 8)

    def test_build_telegram_menu_empty(self):
        menu = self.app_module.build_telegram_menu([])
        self.assertEqual(len(menu.inline_keyboard), 2)

    # -- Telegram start handler --

    def test_telegram_start_unapproved_user(self):
        update = self._mock_update(chat_id=1001)
        context = self._mock_context()
        self.assertFalse(self.app_module.acl_is_approved(1001))

        self._run_async(self.app_module.telegram_start(update, context))

        self.assertEqual(len(self.app_module.pending_codes), 1)
        update.message.reply_text.assert_awaited_once()

    def test_telegram_start_approved_user(self):
        self.app_module.acl_approve(chat_id=1001, username="testuser", first_name="Test")
        update = self._mock_update(chat_id=1001)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_start(update, context))

        self.assertNotIn(1001, {v["chat_id"] for v in self.app_module.pending_codes.values()})

    def test_telegram_start_generates_code_in_pending_codes(self):
        self.assertFalse(self.app_module.acl_is_approved(1002))
        update = self._mock_update(chat_id=1002, username="newuser", first_name="New")
        context = self._mock_context()

        self._run_async(self.app_module.telegram_start(update, context))

        self.assertEqual(len(self.app_module.pending_codes), 1)
        code_entry = next(iter(self.app_module.pending_codes.values()))
        self.assertEqual(code_entry["chat_id"], 1002)
        self.assertEqual(code_entry["username"], "newuser")
        self.assertIn("created_at", code_entry)

    # -- Telegram help handler --

    def test_telegram_help(self):
        update = self._mock_update()
        context = self._mock_context()

        self._run_async(self.app_module.telegram_help(update, context))

        update.message.reply_text.assert_awaited_once()
        args, _ = update.message.reply_text.call_args
        self.assertIn("/start", args[0])
        self.assertIn("/help", args[0])

    # -- Telegram cookies handler --

    def test_telegram_cookies_unapproved_user(self):
        update = self._mock_update(chat_id=2001)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_cookies(update, context))

        update.message.reply_text.assert_awaited_once()
        args, _ = update.message.reply_text.call_args
        self.assertIn("No tienes acceso", args[0])

    def test_telegram_cookies_approved_user(self):
        self.app_module.acl_approve(chat_id=2001, username="cookietest", first_name="Cookie")
        self.assertEqual(len(self.app_module.pending_cookies_setup), 0)
        update = self._mock_update(chat_id=2001)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_cookies(update, context))

        self.assertIn(2001, self.app_module.pending_cookies_setup)
        update.message.reply_text.assert_awaited_once()
        args, _ = update.message.reply_text.call_args
        self.assertIn("formato Netscape", args[0])

    # -- Telegram cancel handler --

    def test_telegram_cancel_with_pending(self):
        self.app_module.pending_cookies_setup[3001] = time.time()
        update = self._mock_update(chat_id=3001)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_cancel(update, context))

        self.assertNotIn(3001, self.app_module.pending_cookies_setup)
        update.message.reply_text.assert_awaited_once()
        args, _ = update.message.reply_text.call_args
        self.assertIn("cancelada", args[0])

    def test_telegram_cancel_without_pending(self):
        update = self._mock_update(chat_id=3002)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_cancel(update, context))

        args, _ = update.message.reply_text.call_args
        self.assertIn("No hay ninguna", args[0])

    # -- Telegram on_message handler --

    def test_telegram_on_message_unapproved(self):
        update = self._mock_update(message_text="https://example.com/v")
        context = self._mock_context()

        self._run_async(self.app_module.telegram_on_message(update, context))

        args, _ = update.message.reply_text.call_args
        self.assertIn("No tienes acceso", args[0])

    def test_telegram_on_message_approved_sets_chat_session(self):
        self.app_module.acl_approve(chat_id=4001, username="vidtest", first_name="Video")
        self.app_module.chat_sessions.clear()

        info = {
            "title": "Test Video",
            "formats": [
                {"format_id": "137", "height": 1080, "vcodec": "avc1", "tbr": 5000},
            ],
        }

        with patch.object(self.app_module, "fetch_video_info", return_value=info):
            update = self._mock_update(message_text="https://youtube.com/watch?v=test", chat_id=4001)
            context = self._mock_context()

            self._run_async(self.app_module.telegram_on_message(update, context))

            self.assertIn(4001, self.app_module.chat_sessions)
            self.assertEqual(self.app_module.chat_sessions[4001]["url"], "https://youtube.com/watch?v=test")
            update.message.reply_text.assert_awaited()

    # -- Telegram callback handler --

    def test_telegram_on_callback_cancel(self):
        self.app_module.chat_sessions[5001] = {"url": "https://example.com/v"}
        update = self._mock_callback_update("dl|cancel", chat_id=5001)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_on_callback(update, context))

        self.assertNotIn(5001, self.app_module.chat_sessions)
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_awaited_once()

    def test_telegram_on_callback_no_session(self):
        update = self._mock_callback_update("dl|best", chat_id=5002)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_on_callback(update, context))

        args, _ = update.callback_query.edit_message_text.call_args
        self.assertIn("No tengo una URL activa", args[0])

    def test_telegram_on_callback_invalid_data(self):
        self.app_module.chat_sessions[5003] = {"url": "https://example.com/v"}
        update = self._mock_callback_update("invalid_data", chat_id=5003)
        context = self._mock_context()

        self._run_async(self.app_module.telegram_on_callback(update, context))

        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_called()

    # -- normalize_cookies --

    def test_normalize_cookies_valid(self):
        content = ".instagram.com\tTRUE\t/\tFALSE\t0\tsessionid\tabc123"
        normalized, err = self.app_module.normalize_cookies(content)
        self.assertIsNone(err)
        self.assertIsNotNone(normalized)
        self.assertIn("sessionid", normalized)

    def test_normalize_cookies_invalid_field_count(self):
        content = "field1 field2 field3"
        _, err = self.app_module.normalize_cookies(content)
        self.assertIsNotNone(err)
        self.assertIn("7", err)

    def test_normalize_cookies_skips_comments(self):
        content = "# Netscape HTTP Cookie File\n.domain.com\tTRUE\t/\tFALSE\t0\tname\tvalue"
        normalized, err = self.app_module.normalize_cookies(content)
        self.assertIsNone(err)
        self.assertIn("# Netscape", normalized)
        self.assertIn("name\tvalue", normalized)

    def test_normalize_cookies_mixed_whitespace(self):
        content = ".domain.com  TRUE  /  FALSE  0  name  value"
        normalized, err = self.app_module.normalize_cookies(content)
        self.assertIsNone(err)
        self.assertIn("name\tvalue", normalized)

    # -- Download log --

    def test_log_download_event(self):
        self.app_module.log_download_event(
            source="telegram", url="https://example.com/tg",
            title="TG clip", format_choice="audio",
            actor="telegram-user", filename="audio.mp3",
        )
        logs = self.app_module.download_log_load()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["source"], "telegram")
        self.assertEqual(logs[0]["actor"], "telegram-user")

    # -- ACL helpers --

    def test_acl_is_approved_returns_false_for_missing(self):
        self.assertFalse(self.app_module.acl_is_approved(9999))

    def test_acl_is_approved_returns_false_for_blocked(self):
        self.app_module.acl_save({
            "888": {"chat_id": 888, "approved": True, "blocked": True},
        })
        self.assertFalse(self.app_module.acl_is_approved(888))

    def test_acl_is_approved_returns_true_for_approved(self):
        self.app_module.acl_approve(chat_id=777, username="gooduser", first_name="Good")
        self.assertTrue(self.app_module.acl_is_approved(777))

    def test_acl_increment_downloads(self):
        self.app_module.acl_approve(chat_id=666, username="dluser", first_name="DL")
        self.app_module.acl_increment_downloads(666)
        self.assertEqual(self.app_module.acl_load()["666"]["downloads"], 1)

    # -- yt_dlp_cookies_args --

    def test_ytdlp_cookies_args_no_cookies(self):
        args = self.app_module.yt_dlp_cookies_args()
        self.assertEqual(args, [])

    def test_ytdlp_cookies_args_with_admin_cookies(self):
        with open(self.app_module.ADMIN_COOKIES_PATH, "w") as f:
            f.write(".domain.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n")
        args = self.app_module.yt_dlp_cookies_args()
        self.assertIn("--cookies", args)
        self.assertIn(self.app_module.ADMIN_COOKIES_PATH, args)

    # -- cookies_admin_status --

    def test_cookies_admin_status_not_configured(self):
        status = self.app_module.cookies_admin_status()
        self.assertFalse(status["configured"])

    def test_cookies_admin_status_configured(self):
        with open(self.app_module.ADMIN_COOKIES_PATH, "w") as f:
            f.write(".domain.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n")
        status = self.app_module.cookies_admin_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["lines"], 1)

    # -- format_duration --

    def test_format_duration_seconds(self):
        self.assertEqual(self.app_module.format_duration(45), "0:45")

    def test_format_duration_minutes(self):
        self.assertEqual(self.app_module.format_duration(125), "2:05")

    def test_format_duration_hours(self):
        self.assertEqual(self.app_module.format_duration(3661), "1:01:01")

    def test_format_duration_none(self):
        self.assertEqual(self.app_module.format_duration(None), "unknown")

    # -- sanitize_filename --

    def test_sanitize_filename_uses_title(self):
        result = self.app_module.sanitize_filename("My Cool Video", "fallback.mp4")
        self.assertEqual(result, "My Cool Video.mp4")

    def test_sanitize_filename_fallback_on_empty_title(self):
        result = self.app_module.sanitize_filename("", "fallback.mp4")
        self.assertEqual(result, "fallback.mp4")

    def test_sanitize_filename_strips_invalid_chars(self):
        result = self.app_module.sanitize_filename('My: Video? <test>', "fallback.mp4")
        self.assertEqual(result, "My Video test.mp4")

    # -- build_quality_options --

    def test_build_quality_options(self):
        info = {
            "formats": [
                {"format_id": "137", "height": 1080, "vcodec": "avc1", "tbr": 5000},
                {"format_id": "247", "height": 720, "vcodec": "vp9", "tbr": 2000},
                {"format_id": "140", "height": None, "vcodec": "none", "tbr": 128},
            ]
        }
        options = self.app_module.build_quality_options(info)
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0]["label"], "1080p")
        self.assertEqual(options[1]["label"], "720p")

    def test_build_quality_options_empty(self):
        self.assertEqual(self.app_module.build_quality_options({"formats": []}), [])

    # -- create_web_access_code / use_web_access_code / sorted_web_codes --

    def test_create_and_use_web_access_code(self):
        code = self.app_module.create_web_access_code("Integration")
        self.assertEqual(len(code), 8)

        entry = self.app_module.use_web_access_code(code)
        self.assertIsNotNone(entry)
        self.assertFalse(entry["revoked"])
        self.assertEqual(entry["use_count"], 1)

        codes = self.app_module.sorted_web_codes()
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0][0], code)

    def test_use_revoked_code_returns_none(self):
        code = self.app_module.create_web_access_code("Revokable")
        codes = self.app_module.web_codes_load()
        codes[code]["revoked"] = True
        self.app_module.web_codes_save(codes)

        entry = self.app_module.use_web_access_code(code)
        self.assertIsNone(entry)

    # -- json helpers --

    def test_json_load_dict_missing_file(self):
        self.assertEqual(self.app_module.json_load_dict("/nonexistent/path"), {})

    def test_json_load_dict_invalid_json(self):
        p = Path(self.temp_dir.name) / "invalid.json"
        p.write_text("{bad json")
        self.assertEqual(self.app_module.json_load_dict(str(p)), {})

    def test_json_load_list_missing_file(self):
        self.assertEqual(self.app_module.json_load_list("/nonexistent/path"), [])

    def test_json_save_and_load(self):
        p = Path(self.temp_dir.name) / "test_data.json"
        self.app_module.json_save(str(p), {"key": "value"})
        loaded = self.app_module.json_load_dict(str(p))
        self.assertEqual(loaded, {"key": "value"})


if __name__ == "__main__":
    unittest.main()
