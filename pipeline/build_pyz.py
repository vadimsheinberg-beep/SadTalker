"""Package the pipeline as a single self-contained executable file.

    python3 pipeline/build_pyz.py -o tzoar-pipeline.pyz

Produces a zipapp: one file, run directly with the system Python.

    ./tzoar-pipeline.pyz registry --doctor --sample 10

This exists because the pipeline needs nothing but the standard library, so
there is no reason to put a 72 MB repository (140 MB with history) on the
server to run a 400 KB package. One file is also far easier to reason about in
a crontab than a checkout that somebody has to remember to `git pull`.

Excluded from the archive: tests, docs, and this builder. What ships is only
what runs.

The avatar stage is the one thing a zipapp cannot serve -- it shells out to
SadTalker's ``inference.py`` and needs the model checkpoints and a GPU, so use
a full clone for that. Everything else, including the daily digest and the
whole script pipeline, runs from the single file.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipapp
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

EXCLUDE_DIRS = {"tests", "docs", "__pycache__"}
EXCLUDE_FILES = {"build_pyz.py"}


def stage(destination: Path) -> Path:
    """Copy the runnable part of the package into a staging directory."""
    staged = destination / "pipeline"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(
        PACKAGE_ROOT,
        staged,
        ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, *EXCLUDE_FILES, "*.pyc"),
    )

    # zipapp runs __main__.py at the archive root, not inside the package.
    (destination / "__main__.py").write_text(
        "import sys\n"
        "from pipeline.cli import main\n"
        "\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    return staged


def build(output: Path, interpreter: str = "/usr/bin/env python3") -> Path:
    import tempfile

    output = Path(output).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp)
        stage(source)
        zipapp.create_archive(
            source, target=output, interpreter=interpreter, compressed=True
        )
    output.chmod(0o755)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o", "--output", default="tzoar-pipeline.pyz", help="path to write"
    )
    parser.add_argument(
        "--interpreter",
        default="/usr/bin/env python3",
        help="shebang interpreter baked into the archive",
    )
    args = parser.parse_args(argv)

    path = build(Path(args.output), args.interpreter)
    size_kb = path.stat().st_size / 1024
    print(f"{path}  ({size_kb:.0f} KB)")
    print(f"  built with Python {sys.version.split()[0]}; needs 3.9+ to run")
    print(f"  smoke test:  {path} --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
