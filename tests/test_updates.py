"""Tests for update management — release-based COD + git-based plugins."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers import updates as updates_module
from handlers.updates import (
    _apply_cod_update,
    _apply_plugin_update,
    _check_cod_release,
    _check_plugins,
    _git_fetch,
    _git_log_summary,
    _git_rev_parse,
    _parse_version,
    _read_local_version,
    handle_updates_apply,
    handle_updates_check,
)


# ─── Helpers ─────────────────────────────────────────────────────────

def _make_proc(stdout=b"", returncode=0):
    """Create a mock async subprocess result."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


GITHUB_RELEASE_RESPONSE = {
    'tag_name': 'v1.2.0',
    'name': 'v1.2.0',
    'body': 'Bug fixes and improvements',
    'tarball_url': 'https://api.github.com/repos/test/test/tarball/v1.2.0',
    'assets': [
        {
            'name': 'cod-v1.2.0.tar.gz',
            'browser_download_url': 'https://github.com/test/test/releases/download/v1.2.0/cod-v1.2.0.tar.gz',
        }
    ],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset module-level cache before each test."""
    updates_module._cache["result"] = None
    updates_module._cache["timestamp"] = 0
    yield
    updates_module._cache["result"] = None
    updates_module._cache["timestamp"] = 0


# ─── Version helpers ─────────────────────────────────────────────────

class TestVersionHelpers:
    def test_read_local_version(self, tmp_path):
        vf = tmp_path / "VERSION"
        vf.write_text("1.5.3\n")
        with patch("handlers.updates.VERSION_FILE", str(vf)):
            assert _read_local_version() == "1.5.3"

    def test_read_local_version_missing(self, tmp_path):
        with patch("handlers.updates.VERSION_FILE", str(tmp_path / "MISSING")):
            assert _read_local_version() == "0.0.0"

    def test_parse_version_with_v(self):
        assert _parse_version("v1.2.3") == "1.2.3"

    def test_parse_version_without_v(self):
        assert _parse_version("1.2.3") == "1.2.3"

    def test_parse_version_strips_whitespace(self):
        assert _parse_version("  v2.0.0  ") == "2.0.0"


# ─── COD: GitHub release check ──────────────────────────────────────

class TestCheckCodRelease:
    @pytest.mark.asyncio
    async def test_update_available(self):
        """Latest release newer than local VERSION → available=True."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=GITHUB_RELEASE_RESPONSE)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("handlers.updates._read_local_version", return_value="1.0.0"), \
             patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await _check_cod_release()

        assert result['available'] is True
        assert result['current'] == '1.0.0'
        assert result['latest'] == '1.2.0'
        assert 'Bug fixes' in result['summary']
        assert result['download_url'].endswith('.tar.gz')

    @pytest.mark.asyncio
    async def test_no_update_same_version(self):
        """Local matches latest → available=False."""
        release = {**GITHUB_RELEASE_RESPONSE, 'tag_name': 'v1.0.0'}
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=release)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("handlers.updates._read_local_version", return_value="1.0.0"), \
             patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await _check_cod_release()

        assert result['available'] is False

    @pytest.mark.asyncio
    async def test_api_error(self):
        """GitHub API returns non-200 → available=False, no crash."""
        mock_resp = AsyncMock()
        mock_resp.status = 404

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("handlers.updates._read_local_version", return_value="1.0.0"), \
             patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await _check_cod_release()

        assert result['available'] is False
        assert result['current'] == '1.0.0'

    @pytest.mark.asyncio
    async def test_network_error(self):
        """Network timeout → available=False, silent failure."""
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(side_effect=asyncio.TimeoutError),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("handlers.updates._read_local_version", return_value="1.0.0"), \
             patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await _check_cod_release()

        assert result['available'] is False

    @pytest.mark.asyncio
    async def test_fallback_to_tarball_url(self):
        """No assets → uses tarball_url from release."""
        release = {**GITHUB_RELEASE_RESPONSE, 'assets': []}
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=release)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("handlers.updates._read_local_version", return_value="1.0.0"), \
             patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await _check_cod_release()

        assert result['download_url'] == release['tarball_url']


# ─── Plugins: git helpers ────────────────────────────────────────────

class TestGitRevParse:
    @pytest.mark.asyncio
    async def test_returns_short_hash(self):
        proc = _make_proc(stdout=b"abc1234\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _git_rev_parse("/fake/repo", "HEAD")
        assert result == "abc1234"

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self):
        proc = _make_proc(returncode=128)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _git_rev_parse("/fake/repo", "HEAD")
        assert result is None

    @pytest.mark.asyncio
    async def test_strips_whitespace(self):
        proc = _make_proc(stdout=b"  deadbeef  \n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _git_rev_parse("/fake/repo", "origin/main")
        assert result == "deadbeef"


class TestGitFetch:
    @pytest.mark.asyncio
    async def test_success(self):
        proc = _make_proc(returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await _git_fetch("/fake/repo") is True

    @pytest.mark.asyncio
    async def test_failure(self):
        proc = _make_proc(returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await _git_fetch("/fake/repo") is False

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        proc = _make_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await _git_fetch("/fake/repo", timeout=1) is False
        proc.kill.assert_called_once()


class TestGitLogSummary:
    @pytest.mark.asyncio
    async def test_returns_log_lines(self):
        proc = _make_proc(stdout=b"abc1234 Fix bug\ndef5678 Add feature\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _git_log_summary("/fake/repo", "main")
        assert "Fix bug" in result
        assert "Add feature" in result

    @pytest.mark.asyncio
    async def test_empty_on_failure(self):
        proc = _make_proc(returncode=128)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await _git_log_summary("/fake/repo", "main") == ""


# ─── Plugins: _check_plugins ────────────────────────────────────────

class TestCheckPlugins:
    @pytest.mark.asyncio
    async def test_not_a_git_repo(self, tmp_path):
        with patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)):
            result = await _check_plugins()
        assert result is None

    @pytest.mark.asyncio
    async def test_update_available(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates._git_fetch", return_value=True), \
             patch("handlers.updates._git_rev_parse", side_effect=["abc1234", "def5678"]), \
             patch("handlers.updates._git_log_summary", return_value="def5678 New commit"):
            result = await _check_plugins()
        assert result["available"] is True
        assert result["current"] == "abc1234"
        assert result["latest"] == "def5678"

    @pytest.mark.asyncio
    async def test_no_update(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates._git_fetch", return_value=True), \
             patch("handlers.updates._git_rev_parse", return_value="abc1234"):
            result = await _check_plugins()
        assert result["available"] is False

    @pytest.mark.asyncio
    async def test_fetch_failure(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates._git_fetch", return_value=False), \
             patch("handlers.updates._git_rev_parse", return_value="abc1234"):
            result = await _check_plugins()
        assert result["available"] is False

    @pytest.mark.asyncio
    async def test_rev_parse_failure(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates._git_fetch", return_value=True), \
             patch("handlers.updates._git_rev_parse", return_value=None):
            result = await _check_plugins()
        assert result is None


# ─── handle_updates_check (HTTP) ─────────────────────────────────────

class TestHandleUpdatesCheck:
    @pytest.mark.asyncio
    async def test_returns_both_statuses(self, client):
        c = await client
        cod = {"available": True, "current": "1.0.0", "latest": "1.2.0",
               "summary": "fixes", "download_url": "https://example.com/x.tar.gz", "tag": "v1.2.0"}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins):
            resp = await c.get("/v1/updates/check")

        assert resp.status == 200
        data = await resp.json()
        assert data["cod"]["available"] is True
        assert data["plugins"]["available"] is False
        # download_url and tag must NOT be in public response
        assert "download_url" not in data["cod"]
        assert "tag" not in data["cod"]

    @pytest.mark.asyncio
    async def test_caches_result(self, client):
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod) as mock_cod, \
             patch("handlers.updates._check_plugins", return_value=plugins) as mock_plug:
            await c.get("/v1/updates/check")
            await c.get("/v1/updates/check")

        # Only called once each (second call uses cache)
        assert mock_cod.call_count == 1
        assert mock_plug.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_expires(self, client):
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod) as mock_cod, \
             patch("handlers.updates._check_plugins", return_value=plugins):
            await c.get("/v1/updates/check")
            updates_module._cache["timestamp"] = time.time() - 700
            await c.get("/v1/updates/check")

        assert mock_cod.call_count == 2

    @pytest.mark.asyncio
    async def test_both_none(self, client):
        """No releases and no plugin repo → both null."""
        c = await client
        with patch("handlers.updates._check_cod_release", return_value=None), \
             patch("handlers.updates._check_plugins", return_value=None):
            resp = await c.get("/v1/updates/check")

        data = await resp.json()
        assert data["cod"] is None
        assert data["plugins"] is None


# ─── _apply_cod_update ───────────────────────────────────────────────

class TestApplyCodUpdate:
    @pytest.mark.asyncio
    async def test_no_download_url(self):
        result = await _apply_cod_update({'available': True})
        assert result is False

    @pytest.mark.asyncio
    async def test_download_http_error(self):
        mock_resp = AsyncMock()
        mock_resp.status = 404

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await _apply_cod_update({'download_url': 'https://example.com/x.tar.gz'})
        assert result is False

    @pytest.mark.asyncio
    async def test_download_network_error(self):
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(side_effect=asyncio.TimeoutError),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await _apply_cod_update({'download_url': 'https://example.com/x.tar.gz'})
        assert result is False

    @pytest.mark.asyncio
    async def test_successful_extraction(self, tmp_path):
        """Simulates downloading and extracting a tarball."""
        import tarfile
        import io

        # Create a fake tarball with VERSION + handler + static
        cod_dir = tmp_path / "cod"
        cod_dir.mkdir()
        (cod_dir / "VERSION").write_text("1.0.0\n")

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode='w:gz') as tar:
            # Add a VERSION file
            ver_data = b"1.2.0\n"
            info = tarfile.TarInfo(name="release/VERSION")
            info.size = len(ver_data)
            tar.addfile(info, io.BytesIO(ver_data))

            # Add a Python file
            py_data = b"# updated server\n"
            info = tarfile.TarInfo(name="release/server.py")
            info.size = len(py_data)
            tar.addfile(info, io.BytesIO(py_data))

        tarball_bytes = tar_buf.getvalue()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=tarball_bytes)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session_ctx = AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx), \
             patch("handlers.updates.COD_DIR", str(cod_dir)):
            result = await _apply_cod_update({'download_url': 'https://example.com/x.tar.gz'})

        assert result is True
        assert (cod_dir / "VERSION").read_text().strip() == "1.2.0"
        assert (cod_dir / "server.py").read_text() == "# updated server\n"


# ─── _apply_plugin_update ───────────────────────────────────────────

class TestApplyPluginUpdate:
    @pytest.mark.asyncio
    async def test_success_with_changes(self, tmp_path):
        hash_file = tmp_path / "build_hash"
        hash_file.write_text("old")

        reset_proc = _make_proc(returncode=0)
        install_proc = _make_proc(returncode=0)

        with patch("handlers.updates.BUILD_HASH_FILE", str(hash_file)), \
             patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates.OPENPILOT_DIR", str(tmp_path)), \
             patch("handlers.updates._git_rev_parse", side_effect=["aaa", "bbb"]), \
             patch("handlers.updates._get_target_branch", return_value="dev"), \
             patch("asyncio.create_subprocess_exec", side_effect=[reset_proc, install_proc]), \
             patch("os.path.isfile", return_value=True):
            result = await _apply_plugin_update()

        assert result["ok"] is True
        assert result["changed"] is True
        assert not hash_file.exists()

    @pytest.mark.asyncio
    async def test_success_no_changes(self, tmp_path):
        hash_file = tmp_path / "build_hash"
        hash_file.write_text("keep")

        reset_proc = _make_proc(returncode=0)

        with patch("handlers.updates.BUILD_HASH_FILE", str(hash_file)), \
             patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates.OPENPILOT_DIR", str(tmp_path)), \
             patch("handlers.updates._git_rev_parse", return_value="same"), \
             patch("handlers.updates._get_target_branch", return_value="dev"), \
             patch("asyncio.create_subprocess_exec", return_value=reset_proc), \
             patch("os.path.isfile", return_value=False):
            result = await _apply_plugin_update()

        assert result["ok"] is True
        assert result["changed"] is False
        assert hash_file.exists()

    @pytest.mark.asyncio
    async def test_reset_failure(self, tmp_path):
        proc = _make_proc(stdout=b"error: reset failed", returncode=1)

        with patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates._git_rev_parse", return_value="aaa"), \
             patch("handlers.updates._get_target_branch", return_value="dev"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _apply_plugin_update()

        assert result["ok"] is False
        assert "reset failed" in result["error"]

    @pytest.mark.asyncio
    async def test_install_failure(self, tmp_path):
        reset_proc = _make_proc(returncode=0)
        install_proc = _make_proc(stdout=b"install.sh: error", returncode=1)

        with patch("handlers.updates.PLUGIN_REPO_DIR", str(tmp_path)), \
             patch("handlers.updates.OPENPILOT_DIR", str(tmp_path)), \
             patch("handlers.updates._git_rev_parse", return_value="aaa"), \
             patch("handlers.updates._get_target_branch", return_value="dev"), \
             patch("asyncio.create_subprocess_exec", side_effect=[reset_proc, install_proc]), \
             patch("os.path.isfile", return_value=True):
            result = await _apply_plugin_update()

        assert result["ok"] is False
        assert "install.sh" in result["error"]


# ─── handle_updates_apply (HTTP) ─────────────────────────────────────

class TestHandleUpdatesApply:
    @pytest.mark.asyncio
    async def test_no_updates(self, client):
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins):
            resp = await c.post("/v1/updates/apply")

        data = await resp.json()
        assert data["cod_updated"] is False
        assert data["plugins_updated"] is False
        assert data["reboot_required"] is False

    @pytest.mark.asyncio
    async def test_cod_only(self, client):
        c = await client
        cod = {"available": True, "current": "1.0.0", "latest": "1.2.0",
               "download_url": "https://example.com/x.tar.gz"}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins), \
             patch("handlers.updates._apply_cod_update", return_value=True), \
             patch("handlers.updates._restart_server"):
            resp = await c.post("/v1/updates/apply")

        data = await resp.json()
        assert data["cod_updated"] is True
        assert data["plugins_updated"] is False

    @pytest.mark.asyncio
    async def test_plugins_only(self, client):
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": True, "current": "abc", "latest": "def", "summary": "new"}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins), \
             patch("handlers.updates._apply_plugin_update", return_value={"ok": True, "changed": True}):
            resp = await c.post("/v1/updates/apply")

        data = await resp.json()
        assert data["cod_updated"] is False
        assert data["plugins_updated"] is True
        assert data["reboot_required"] is True

    @pytest.mark.asyncio
    async def test_both(self, client):
        c = await client
        cod = {"available": True, "current": "1.0.0", "latest": "1.2.0",
               "download_url": "https://example.com/x.tar.gz"}
        plugins = {"available": True, "current": "abc", "latest": "def", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins), \
             patch("handlers.updates._apply_cod_update", return_value=True), \
             patch("handlers.updates._apply_plugin_update", return_value={"ok": True, "changed": True}), \
             patch("handlers.updates._restart_server"):
            resp = await c.post("/v1/updates/apply")

        data = await resp.json()
        assert data["cod_updated"] is True
        assert data["plugins_updated"] is True
        assert data["reboot_required"] is True

    @pytest.mark.asyncio
    async def test_clears_cache(self, client):
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        updates_module._cache["result"] = {"cod": cod, "plugins": plugins}
        updates_module._cache["timestamp"] = time.time()

        await c.post("/v1/updates/apply")

        assert updates_module._cache["result"] is None
        assert updates_module._cache["timestamp"] == 0

    @pytest.mark.asyncio
    async def test_uses_cache(self, client):
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        updates_module._cache["result"] = {"cod": cod, "plugins": plugins}
        updates_module._cache["timestamp"] = time.time()

        with patch("handlers.updates._check_cod_release") as mock_cod:
            resp = await c.post("/v1/updates/apply")

        assert resp.status == 200
        mock_cod.assert_not_called()

    @pytest.mark.asyncio
    async def test_cod_apply_failure(self, client):
        c = await client
        cod = {"available": True, "current": "1.0.0", "latest": "1.2.0",
               "download_url": "https://example.com/x.tar.gz"}
        plugins = {"available": False, "current": "abc", "latest": "abc", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins), \
             patch("handlers.updates._apply_cod_update", return_value=False):
            resp = await c.post("/v1/updates/apply")

        data = await resp.json()
        assert data["cod_updated"] is False

    @pytest.mark.asyncio
    async def test_plugin_apply_failure(self, client):
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": True, "current": "abc", "latest": "def", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins), \
             patch("handlers.updates._apply_plugin_update", return_value={"ok": False, "error": "boom"}):
            resp = await c.post("/v1/updates/apply")

        data = await resp.json()
        assert data["plugins_updated"] is False
        assert data["reboot_required"] is False

    @pytest.mark.asyncio
    async def test_restart_only_on_cod_update(self, client):
        """Server restart NOT scheduled when only plugins updated."""
        c = await client
        cod = {"available": False, "current": "1.0.0", "latest": "1.0.0", "summary": ""}
        plugins = {"available": True, "current": "abc", "latest": "def", "summary": ""}

        with patch("handlers.updates._check_cod_release", return_value=cod), \
             patch("handlers.updates._check_plugins", return_value=plugins), \
             patch("handlers.updates._apply_plugin_update", return_value={"ok": True, "changed": True}), \
             patch("handlers.updates._restart_server") as mock_restart:
            await c.post("/v1/updates/apply")

        mock_restart.assert_not_called()
