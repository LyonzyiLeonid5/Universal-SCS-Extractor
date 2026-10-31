#!/usr/bin/env python3
"""Build Universal SCS Extractor for the current operating system.

Examples:
  python build.py --format exe
  python build.py --format deb --version 1.0.0
  python build.py --format app
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT_BUILD = Path(__file__).resolve().parent.parent / "build.py"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build Universal SCS Extractor for the current operating system.")
    parser.add_argument("--format", choices=["auto", "all", "exe", "deb", "rpm", "app"],
                        default="auto")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--community-webhook", default="")
    parser.add_argument("--community-message-id", default="")
    args = parser.parse_args()
    command = [sys.executable, str(ROOT_BUILD), "extractor", "--format", args.format,
               "--version", args.version, "--python", args.python]
    if args.skip_install:
        command.append("--skip-install")
    if args.community_webhook:
        command.extend(("--community-webhook", args.community_webhook))
    if args.community_message_id:
        command.extend(("--community-message-id", args.community_message_id))
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
