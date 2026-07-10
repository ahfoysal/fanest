from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Vite integration (@vite: dev HMR client + entrypoints, or prod manifest)
# --------------------------------------------------------------------------- #
class ViteAssets:
    def __init__(self, options: dict[str, Any] | None) -> None:
        options = options or {}
        self.dev_server: str | None = options.get("dev_server")
        self.entrypoints: list[str] = list(options.get("entrypoints", options.get("input", [])) or [])
        self.manifest_path: str | None = options.get("manifest")
        self.hot_file: str | None = options.get("hot_file")
        self.build_directory: str = options.get("build_directory", "build")
        self.react_refresh: bool = options.get("react_refresh", True)
        self._manifest: dict[str, Any] | None = None

    def is_dev(self) -> bool:
        if self.hot_file and Path(self.hot_file).exists():
            return True
        if self.manifest_path and Path(self.manifest_path).exists():
            return False
        return bool(self.dev_server)

    def _dev_url(self) -> str:
        if self.hot_file and Path(self.hot_file).exists():
            return Path(self.hot_file).read_text(encoding="utf-8").strip().rstrip("/")
        return (self.dev_server or "http://localhost:5173").rstrip("/")

    def _manifest_data(self) -> dict[str, Any]:
        manifest = self._manifest
        if manifest is None:
            if not self.manifest_path or not Path(self.manifest_path).exists():
                manifest = {}
            else:
                manifest = json.loads(Path(self.manifest_path).read_text(encoding="utf-8"))
            self._manifest = manifest
        return manifest

    def version_hash(self) -> str:
        """A content hash of the Vite manifest, so a rebuild busts the client cache."""
        if self.manifest_path and Path(self.manifest_path).exists():
            import hashlib

            return hashlib.md5(Path(self.manifest_path).read_bytes()).hexdigest()[:12]
        if self.hot_file and Path(self.hot_file).exists():
            return "dev"
        return ""

    def tags(self) -> str:
        if not self.entrypoints:
            return ""
        if self.is_dev():
            base = self._dev_url()
            tags = [f'<script type="module" src="{base}/@vite/client"></script>']
            if self.react_refresh:
                tags.append(
                    f'<script type="module">'
                    f'import RefreshRuntime from "{base}/@react-refresh";'
                    f"RefreshRuntime.injectIntoGlobalHook(window);"
                    f"window.$RefreshReg$=()=>{{}};window.$RefreshSig$=()=>(type)=>type;"
                    f"window.__vite_plugin_react_preamble_installed__=true;</script>"
                )
            for entry in self.entrypoints:
                tags.append(f'<script type="module" src="{base}/{entry}"></script>')
            return "\n".join(tags)
        # production: resolve entrypoints through the manifest
        manifest = self._manifest_data()
        base = f"/{self.build_directory.strip('/')}"
        tags = []
        seen_css: set[str] = set()
        for entry in self.entrypoints:
            chunk = manifest.get(entry)
            if chunk is None:
                continue
            for css in chunk.get("css", []):
                if css not in seen_css:
                    seen_css.add(css)
                    tags.append(f'<link rel="stylesheet" href="{base}/{css}">')
            for imported in chunk.get("imports", []):
                imported_chunk = manifest.get(imported, {})
                for css in imported_chunk.get("css", []):
                    if css not in seen_css:
                        seen_css.add(css)
                        tags.append(f'<link rel="stylesheet" href="{base}/{css}">')
            tags.append(f'<script type="module" src="{base}/{chunk["file"]}"></script>')
        return "\n".join(tags)
