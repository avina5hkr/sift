"""Write THIRD_PARTY.md: the licences of everything Sift.app ships or downloads.

Python: the runtime dependency closure (`uv export --no-dev`), licence from package metadata.
Web: the production dependencies in web/package.json and what they pull in (node_modules).
Downloads: platform-tools and the models, from their own terms.
Run: uv run python scripts/third_party.py
"""

import importlib.metadata as md
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Where package metadata understates what the wheel ships.
OVERRIDES = {
    "pi-heif": "BSD-3-Clause; its macOS wheel bundles libheif and the libde265 decoder "
    "(LGPL-3.0) as shared libraries; it has no encoder",
}
NATIVE = [
    ("CPython", "3.14", "PSF-2.0"),
    ("PyInstaller bootloader", "6.22.3", "GPL-2.0 with the bootloader exception"),
    ("ONNX Runtime native library", "via onnxruntime", "MIT"),
    ("libheif, libde265", "via pi-heif", "LGPL-3.0"),
    ("libjpeg-turbo, libpng, libtiff, libwebp, zlib, freetype", "via Pillow",
     "BSD-style, zlib, FreeType (permissive)"),
    ("Hugging Face tokenizers (Rust)", "via tokenizers", "Apache-2.0"),
]  # fmt: skip


def python_packages() -> list[tuple[str, str, str]]:
    out = subprocess.run(
        ["uv", "export", "--no-dev", "--no-hashes", "--format", "requirements-txt", "--frozen"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    names = sorted({m.group(1) for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", out, re.M)} - {"sift"})
    rows = []
    for name in names:
        try:
            meta = md.metadata(name)
        except md.PackageNotFoundError:
            continue
        lic = meta.get("License-Expression") or meta.get("License") or ""
        if not lic or len(lic) > 60:
            classes = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                       if c.startswith("License ::")]  # fmt: skip
            lic = ", ".join(classes) or (lic[:60] + "..." if lic else "see package")
        lic = OVERRIDES.get(name.lower(), lic)
        rows.append((name, md.version(name), lic.replace("\n", " ")))
    return rows


def web_packages() -> list[tuple[str, str, str]]:
    pkg = json.loads((ROOT / "web" / "package.json").read_text())
    todo, seen, rows = list(pkg.get("dependencies", {})), set(), []
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        meta_path = ROOT / "web" / "node_modules" / name / "package.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        lic = meta.get("license") or (meta.get("licenses") or [{}])[0].get("type", "see package")
        rows.append((name, meta.get("version", "?"), str(lic)))
        todo += list(meta.get("dependencies", {}))
    return sorted(rows)


def main() -> None:
    lines = [
        "# Third-party software",
        "",
        "Sift.app bundles the software below, each under its own licence (from each package's "
        "metadata; the full texts are in each project's source repository). Sift itself is "
        "MIT-licensed (see LICENSE). libheif and libde265 (LGPL-3.0) are separate shared "
        "libraries in `Sift.app/Contents/Frameworks`; you may replace them with your own builds, "
        "and their source is at https://github.com/strukturag/libheif and "
        "https://github.com/strukturag/libde265.",
        "",
        "## Downloaded on first use (with your consent)",
        "",
        "| Item | Source | Terms |",
        "| --- | --- | --- |",
        "| Android SDK Platform-Tools (adb) | dl.google.com "
        "| Android Software Development Kit License |",
        "| CLIP ViT-B/32 ONNX model and tokenizer | pinned URLs in the app | MIT (OpenAI CLIP) |",
        "",
        "## Runtime and native libraries",
        "",
        "| Library | Version | Licence |",
        "| --- | --- | --- |",
        *[f"| {n} | {v} | {lic} |" for n, v, lic in NATIVE],
        "",
        "## Python packages",
        "",
        "| Package | Version | Licence |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {n} | {v} | {lic} |" for n, v, lic in python_packages()]
    lines += ["", "## Web packages", "", "| Package | Version | Licence |", "| --- | --- | --- |"]
    lines += [f"| {n} | {v} | {lic} |" for n, v, lic in web_packages()]
    (ROOT / "THIRD_PARTY.md").write_text("\n".join(lines) + "\n")
    print("THIRD_PARTY.md written")


if __name__ == "__main__":
    main()
