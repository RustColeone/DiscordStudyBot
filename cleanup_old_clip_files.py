from __future__ import annotations

import argparse
import time
from pathlib import Path

from providers.clip_query import ensure_clip_output_dir


def delete_old_files(max_age_seconds: int) -> tuple[int, int]:
    output_dir = ensure_clip_output_dir()
    now = time.time()
    deleted = 0
    kept = 0

    for path in output_dir.iterdir():
        if not path.is_file():
            continue

        age_seconds = now - path.stat().st_mtime
        if age_seconds > max_age_seconds:
            path.unlink(missing_ok=True)
            deleted += 1
        else:
            kept += 1

    return deleted, kept


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete generated clip files older than a given age.")
    parser.add_argument("--max-age-minutes", type=int, default=60, help="Delete files older than this many minutes")
    args = parser.parse_args()

    deleted, kept = delete_old_files(max(args.max_age_minutes, 1) * 60)
    output_dir = ensure_clip_output_dir()
    print(f"Clip cleanup complete in {output_dir}")
    print(f"Deleted: {deleted}")
    print(f"Kept: {kept}")


if __name__ == "__main__":
    main()
