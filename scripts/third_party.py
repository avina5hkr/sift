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
        "Sift.app bundles the Python and web packages below, each under its own licence. The "
        "licence texts are in each package's metadata inside the app bundle.",
        "",
        "## Downloaded on first use (with your consent)",
        "",
        "| Item | Source | Terms |",
        "| --- | --- | --- |",
        "| Android SDK Platform-Tools (adb) | dl.google.com "
        "| Android Software Development Kit License |",
        "| CLIP ViT-B/32 ONNX model and tokenizer | pinned URLs in the app | MIT (OpenAI CLIP) |",
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
