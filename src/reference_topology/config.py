"""Local configuration, independent of the current working directory."""
from __future__ import annotations

from dataclasses import InitVar, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import threading
import tomllib
from urllib.parse import urlsplit
import uuid


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ReferenceTopology"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "reference-topology"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    allowed_roots: list[Path] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8765
    engine: str = "python-rules"
    grobid_url: str = "http://127.0.0.1:8070"
    max_pdf_size_mb: int = 100
    max_pdf_pages: int = 1000
    timeout_seconds: int = 120
    maximum_nodes: int = 300
    maximum_edges: int = 1000
    maximum_depth: int = 3
    host_id: str = ""
    extra_hosts: list[str] = field(default_factory=list)
    _allow_missing_roots: InitVar[bool] = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self, _allow_missing_roots=False):
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.allowed_roots = self.validate_roots(self.allowed_roots, allow_missing=_allow_missing_roots)
        if self.engine not in {"python-rules", "grobid-local"}:
            raise ValueError("未知解析器")
        parsed = urlsplit(self.grobid_url)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password):
            raise ValueError("GROBID 地址必须是本机 loopback HTTP 服务")
        for name in ("max_pdf_size_mb", "max_pdf_pages", "timeout_seconds", "maximum_nodes", "maximum_edges", "maximum_depth"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} 必须为正数")
        for child in ("managed", "artifacts", "tmp", "backups"):
            (self.data_dir / child).mkdir(parents=True, exist_ok=True)
        self.host_id = self.host_id or str(uuid.uuid4())

    @staticmethod
    def validate_roots(roots, *, allow_missing=False) -> list[Path]:
        result = []
        for raw in roots:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                raise ValueError("论文目录必须为绝对路径")
            p = p.resolve(strict=not allow_missing)
            if not p.is_dir() and not (allow_missing and not p.exists()):
                raise ValueError(f"不是目录：{p}")
            if p not in result:
                result.append(p)
        return result

    @staticmethod
    def root_id(path: Path) -> str:
        return hashlib.sha256(str(path).encode()).hexdigest()[:16]

    def roots(self) -> list[dict]:
        with self._lock:
            return [{"id": self.root_id(p), "name": p.name or str(p), "path": str(p),
                     "available": p.is_dir()} for p in self.allowed_roots]

    def public(self) -> dict:
        return {"allowed_roots": self.roots(), "parser": {"engine": self.engine,
                "grobid_url": self.grobid_url, "timeout_seconds": self.timeout_seconds},
                "limits": {"max_pdf_size_mb": self.max_pdf_size_mb, "max_pdf_pages": self.max_pdf_pages,
                "maximum_nodes": self.maximum_nodes, "maximum_edges": self.maximum_edges,
                "maximum_depth": self.maximum_depth}, "network_enabled": False,
                "data_dir": str(self.data_dir)}

    def save(self):
        with self._lock:
            data = {"host_id": self.host_id, "allowed_roots": [str(p) for p in self.allowed_roots],
                    "engine": self.engine, "grobid_url": self.grobid_url}
            path = self.data_dir / "settings.json"
            temp = self.data_dir / f".settings-{uuid.uuid4().hex}.tmp"
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)

    def update(self, *, roots=None, engine=None):
        with self._lock:
            new_roots = self.validate_roots(roots) if roots is not None else self.allowed_roots
            if engine is not None and engine not in {"python-rules", "grobid-local"}:
                raise ValueError("未知解析器")
            self.allowed_roots = new_roots
            if engine is not None:
                self.engine = engine
            self.save()

    @classmethod
    def load(cls, *, data_dir=None, config=None, roots=None, host=None, port=None):
        options = {}
        if config:
            with Path(config).open("rb") as f:
                cfg = tomllib.load(f)
            storage = cfg.get("storage", {})
            if storage.get("data_dir"):
                options["data_dir"] = Path(storage["data_dir"])
            for key in ("host", "port"):
                if key in cfg.get("server", {}):
                    options[key] = cfg["server"][key]
            files = cfg.get("files", {})
            for key in ("allowed_roots", "max_pdf_size_mb", "max_pdf_pages"):
                if key in files:
                    options[key] = files[key]
            parser = cfg.get("parser", {})
            for key in ("engine", "timeout_seconds"):
                if key in parser:
                    options[key] = parser[key]
            options["grobid_url"] = parser.get("grobid", {}).get("base_url", "http://127.0.0.1:8070")
            for key in ("maximum_nodes", "maximum_edges", "maximum_depth"):
                if key in cfg.get("graph", {}):
                    options[key] = cfg["graph"][key]
        location = Path(data_dir or options.get("data_dir") or default_data_dir()).expanduser().resolve()
        saved = location / "settings.json"
        persisted = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
        merged = {k: v for k, v in persisted.items() if k in {"allowed_roots", "engine", "grobid_url", "host_id"}}
        merged.update(options)
        merged["data_dir"] = location
        # A moved folder or disconnected drive must not block the settings UI.
        # Only previously authorized roots may be restored while missing;
        # explicit new roots still need to exist when they are configured.
        merged["_allow_missing_roots"] = roots is None and "allowed_roots" not in options
        for name, value in (("allowed_roots", roots), ("host", host), ("port", port)):
            if value is not None:
                merged[name] = value
        settings = cls(**merged)
        settings.save()
        return settings
