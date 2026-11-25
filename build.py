from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent
target = root / "dist" / "movie2motion-0.4.0.zip"
target.parent.mkdir(exist_ok=True)
with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
    for source in sorted((root / "movie2motion").glob("*.py")):
        archive.write(source, source.relative_to(root))
print(target)
