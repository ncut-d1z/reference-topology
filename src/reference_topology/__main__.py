"""Run the local workbench or restore a verified backup into a new directory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import Settings
from .storage import StoreError


def main():
    # Windows consoles and redirected CI pipes can use cp1252 (or another
    # legacy codepage) that cannot represent Chinese startup messages and
    # Unicode filesystem paths. Preserve the console's encoding, escaping
    # only characters it cannot encode instead of aborting startup.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="本地 PDF 与引用拓扑工作台")
    parser.add_argument("--config", type=Path, help="可选 TOML 配置文件")
    parser.add_argument("--data-dir", type=Path, help="本地数据目录（默认平台用户数据目录）")
    parser.add_argument("--root", action="append", help="允许的论文绝对目录，可重复指定")
    parser.add_argument("--host", default=None, help="默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="默认 8765")
    parser.add_argument("--allow-host", action="append", default=[], help="显式允许的 HTTP Host（仅专用反向代理验证使用）")
    parser.add_argument("--check", action="store_true", help="检查配置与数据库后退出")
    parser.add_argument("--backup", action="store_true", help="创建完整本地备份后退出")
    parser.add_argument("--restore", type=Path, help="恢复备份至 --data-dir 指定的新目录")
    args = parser.parse_args()
    try:
        if args.restore:
            if not args.data_dir:
                parser.error("恢复时必须用 --data-dir 指定空目录或新目录")
            from .maintenance import restore_backup
            print(json.dumps(restore_backup(args.restore, args.data_dir), ensure_ascii=False, indent=2))
            return
        settings = Settings.load(data_dir=args.data_dir, config=args.config, roots=args.root, host=args.host, port=args.port)
        settings.extra_hosts = args.allow_host
        from .storage import Store
        if args.check or args.backup:
            store = Store(settings.data_dir / "reference_topology.sqlite3")
            if args.backup:
                from .maintenance import create_backup
                result = create_backup(settings, store)
            else:
                result = {"status": "ok", "schema_version": store.SCHEMA_VERSION,
                          "data_dir": str(settings.data_dir), "roots": settings.roots(), "parser": settings.engine}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        from .app import create_app
        import uvicorn
        print(f"Reference Topology · http://{settings.host}:{settings.port}")
        print(f"本地数据目录：{settings.data_dir}")
        app = create_app(settings)
        config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info",
                                proxy_headers=False)
        server = uvicorn.Server(config)
        # The web shutdown button requests Server.should_exit, not os._exit;
        # shutdown then runs the normal lifespan cleanup and worker.stop().
        app.state.uvicorn_server = server
        server.run()
    except (ValueError, OSError, StoreError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
