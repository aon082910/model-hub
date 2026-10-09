"""Run Model Hub's mesh loading and health check over a folder of models and report what fails.

For stress-testing on a big, messy collection (for example the Thingi10K set: `pip install thingi10k`, or its raw files from Hugging Face; each model keeps
its own licence, so use it for testing only). Nothing is imported into a library and nothing is written except the report.

    python tools/corpus_check.py /path/to/models [--limit 500] [--json report.json]

Exit status is 0 when every file either loaded or failed with a clear message (no crash), 1 when something crashed the checker itself.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXTENSIONS = {".stl", ".obj", ".3mf", ".fbx"}


def check(path: Path) -> dict:
    from app.mesh_health import health_of_file
    started = time.time()
    row = {"file": str(path), "bytes": path.stat().st_size}
    try:
        result = health_of_file(path)
        row.update(status="checked", ok=result.get("ok"), watertight=result.get("watertight"), faces=result.get("faces"), issues=result.get("issues", []))
    except ValueError as e:                              # a clear refusal: an empty or unreadable file
        row.update(status="unreadable", message=str(e)[:200])
    except Exception as e:                               # anything else is a crash the checker should have survived
        row.update(status="crashed", message=f"{e.__class__.__name__}: {str(e)[:200]}")
    row["seconds"] = round(time.time() - started, 2)
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=0, help="stop after this many files (0 = all)")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args(argv)
    files = sorted(p for p in args.folder.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
    if args.limit:
        files = files[:args.limit]
    rows = [check(p) for p in files]
    counts: dict = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    healthy = sum(1 for r in rows if r.get("ok") is True)
    print(f"{len(rows)} files: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + f"; {healthy} sound enough to print as they are")
    for r in rows:
        if r["status"] != "checked":
            print(f"  {r['status']}: {r['file']}: {r.get('message', '')}")
    if args.json:
        args.json.write_text(json.dumps({"summary": counts, "files": rows}, indent=1), encoding="utf-8")
    return 1 if counts.get("crashed") else 0


if __name__ == "__main__":
    sys.exit(main())
