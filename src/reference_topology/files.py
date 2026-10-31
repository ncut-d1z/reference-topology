"""Registered PDF access: paths never serve as public file identifiers."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from .config import Settings


class FileError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class FileService:
    def __init__(self, settings: Settings, store):
        self.settings, self.store = settings, store

    def root(self, root_id: str) -> Path:
        for item in self.settings.roots():
            if item["id"] == root_id:
                return Path(item["path"])
        raise FileError("path_not_allowed", "目录未授权或已从设置移除", 403)

    def resolve(self, root_id: str, relative: str) -> Path:
        root = self.root(root_id)
        if Path(relative).is_absolute() or "\x00" in relative:
            raise FileError("path_not_allowed", "请选择允许目录内的相对路径", 403)
        try:
            path = (root / relative).resolve(strict=True)
            path.relative_to(root)
        except FileNotFoundError:
            raise FileError("file_missing", "文件或目录不存在", 404)
        except (ValueError, RuntimeError):
            raise FileError("path_not_allowed", "路径或符号链接超出允许目录", 403)
        return path

    def browse(self, root_id: str, relative: str = "", offset: int = 0) -> dict:
        root = self.root(root_id)
        path = self.resolve(root_id, relative)
        if not path.is_dir():
            raise FileError("invalid_directory", "选择的位置不是目录")
        result = []
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            raise FileError("path_not_allowed", "无法读取此目录", 403)
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                resolved = child.resolve(strict=True)
                resolved.relative_to(root)
                if child.is_dir() or (child.is_file() and child.suffix.lower() == ".pdf"):
                    result.append({"name": child.name, "path": child.relative_to(root).as_posix(),
                                   "type": "directory" if child.is_dir() else "pdf",
                                   "size": child.stat().st_size if child.is_file() else None})
            except (OSError, ValueError, RuntimeError):
                continue
        rel = path.relative_to(root).as_posix()
        return {"path": "" if rel == "." else rel,
                "parent": path.parent.relative_to(root).as_posix() if path != root else None,
                "items": result[offset:offset+500], "total": len(result), "truncated": offset+500 < len(result)}

    def _inspect(self, path: Path, *, snapshot: bool = False):
        limit = self.settings.max_pdf_size_mb * 1024 * 1024
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        temporary = None
        try:
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as f:
                before = os.fstat(f.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise FileError("invalid_pdf", "不是普通 PDF 文件")
                if before.st_size > limit:
                    raise FileError("file_too_large", "PDF 超过配置的大小上限", 413)
                header = f.read(1024)
                if b"%PDF-" not in header:
                    raise FileError("invalid_pdf", "文件不包含有效 PDF 文件头")
                f.seek(0)
                digest, size = hashlib.sha256(), 0
                output = None
                if snapshot:
                    n, name = tempfile.mkstemp(suffix=".pdf", prefix="read-", dir=self.settings.data_dir / "tmp")
                    temporary = Path(name)
                    output = os.fdopen(n, "wb")
                try:
                    while chunk := f.read(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise FileError("file_too_large", "PDF 超过配置的大小上限", 413)
                        digest.update(chunk)
                        if output:
                            output.write(chunk)
                    after = os.fstat(f.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise FileError("content_changed", "读取期间文件发生变化，请重试", 409)
                finally:
                    if output:
                        output.close()
                return digest.hexdigest(), size, temporary
        except FileNotFoundError:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise FileError("file_missing", "原 PDF 已移动或不存在", 404)
        except Exception:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise

    def register(self, root_id: str, relative: str) -> dict:
        path = self.resolve(root_id, relative)
        if path.suffix.lower() != ".pdf":
            raise FileError("invalid_pdf", "请选择 PDF 文件")
        sha, size, _ = self._inspect(path)
        original = (self.root(root_id) / relative).absolute().as_uri()
        return self.store.register_asset(sha256=sha, byte_size=size, canonical_uri=path.as_uri(),
                original_uri=original, display_name=path.name, host_id=self.settings.host_id,
                root_key=root_id, location_kind="linked")

    def register_upload(self, temporary: Path, display_name: str) -> dict:
        sha, size, _ = self._inspect(temporary)
        destination = self.settings.data_dir / "managed" / f"{sha}.pdf"
        if not destination.exists():
            os.replace(temporary, destination)
        else:
            existing, _, _ = self._inspect(destination)
            if existing != sha:
                raise FileError("content_changed", "托管文件内容异常，请检查数据目录", 409)
            temporary.unlink(missing_ok=True)
        clean_name = display_name.replace("\\", "/").rsplit("/", 1)[-1]
        clean_name = "".join(c for c in clean_name if c.isprintable())[:240] or "document.pdf"
        return self.store.register_asset(sha256=sha, byte_size=size, canonical_uri=destination.as_uri(),
                original_uri=None, display_name=clean_name, host_id=self.settings.host_id,
                root_key="managed", location_kind="managed")

    def location_path(self, location: dict) -> Path:
        if location["host_id"] != self.settings.host_id:
            raise FileError("file_missing", "此位置属于另一台电脑，需要重新绑定", 404)
        uri = urlsplit(location["canonical_uri"])
        if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
            raise FileError("path_not_allowed", "不支持此文件位置", 403)
        path = Path(url2pathname(uri.path)).resolve(strict=True)
        root = self.settings.data_dir / "managed" if location["location_kind"] == "managed" else self.root(location["root_key"])
        try:
            path.relative_to(root)
        except ValueError:
            raise FileError("path_not_allowed", "文件位置超出允许目录", 403)
        return path

    def snapshot(self, file_id: str) -> tuple[Path, dict]:
        asset = self.store.get_asset(file_id)
        last_error = FileError("file_missing", "没有可访问的位置，请重新绑定 PDF", 404)
        for location in asset["locations"]:
            if not location.get("is_current", True):
                continue
            try:
                path = self.location_path(location)
                sha, _, temporary = self._inspect(path, snapshot=True)
                if sha != asset["sha256"]:
                    temporary.unlink(missing_ok=True)
                    self.store.set_location_status(location["id"], "changed")
                    last_error = FileError("content_changed", "原位置已经是不同内容，请重新登记或绑定", 409)
                    continue
                self.store.set_location_status(location["id"], "available")
                return temporary, location
            except FileNotFoundError:
                self.store.set_location_status(location["id"], "missing")
                if last_error.code != "content_changed":
                    last_error = FileError("file_missing", "原 PDF 已移动或不存在", 404)
            except (PermissionError, FileError) as error:
                code = getattr(error, "code", "path_not_allowed")
                self.store.set_location_status(location["id"], "missing" if code == "file_missing" else "inaccessible")
                if last_error.code != "content_changed":
                    last_error = error if isinstance(error, FileError) else FileError("path_not_allowed", "没有读取文件的权限", 403)
        raise last_error

    def rebind(self, file_id: str, root_id: str, relative: str) -> dict:
        asset = self.store.get_asset(file_id)
        path = self.resolve(root_id, relative)
        sha, size, _ = self._inspect(path)
        if sha != asset["sha256"]:
            raise FileError("content_changed", "所选文件与原 PDF 的内容哈希不同", 409)
        return self.store.register_asset(sha256=sha, byte_size=size, canonical_uri=path.as_uri(),
                original_uri=(self.root(root_id) / relative).absolute().as_uri(), display_name=path.name,
                host_id=self.settings.host_id, root_key=root_id, location_kind="linked")
