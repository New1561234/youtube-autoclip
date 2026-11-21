"""
Tests for the optional YouTube upload feature.

Every Google API call is mocked, so these tests never touch the network and do
not require real credentials or the Google client libraries to be installed.

Run: python test_youtube.py
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import youtube_auth
import youtube_upload
import app as clip_app


# ---------------------------------------------------------------------------
# Fake Google client plumbing
# ---------------------------------------------------------------------------

class _FakeStatus:
    def __init__(self, frac):
        self._frac = frac

    def progress(self):
        return self._frac


class _FakeRequest:
    def __init__(self, statuses, response):
        self._statuses = list(statuses)
        self._response = response

    def next_chunk(self):
        if self._statuses:
            return _FakeStatus(self._statuses.pop(0)), None
        return None, self._response


class _FakeMediaFileUpload:
    def __init__(self, filename, mimetype=None, chunksize=None, resumable=None):
        self.filename = filename
        self.mimetype = mimetype
        self.chunksize = chunksize
        self.resumable = resumable


class _FakeVideos:
    def __init__(self, request):
        self._request = request
        self.inserted = None

    def insert(self, part=None, body=None, media_body=None):
        self.inserted = {"part": part, "body": body, "media": media_body}
        return self._request


class _FakeUploadService:
    def __init__(self, response):
        self._request = _FakeRequest([0.25, 0.75], response)
        self._videos = _FakeVideos(self._request)

    def videos(self):
        return self._videos


class _FakeChannelsRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeChannels:
    def __init__(self, response):
        self._response = response

    def list(self, part=None, mine=None):
        return _FakeChannelsRequest(self._response)


class _FakeChannelService:
    def __init__(self, response):
        self._response = response

    def channels(self):
        return _FakeChannels(self._response)


@contextmanager
def fake_googleapiclient(build_impl):
    """Install fake googleapiclient modules for the duration of the block."""
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = build_impl

    http = types.ModuleType("googleapiclient.http")
    http.MediaFileUpload = _FakeMediaFileUpload

    errors = types.ModuleType("googleapiclient.errors")

    class HttpError(Exception):
        def __init__(self, content):
            self.content = content
            super().__init__("http error")

    errors.HttpError = HttpError

    pkg = types.ModuleType("googleapiclient")
    pkg.discovery = discovery
    pkg.http = http
    pkg.errors = errors

    names = [
        "googleapiclient",
        "googleapiclient.discovery",
        "googleapiclient.http",
        "googleapiclient.errors",
    ]
    saved = {n: sys.modules.get(n) for n in names}
    sys.modules["googleapiclient"] = pkg
    sys.modules["googleapiclient.discovery"] = discovery
    sys.modules["googleapiclient.http"] = http
    sys.modules["googleapiclient.errors"] = errors
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _write_temp(suffix, data=b"\x00"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


# ---------------------------------------------------------------------------
# Unit tests: youtube_auth
# ---------------------------------------------------------------------------

class YouTubeAuthTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(len(youtube_auth.SCOPES), 2)
        self.assertIn("youtube.upload", youtube_auth.SCOPES[0])
        self.assertEqual(
            youtube_auth.REDIRECT_URI,
            "http://127.0.0.1:5000/youtube/oauth2callback",
        )

    def test_is_configured(self):
        tmp = tempfile.mkdtemp()
        secret = os.path.join(tmp, "client_secret.json")
        try:
            with mock.patch.object(youtube_auth, "CLIENT_SECRET_FILE", secret):
                self.assertFalse(youtube_auth.is_configured())
                with open(secret, "w", encoding="utf-8") as fh:
                    fh.write("{}")
                self.assertTrue(youtube_auth.is_configured())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_credentials_none_without_token(self):
        missing = os.path.join(tempfile.gettempdir(), "clipper_no_such_token.json")
        if os.path.exists(missing):
            os.remove(missing)
        with mock.patch.object(youtube_auth, "TOKEN_FILE", missing):
            self.assertIsNone(youtube_auth.load_credentials())

    def test_save_and_clear_credentials(self):
        tmp = tempfile.mkdtemp()
        token = os.path.join(tmp, "token.json")

        class FakeCreds:
            def to_json(self):
                return json.dumps({"token": "abc"})

        try:
            with mock.patch.object(youtube_auth, "TOKEN_FILE", token), \
                    mock.patch.object(youtube_auth, "CRED_DIR", tmp):
                youtube_auth.save_credentials(FakeCreds())
                self.assertTrue(os.path.isfile(token))
                with open(token, "r", encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh)["token"], "abc")

                youtube_auth.clear_credentials()
                self.assertFalse(os.path.exists(token))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_get_connected_channel(self):
        def fake_build(name, version, credentials=None, cache_discovery=None):
            self.assertEqual(version, "v3")
            return _FakeChannelService({
                "items": [{
                    "id": "UC123",
                    "snippet": {"title": "My Channel", "customUrl": "@mine"},
                }]
            })

        with fake_googleapiclient(fake_build):
            channel = youtube_auth.get_connected_channel(object())

        self.assertEqual(channel["id"], "UC123")
        self.assertEqual(channel["title"], "My Channel")
        self.assertEqual(channel["custom_url"], "@mine")

    def test_get_connected_channel_empty(self):
        def fake_build(name, version, credentials=None, cache_discovery=None):
            return _FakeChannelService({"items": []})

        with fake_googleapiclient(fake_build):
            self.assertIsNone(youtube_auth.get_connected_channel(object()))


# ---------------------------------------------------------------------------
# Unit tests: youtube_upload
# ---------------------------------------------------------------------------

class YouTubeUploadTests(unittest.TestCase):
    def test_upload_success_reports_progress(self):
        path = _write_temp(".mp4")
        progress = []

        def fake_build(name, version, credentials=None, cache_discovery=None):
            return _FakeUploadService({"id": "vid123"})

        try:
            with fake_googleapiclient(fake_build):
                result = youtube_upload.upload_video(
                    object(), path, "My Title", "A description", "private",
                    progress_cb=progress.append,
                )
        finally:
            os.remove(path)

        self.assertEqual(result["video_id"], "vid123")
        self.assertEqual(result["url"], "https://youtu.be/vid123")
        self.assertEqual(result["title"], "My Title")
        # 0.25 -> 25, 0.75 -> 75, then a final 100 from the completion callback.
        self.assertEqual(progress, [25, 75, 100])

    def test_upload_validation(self):
        path = _write_temp(".mp4")
        try:
            # Empty title
            with self.assertRaises(youtube_upload.YouTubeUploadError):
                youtube_upload.upload_video(object(), path, "   ", "", "private")
            # Invalid privacy
            with self.assertRaises(youtube_upload.YouTubeUploadError):
                youtube_upload.upload_video(object(), path, "Title", "", "friends")
            # Missing file
            with self.assertRaises(youtube_upload.YouTubeUploadError):
                youtube_upload.upload_video(
                    object(), os.path.join(tempfile.gettempdir(), "nope_clip.mp4"),
                    "Title", "", "private",
                )
        finally:
            os.remove(path)

    def test_upload_title_too_long(self):
        path = _write_temp(".mp4")
        try:
            with self.assertRaises(youtube_upload.YouTubeUploadError):
                youtube_upload.upload_video(object(), path, "x" * 101, "", "private")
        finally:
            os.remove(path)


# ---------------------------------------------------------------------------
# Unit tests: app._run_upload (background worker)
# ---------------------------------------------------------------------------

class RunUploadTests(unittest.TestCase):
    def setUp(self):
        self.job_id = uuid.uuid4().hex

    def tearDown(self):
        clip_app.uploads_status.pop(self.job_id, None)

    def test_run_upload_success(self):
        with mock.patch.object(youtube_auth, "load_credentials", return_value=object()), \
                mock.patch.object(
                    youtube_upload, "upload_video",
                    return_value={"video_id": "v1", "url": "https://youtu.be/v1", "title": "t"},
                ):
            clip_app._run_upload(self.job_id, "/tmp/x.mp4", "t", "d", "private")

        rec = clip_app.uploads_status[self.job_id]
        self.assertEqual(rec["status"], "Complete")
        self.assertEqual(rec["url"], "https://youtu.be/v1")

    def test_run_upload_not_connected(self):
        with mock.patch.object(youtube_auth, "load_credentials", return_value=None):
            clip_app._run_upload(self.job_id, "/tmp/x.mp4", "t", "d", "private")

        rec = clip_app.uploads_status[self.job_id]
        self.assertEqual(rec["status"], "Error")
        self.assertEqual(rec["error"], "not_connected")

    def test_run_upload_api_error(self):
        with mock.patch.object(youtube_auth, "load_credentials", return_value=object()), \
                mock.patch.object(
                    youtube_upload, "upload_video",
                    side_effect=youtube_upload.YouTubeUploadError("boom"),
                ):
            clip_app._run_upload(self.job_id, "/tmp/x.mp4", "t", "d", "private")

        rec = clip_app.uploads_status[self.job_id]
        self.assertEqual(rec["status"], "Error")
        self.assertEqual(rec["error"], "boom")


# ---------------------------------------------------------------------------
# Route tests (Flask test client, Google calls mocked)
# ---------------------------------------------------------------------------

class YoutubeRouteTests(unittest.TestCase):
    def setUp(self):
        clip_app.app.config["TESTING"] = True
        self.client = clip_app.app.test_client()

        self.job_id = uuid.uuid4().hex
        self.tmpdir = tempfile.mkdtemp()
        self.filename = "dQw4w9WgXcQ_clip.mp4"
        self.file_path = os.path.join(self.tmpdir, self.filename)
        with open(self.file_path, "wb") as fh:
            fh.write(b"\x00")

        clip_app.jobs_status[self.job_id] = {
            "status": "Complete",
            "result": {"filename": self.filename, "clip_duration": 45, "video_title": "Never Gonna"},
        }
        clip_app.uploads_status.pop(self.job_id, None)

    def tearDown(self):
        clip_app.jobs_status.pop(self.job_id, None)
        clip_app.uploads_status.pop(self.job_id, None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _payload(self, **overrides):
        payload = {
            "job_id": self.job_id,
            "filename": self.filename,
            "title": "My Clip",
            "description": "desc",
            "privacy": "private",
            "confirm": True,
        }
        payload.update(overrides)
        return payload

    # --- status ---------------------------------------------------------
    def test_status_not_configured(self):
        with mock.patch.object(youtube_auth, "is_configured", return_value=False):
            data = self.client.get("/youtube/status").get_json()
        self.assertFalse(data["configured"])
        self.assertFalse(data["connected"])

    def test_status_connected(self):
        with mock.patch.object(youtube_auth, "is_configured", return_value=True), \
                mock.patch.object(youtube_auth, "load_credentials", return_value=object()), \
                mock.patch.object(
                    youtube_auth, "get_connected_channel",
                    return_value={"title": "My Channel"},
                ):
            data = self.client.get("/youtube/status").get_json()
        self.assertTrue(data["connected"])
        self.assertEqual(data["channel"]["title"], "My Channel")

    # --- upload validation ---------------------------------------------
    def test_upload_requires_confirmation(self):
        with mock.patch.object(clip_app, "get_job_path", return_value=self.tmpdir), \
                mock.patch.object(youtube_auth, "is_configured", return_value=True), \
                mock.patch.object(youtube_auth, "load_credentials", return_value=object()):
            resp = self.client.post("/youtube/upload", json=self._payload(confirm=False))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "confirm_required")

    def test_upload_rejects_arbitrary_path(self):
        with mock.patch.object(clip_app, "get_job_path", return_value=self.tmpdir), \
                mock.patch.object(youtube_auth, "is_configured", return_value=True), \
                mock.patch.object(youtube_auth, "load_credentials", return_value=object()):
            resp = self.client.post("/youtube/upload", json=self._payload(filename="evil.mp4"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_file")

    def test_upload_requires_configuration(self):
        with mock.patch.object(youtube_auth, "is_configured", return_value=False):
            resp = self.client.post("/youtube/upload", json=self._payload())
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "not_configured")

    def test_upload_requires_connection(self):
        with mock.patch.object(youtube_auth, "is_configured", return_value=True), \
                mock.patch.object(youtube_auth, "load_credentials", return_value=None):
            resp = self.client.post("/youtube/upload", json=self._payload())
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error"], "not_connected")

    def test_upload_missing_file(self):
        empty = tempfile.mkdtemp()
        try:
            with mock.patch.object(clip_app, "get_job_path", return_value=empty), \
                    mock.patch.object(youtube_auth, "is_configured", return_value=True), \
                    mock.patch.object(youtube_auth, "load_credentials", return_value=object()):
                resp = self.client.post("/youtube/upload", json=self._payload())
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "file_missing")

    def test_upload_rejects_unknown_job(self):
        resp = self.client.post("/youtube/upload", json=self._payload(job_id=uuid.uuid4().hex))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "clip_not_ready")

    def test_upload_duplicate_prevention(self):
        clip_app.uploads_status[self.job_id] = {
            "status": "Uploading", "progress": 5, "message": "Uploading\u2026 5%",
            "video_id": None, "url": None, "error": None,
        }
        with mock.patch.object(youtube_auth, "is_configured", return_value=True), \
                mock.patch.object(youtube_auth, "load_credentials", return_value=object()):
            resp = self.client.post("/youtube/upload", json=self._payload())
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["duplicate"])
        self.assertEqual(body["status"], "Uploading")

    def test_upload_success_flow(self):
        class SyncThread:
            def __init__(self, target=None, args=(), daemon=None, **kwargs):
                self._target = target
                self._args = args

            def start(self):
                self._target(*self._args)

        with mock.patch.object(clip_app, "get_job_path", return_value=self.tmpdir), \
                mock.patch.object(clip_app.threading, "Thread", SyncThread), \
                mock.patch.object(youtube_auth, "is_configured", return_value=True), \
                mock.patch.object(youtube_auth, "load_credentials", return_value=object()), \
                mock.patch.object(
                    youtube_upload, "upload_video",
                    return_value={"video_id": "vid123", "url": "https://youtu.be/vid123", "title": "t"},
                ):
            resp = self.client.post("/youtube/upload", json=self._payload())

        self.assertEqual(resp.status_code, 200)
        status = self.client.get("/youtube/upload/status/" + self.job_id).get_json()
        self.assertEqual(status["status"], "Complete")
        self.assertEqual(status["url"], "https://youtu.be/vid123")

    def test_upload_status_unknown_job(self):
        resp = self.client.get("/youtube/upload/status/" + uuid.uuid4().hex)
        self.assertEqual(resp.status_code, 404)

    # --- OAuth routes ---------------------------------------------------
    def test_connect_redirects_to_google(self):
        with mock.patch.object(
            youtube_auth, "start_oauth_flow",
            return_value=("https://accounts.google.com/o/oauth2/auth?x=1", "state123"),
        ):
            resp = self.client.get("/youtube/connect")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("accounts.google.com", resp.headers["Location"])

    def test_connect_redirects_on_not_configured(self):
        with mock.patch.object(
            youtube_auth, "start_oauth_flow",
            side_effect=youtube_auth.YouTubeAuthError("not_configured"),
        ):
            resp = self.client.get("/youtube/connect")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("youtube_error=not_configured", resp.headers["Location"])

    def test_callback_success(self):
        with mock.patch.object(youtube_auth, "complete_oauth_flow") as complete:
            resp = self.client.get("/youtube/oauth2callback?code=abc&state=xyz")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("youtube=connected", resp.headers["Location"])
        complete.assert_called_once_with("xyz", "abc")

    def test_callback_access_denied(self):
        resp = self.client.get("/youtube/oauth2callback?error=access_denied")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("youtube_error=access_denied", resp.headers["Location"])

    def test_disconnect(self):
        with mock.patch.object(youtube_auth, "clear_credentials") as clear:
            resp = self.client.post("/youtube/disconnect")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        clear.assert_called_once()


class ParseUrlTests(unittest.TestCase):
    def test_watch(self):
        vid, err = clip_app.parse_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIsNone(err)
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_youtu_be(self):
        vid, err = clip_app.parse_youtube_url("https://youtu.be/dQw4w9WgXcQ")
        self.assertIsNone(err)
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_shorts(self):
        vid, err = clip_app.parse_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        self.assertIsNone(err)
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_live(self):
        vid, err = clip_app.parse_youtube_url("https://www.youtube.com/live/dQw4w9WgXcQ")
        self.assertIsNone(err)
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_embed(self):
        vid, err = clip_app.parse_youtube_url("https://www.youtube.com/embed/dQw4w9WgXcQ")
        self.assertIsNone(err)
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_unsupported_path(self):
        vid, err = clip_app.parse_youtube_url("https://www.youtube.com/playlist?list=PL123")
        self.assertIsNone(vid)
        self.assertIsNotNone(err)

    def test_invalid_live_id(self):
        vid, err = clip_app.parse_youtube_url("https://www.youtube.com/live/short")
        self.assertIsNone(vid)
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main(verbosity=2)