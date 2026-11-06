"""Check GitHub releases and download verified updates without replacing this app."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_DOWNLOAD = 250 * 1024 * 1024
_APP_NAME = "Baomiguan.exe"


class UpdateError(ValueError):
    """An update could not be safely retrieved or verified."""


def _version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if not match:
        raise UpdateError("发行版本号无效。")
    return tuple(int(match.group(i)) for i in (1, 2, 3))


def _updates_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Baomiguan" / "updates"


class UpdateService:
    """Only the configured repository's release files may initiate downloads."""

    def __init__(
        self,
        repo: str = "knightHuo/baomiguan-cli",
        version: str = "2.0.0",
        *,
        session: requests.Session | None = None,
        download_dir: Path | None = None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise UpdateError("更新仓库名称无效。")
        self.repo = repo
        self.version = version
        _version(version)
        self.session = session or requests.Session()
        self.download_dir = Path(download_dir) if download_dir else _updates_dir()
        self._release: dict | None = None
        self._lock = threading.RLock()

    def check(self) -> dict:
        with self._lock:
            return self._check()

    def _check(self) -> dict:
        result = {
            "available": False,
            "currentVersion": self.version,
            "latestVersion": self.version,
            "releaseUrl": f"https://github.com/{self.repo}/releases",
            "notes": "",
            "assets": [],
        }
        self._release = None
        try:
            response = self.session.get(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
                timeout=(5, 20),
                allow_redirects=False,
            )
            try:
                if response.status_code == 404:
                    result["message"] = "暂时没有已发布的新版本。"
                    return result
                response.raise_for_status()
                if response.status_code != 200:
                    raise UpdateError("更新服务返回了意外状态。")
                release = response.json()
            finally:
                response.close()
            if not isinstance(release, dict):
                raise UpdateError("发行信息格式无效。")
            tag = str(release.get("tag_name", ""))
            latest = _version(tag)
            if release.get("draft") or release.get("prerelease") or "-" in tag:
                raise UpdateError("最新发行不是稳定版本。")
            assets = release.get("assets", [])
            if not isinstance(assets, list):
                raise UpdateError("发行附件格式无效。")
            safe_assets = []
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name", ""))
                if name not in {_APP_NAME, "SHA256SUMS"}:
                    continue
                self._asset_url(asset, tag)
                safe_assets.append(asset)
            self._release = {"tag": tag, "assets": safe_assets}
            result.update(
                available=latest > _version(self.version),
                latestVersion=tag.removeprefix("v"),
                releaseUrl=f"https://github.com/{self.repo}/releases/tag/{quote(tag, safe='')}",
                notes=str(release.get("body") or "")[:20000],
                assets=[
                    {"name": a["name"], "size": a.get("size", 0)} for a in safe_assets
                ],
            )
            if not result["available"]:
                result["message"] = "当前已是最新版本。"
        except (requests.RequestException, ValueError, TypeError):
            self._release = None
            result["message"] = "暂时无法检查更新，请稍后重试或打开项目发行页。"
        return result

    def _asset_url(self, asset: dict, tag: str) -> str:
        name = asset.get("name", "")
        if not isinstance(name, str) or name not in {_APP_NAME, "SHA256SUMS"}:
            raise UpdateError("更新附件名称无效。")
        url = str(asset.get("browser_download_url", ""))
        parsed = urlparse(url)
        expected = f"/{self.repo}/releases/download/{tag}/{name}"
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or unquote(parsed.path) != expected
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise UpdateError("更新附件不属于指定仓库。")
        return url

    def _stream(self, url: str, tag: str):
        """Follow GitHub's HTTPS asset redirect, never an arbitrary redirect."""
        for _ in range(6):
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or parsed.hostname
                not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
            ):
                raise UpdateError("更新下载地址不受信任。")
            if parsed.hostname == "github.com":
                name = unquote(parsed.path).rsplit("/", 1)[-1]
                self._asset_url({"name": name, "browser_download_url": url}, tag)
            response = self.session.get(
                url, timeout=(5, 60), stream=True, allow_redirects=False
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                if not location:
                    raise UpdateError("更新下载重定向无效。")
                url = urljoin(url, location)
                continue
            try:
                response.raise_for_status()
                if response.status_code != 200:
                    raise UpdateError("更新下载返回了意外状态。")
            except Exception:
                response.close()
                raise
            return response
        raise UpdateError("更新下载重定向次数过多。")

    def _checksum(self, asset: dict, release: dict) -> str:
        digest = asset.get("digest")
        if digest:
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise UpdateError("更新附件的校验格式无效。")
            value = digest.removeprefix("sha256:")
            if not _SHA256.fullmatch(value):
                raise UpdateError("更新附件的 SHA-256 无效。")
            return value.lower()
        manifests = [a for a in release["assets"] if a["name"] == "SHA256SUMS"]
        if len(manifests) != 1:
            raise UpdateError("发行缺少 SHA-256 校验，已取消下载。")
        response = self._stream(self._asset_url(manifests[0], release["tag"]), release["tag"])
        try:
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                chunks.extend(chunk)
                if len(chunks) > 1024 * 1024:
                    raise UpdateError("发行校验文件过大。")
            content = chunks.decode("utf-8-sig")
        finally:
            response.close()
        matches = []
        for line in content.splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
            if match and match.group(2) == asset["name"]:
                matches.append(match.group(1).lower())
        if len(matches) != 1:
            raise UpdateError("发行校验文件中没有唯一的程序校验值。")
        return matches[0]

    def download(self) -> dict:
        with self._lock:
            info = self.check()
            release = self._release
            if not info["available"] or release is None:
                raise UpdateError(info.get("message", "没有可下载的新版本。"))
            assets = [a for a in release["assets"] if a["name"] == _APP_NAME]
            if len(assets) != 1:
                raise UpdateError("发行缺少唯一的 Windows 程序附件。")
            asset = assets[0]
            expected = self._checksum(asset, release)
            self.download_dir.mkdir(parents=True, exist_ok=True)
            target = self.download_dir / f"Baomiguan-{info['latestVersion']}.exe"
            fd, temporary = tempfile.mkstemp(prefix=".update-", suffix=".part", dir=self.download_dir)
            try:
                with os.fdopen(fd, "wb") as output:
                    response = self._stream(self._asset_url(asset, release["tag"]), release["tag"])
                    try:
                        checksum = hashlib.sha256()
                        size = 0
                        for chunk in response.iter_content(chunk_size=128 * 1024):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > _MAX_DOWNLOAD:
                                raise UpdateError("更新程序超过下载大小限制。")
                            checksum.update(chunk)
                            output.write(chunk)
                    finally:
                        response.close()
                if not size or (asset.get("size") and size != asset["size"]):
                    raise UpdateError("更新程序下载不完整。")
                if checksum.hexdigest() != expected:
                    raise UpdateError("更新程序 SHA-256 校验失败，已取消保存。")
                os.replace(temporary, target)
            except (requests.RequestException, OSError) as exc:
                raise UpdateError("更新下载失败，请检查网络或目录权限。") from exc
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {
                "path": str(target),
                "version": info["latestVersion"],
                "message": "新版本已下载并通过 SHA-256 校验。关闭当前程序后，启动下载的程序完成切换。",
            }
