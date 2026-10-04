"""Create source and extension-only ZIPs without stale binaries or caches."""
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
dist = root / "dist"
dist.mkdir(exist_ok=True)
shutil.copytree(root / "browser-extension", dist / "browser-extension", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc"))
excluded = {"dist", "build", ".git", "__pycache__", ".venv", ".venv-build", ".DS_Store"}
for name, folder in (("BiliDownloader-source.zip", root),
                     ("BiliDownloader-chrome-edge-extension.zip", root / "browser-extension")):
    files = [p for p in folder.rglob("*") if p.is_file()
             and not excluded.intersection(p.relative_to(folder).parts) and p.suffix not in {".pyc", ".pyo"}]
    with ZipFile(dist / name, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, Path(folder.name) / path.relative_to(folder))
    print("Created", dist / name)
