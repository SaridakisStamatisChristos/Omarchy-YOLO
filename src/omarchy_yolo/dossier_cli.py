from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .model import JobState
from .provenance import verify_dossier
from .util import YoloError, ensure_private_dir, open_private_binary, validate_job_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yolo-dossier",
        description="Verify or export a completed Omarchy YOLO execution dossier",
    )
    parser.add_argument("job_id")
    parser.add_argument("--output", help="write canonical dossier JSON to a private file")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify the stored digest and print only its SHA-256",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    job_id = validate_job_id(args.job_id)
    config = load_config()
    db = Database(config.db_path)
    try:
        job = db.get_job(job_id)
        if job.state != JobState.COMPLETED or job.stop_requested:
            raise YoloError("execution dossier is not available until durable job completion")
        record = db.get_dossier(job_id)
    finally:
        db.close()
    if record is None:
        raise YoloError("completed job has no published execution dossier")

    content = str(record["content"])
    digest = str(record["sha256"])
    if not verify_dossier(content, digest):
        raise YoloError("stored execution dossier failed SHA-256 verification")

    if args.verify_only:
        print(digest)
        return 0
    if args.output:
        output = Path(args.output).expanduser()
        ensure_private_dir(output.parent)
        if output.is_symlink():
            raise YoloError(f"refusing symlink dossier output: {output}")
        with open_private_binary(output) as fh:
            fh.write((content + "\n").encode("utf-8"))
        print(f"{digest}  {output}")
        return 0
    print(content)
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = run(args)
    except (YoloError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
