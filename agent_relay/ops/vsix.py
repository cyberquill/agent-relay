"""Package the relay-bridge extension as a VSIX (a zip with a manifest), without needing `vsce`."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from agent_relay.log import get_logger

log = get_logger(__name__)

EXTENSION_DIR = Path(__file__).resolve().parents[2] / "extensions" / "relay-bridge"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension=".json" ContentType="application/json"/>
<Default Extension=".js" ContentType="application/javascript"/>
<Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""


class VsixError(RuntimeError):
    pass


def manifest(package: dict[str, object]) -> str:
    def attr(key: str, fallback: str = "") -> str:
        return quoteattr(str(package.get(key) or fallback))

    engines = package.get("engines")
    engine = str(engines.get("vscode", "*")) if isinstance(engines, dict) else "*"
    kinds = package.get("extensionKind")
    kind = ",".join(str(k) for k in kinds) if isinstance(kinds, list) else "workspace"
    categories = package.get("categories")
    category = ",".join(str(c) for c in categories) if isinstance(categories, list) else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id={attr("name")} Version={attr("version")} Publisher={attr("publisher")}/>
    <DisplayName>{escape(str(package.get("displayName") or package.get("name") or ""))}</DisplayName>
    <Description xml:space="preserve">{escape(str(package.get("description") or ""))}</Description>
    <Categories>{escape(category)}</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value={quoteattr(engine)}/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value={quoteattr(kind)}/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def _write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def build_vsix(out: Path, extension_dir: Path = EXTENSION_DIR) -> Path:
    """Zip package.json and the compiled `out/src` into `out`. Needs `npm run build` first."""
    package_file = extension_dir / "package.json"
    compiled = extension_dir / "out" / "src"
    if not package_file.is_file():
        raise VsixError(f"no package.json in {extension_dir}")
    package = json.loads(package_file.read_text())
    main = str(package.get("main") or "")
    scripts = sorted(compiled.glob("*.js")) if compiled.is_dir() else []
    if not main or not (extension_dir / main).is_file() or not scripts:
        raise VsixError(
            f"{extension_dir.name} is not built; run `npm ci && npm run build` in {extension_dir}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as archive:
        _write(archive, "[Content_Types].xml", CONTENT_TYPES.encode())
        _write(archive, "extension.vsixmanifest", manifest(package).encode())
        _write(archive, "extension/package.json", package_file.read_bytes())
        for script in scripts:
            _write(archive, f"extension/out/src/{script.name}", script.read_bytes())
    log.info(f"packaged {package.get('name')} {package.get('version')} into {out}")
    return out
