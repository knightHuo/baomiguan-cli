import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from baomiguan.updates import UpdateError, UpdateService

REPO = "knightHuo/baomiguan-cli"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD = f"https://github.com/{REPO}/releases/download/v2.1.0/"
PROGRAM = b"verified test program"
DIGEST = hashlib.sha256(PROGRAM).hexdigest()


class Response:
    def __init__(self, data=None, content=b"", status=200, headers=None):
        self.data = data
        self.content = content
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


def release(*, digest=f"sha256:{DIGEST}", url=None):
    asset = {
        "name": "Baomiguan.exe",
        "size": len(PROGRAM),
        "browser_download_url": url or DOWNLOAD + "Baomiguan.exe",
    }
    if digest:
        asset["digest"] = digest
    return {"tag_name": "v2.1.0", "body": "New release", "assets": [asset]}


def service(tmp_path, data=None, responses=None):
    routes = {API: Response(data=release() if data is None else data)}
    routes.update(responses or {})
    session = Session(routes)
    return UpdateService(session=session, download_dir=tmp_path), session


def test_check_compares_numeric_versions(tmp_path):
    data = release()
    data["tag_name"] = "v2.10.0"
    data["assets"] = []
    updater, session = service(tmp_path, data=data)
    updater.version = "2.9.0"
    result = updater.check()
    assert result["available"] is True
    assert result["latestVersion"] == "2.10.0"
    assert result["releaseUrl"] == f"https://github.com/{REPO}/releases/tag/v2.10.0"
    assert session.calls[0][1]["allow_redirects"] is False


def test_missing_first_release_is_normal(tmp_path):
    updater, _ = service(tmp_path, responses={API: Response(status=404)})
    assert updater.check()["available"] is False
    assert "暂时没有" in updater.check()["message"]


def test_network_failure_does_not_expose_sensitive_error(tmp_path):
    updater, _ = service(tmp_path, responses={API: requests.ConnectionError("secret")})
    result = updater.check()
    assert result["available"] is False
    assert "secret" not in result["message"]


@pytest.mark.parametrize("url", [
    "http://github.com/knightHuo/baomiguan-cli/releases/download/v2.1.0/Baomiguan.exe",
    "https://github.com/other/repo/releases/download/v2.1.0/Baomiguan.exe",
    DOWNLOAD + "../Baomiguan.exe",
    DOWNLOAD + "%2e%2e%2fBaomiguan.exe",
    DOWNLOAD + "Baomiguan.exe?redirect=evil",
    "https://github.com.evil.invalid/knightHuo/baomiguan-cli/releases/download/v2.1.0/Baomiguan.exe",
])
def test_check_rejects_untrusted_asset_urls(tmp_path, url):
    updater, _ = service(tmp_path, data=release(url=url))
    assert updater.check()["available"] is False
    assert list(tmp_path.iterdir()) == []


def test_verified_download_is_saved_without_replacing_running_app(tmp_path):
    updater, _ = service(tmp_path, responses={
        DOWNLOAD + "Baomiguan.exe": Response(content=PROGRAM),
    })
    result = updater.download()
    assert result["version"] == "2.1.0"
    assert (tmp_path / "Baomiguan-2.1.0.exe").read_bytes() == PROGRAM
    assert "关闭当前程序" in result["message"]
    assert not list(tmp_path.glob("*.part"))


def test_manifest_fallback_is_supported(tmp_path):
    data = release(digest=None)
    data["assets"].append({
        "name": "SHA256SUMS",
        "browser_download_url": DOWNLOAD + "SHA256SUMS",
    })
    updater, _ = service(tmp_path, data=data, responses={
        DOWNLOAD + "SHA256SUMS": Response(content=f"{DIGEST}  Baomiguan.exe\n".encode()),
        DOWNLOAD + "Baomiguan.exe": Response(content=PROGRAM),
    })
    assert updater.download()["version"] == "2.1.0"


def test_missing_checksum_prevents_executable_request(tmp_path):
    updater, session = service(tmp_path, data=release(digest=None))
    with pytest.raises(UpdateError, match="缺少 SHA-256"):
        updater.download()
    assert [url for url, _ in session.calls] == [API]
    assert list(tmp_path.iterdir()) == []


def test_checksum_mismatch_removes_partial_file(tmp_path):
    updater, _ = service(tmp_path, data=release(digest="sha256:" + "0" * 64), responses={
        DOWNLOAD + "Baomiguan.exe": Response(content=PROGRAM),
    })
    with pytest.raises(UpdateError, match="校验失败"):
        updater.download()
    assert list(tmp_path.iterdir()) == []


def test_download_follows_only_github_https_cdn(tmp_path):
    cdn = "https://release-assets.githubusercontent.com/github-production-release-asset/test?token=x"
    updater, session = service(tmp_path, responses={
        DOWNLOAD + "Baomiguan.exe": Response(status=302, headers={"Location": cdn}),
        cdn: Response(content=PROGRAM),
    })
    assert updater.download()["version"] == "2.1.0"
    assert len(session.calls) == 3
    assert all(not kwargs["allow_redirects"] for _, kwargs in session.calls)


@pytest.mark.parametrize("target", [
    "http://release-assets.githubusercontent.com/file",
    "https://example.com/file",
    "https://github.com/other/repo/releases/download/v2.1.0/file",
    DOWNLOAD + "../../../../other/repo/Baomiguan.exe",
])
def test_untrusted_redirect_is_never_requested(tmp_path, target):
    updater, session = service(tmp_path, responses={
        DOWNLOAD + "Baomiguan.exe": Response(status=302, headers={"Location": target}),
    })
    with pytest.raises(UpdateError):
        updater.download()
    assert target not in [url for url, _ in session.calls]
    assert list(tmp_path.iterdir()) == []


def test_older_release_is_not_downloaded(tmp_path):
    updater, session = service(tmp_path)
    updater.version = "3.0.0"
    with pytest.raises(UpdateError, match="最新版本"):
        updater.download()
    assert len(session.calls) == 1


def test_filename_traversal_is_not_downloaded(tmp_path):
    data = release()
    data["assets"][0]["name"] = "../Baomiguan.exe"
    updater, session = service(tmp_path, data=data)
    with pytest.raises(UpdateError, match="Windows"):
        updater.download()
    assert len(session.calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_background_check_cannot_clear_release_between_download_check_and_snapshot(tmp_path, monkeypatch):
    updater, session = service(tmp_path, responses={
        DOWNLOAD + "Baomiguan.exe": Response(content=PROGRAM),
    })
    original_get = session.get
    api_responses = iter([Response(data=release()), Response(status=404)])

    def get(url, **kwargs):
        if url == API:
            session.calls.append((url, kwargs))
            return next(api_responses)
        return original_get(url, **kwargs)

    monkeypatch.setattr(session, "get", get)
    original_check = updater.check
    download_checked = threading.Event()
    resume_download = threading.Event()
    background_started = threading.Event()
    background_finished = threading.Event()
    role = threading.local()

    def check():
        downloading = getattr(role, "downloading", False)
        if not downloading:
            background_started.set()
        result = original_check()
        if downloading:
            download_checked.set()
            assert resume_download.wait(3)
        else:
            background_finished.set()
        return result

    monkeypatch.setattr(updater, "check", check)

    def download():
        role.downloading = True
        return updater.download()

    with ThreadPoolExecutor(max_workers=2) as executor:
        download_future = executor.submit(download)
        try:
            assert download_checked.wait(3)
            check_future = executor.submit(updater.check)
            assert background_started.wait(3)
            assert not background_finished.wait(0.15)
            assert updater._release is not None
        finally:
            resume_download.set()
        assert download_future.result(timeout=3)["version"] == "2.1.0"
        assert check_future.result(timeout=3)["available"] is False
    assert (tmp_path / "Baomiguan-2.1.0.exe").read_bytes() == PROGRAM
