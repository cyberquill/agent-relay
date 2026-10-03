"""Packaging relay-bridge as a VSIX without vsce."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from agent_relay.ops.vsix import EXTENSION_DIR, VsixError, build_vsix

NS = {"v": "http://schemas.microsoft.com/developer/vsx-schema/2011"}


def fake_extension(root: Path, built: bool = True) -> Path:
    ext = root / "ext"
    (ext / "out" / "src").mkdir(parents=True)
    package = {
        "name": "relay-bridge",
        "displayName": "Agent Relay <Bridge>",
        "publisher": "agent-relay",
        "version": "0.1.0",
        "description": "Phone & desk",
        "engines": {"vscode": "^1.110.0"},
        "extensionKind": ["ui"],
        "categories": ["Other"],
        "main": "./out/src/extension.js",
    }
    (ext / "package.json").write_text(json.dumps(package))
    if built:
        (ext / "out" / "src" / "extension.js").write_text("exports.activate = () => {};\n")
        (ext / "out" / "src" / "bridge.js").write_text("module.exports = {};\n")
    return ext


def test_vsix_holds_manifest_package_and_compiled_code(tmp_path: Path) -> None:
    out = build_vsix(tmp_path / "relay-bridge.vsix", fake_extension(tmp_path))
    with zipfile.ZipFile(out) as archive:
        names = sorted(archive.namelist())
        manifest = ElementTree.fromstring(archive.read("extension.vsixmanifest"))
        package = json.loads(archive.read("extension/package.json"))
    assert names == [
        "[Content_Types].xml",
        "extension.vsixmanifest",
        "extension/out/src/bridge.js",
        "extension/out/src/extension.js",
        "extension/package.json",
    ]
    identity = manifest.find("v:Metadata/v:Identity", NS)
    assert identity is not None
    assert identity.attrib == {
        "Language": "en-US",
        "Id": "relay-bridge",
        "Version": "0.1.0",
        "Publisher": "agent-relay",
    }
    display = manifest.find("v:Metadata/v:DisplayName", NS)
    assert display is not None and display.text == "Agent Relay <Bridge>"
    props = {
        p.attrib["Id"]: p.attrib["Value"] for p in manifest.iterfind("v:Metadata/v:Properties/v:Property", NS)
    }
    assert props["Microsoft.VisualStudio.Code.Engine"] == "^1.110.0"
    assert props["Microsoft.VisualStudio.Code.ExtensionKind"] == "ui"
    assert package["main"] == "./out/src/extension.js"


def test_vsix_is_reproducible(tmp_path: Path) -> None:
    ext = fake_extension(tmp_path)
    first = build_vsix(tmp_path / "a.vsix", ext).read_bytes()
    second = build_vsix(tmp_path / "b.vsix", ext).read_bytes()
    assert first == second


def test_unbuilt_extension_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VsixError, match="npm run build"):
        build_vsix(tmp_path / "x.vsix", fake_extension(tmp_path, built=False))
    with pytest.raises(VsixError, match="no package.json"):
        build_vsix(tmp_path / "x.vsix", tmp_path / "missing")


def test_the_real_extension_declares_what_the_packer_needs() -> None:
    package = json.loads((EXTENSION_DIR / "package.json").read_text())
    assert package["main"] == "./out/src/extension.js"
    assert package["activationEvents"] == ["onStartupFinished"]
    assert package["extensionKind"] == ["ui"]
