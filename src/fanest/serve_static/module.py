from pathlib import Path
from typing import Any

from fanest import Module


class ServeStaticModule:
    @staticmethod
    def for_root(
        *,
        root_path: str | None = None,
        serve_root: str = "/static",
        name: str = "static",
        roots: list[dict[str, Any]] | None = None,
        html: bool = False,
        check_dir: bool = True,
        follow_symlink: bool = False,
    ) -> type:
        assets = roots or [
            {
                "root_path": root_path,
                "serve_root": serve_root,
                "name": name,
                "html": html,
                "check_dir": check_dir,
                "follow_symlink": follow_symlink,
            }
        ]
        normalized_assets = [_normalize_asset(asset) for asset in assets]

        @Module()
        class DynamicServeStaticModule:
            pass

        setattr(DynamicServeStaticModule, "__fanest_static_assets__", normalized_assets)
        return DynamicServeStaticModule

    @staticmethod
    def for_roots(roots: list[dict[str, Any]]) -> type:
        return ServeStaticModule.for_root(roots=roots)


def _normalize_asset(asset: dict[str, Any]) -> dict[str, Any]:
    serve_root = asset.get("serve_root", asset.get("path", "/static"))
    if not isinstance(serve_root, str) or not serve_root.startswith("/"):
        raise ValueError("serve_root must start with '/'.")
    root_path = asset.get("root_path", asset.get("directory"))
    if root_path is None:
        raise ValueError("root_path is required for static assets.")
    packages = asset.get("packages")
    if packages is None:
        directory_path = Path(root_path).expanduser().resolve()
        if asset.get("check_dir", True):
            if not directory_path.exists():
                raise FileNotFoundError(f"Static assets directory not found: {directory_path}")
            if not directory_path.is_dir():
                raise NotADirectoryError(f"Static assets root must be a directory: {directory_path}")
        directory = str(directory_path)
    else:
        directory = str(root_path)
    if serve_root != "/" and serve_root.endswith("/"):
        serve_root = serve_root.rstrip("/")
    return {
        "path": serve_root,
        "directory": directory,
        "name": asset.get("name", "static"),
        "html": bool(asset.get("html", False)),
        "check_dir": bool(asset.get("check_dir", True)),
        "follow_symlink": bool(asset.get("follow_symlink", False)),
        "packages": packages,
    }
