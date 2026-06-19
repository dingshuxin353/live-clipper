from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-clipper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Run pipeline up to cheap-model candidate generation.")
    scan.add_argument("video_path", type=Path)

    brief = subparsers.add_parser("brief", help="Build a compact Codex review package.")
    brief.add_argument("run_dir", type=Path)

    render = subparsers.add_parser("render", help="Render clips from selected_clips.json.")
    render.add_argument("selection_path", type=Path)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    parser.error(f"{args.command!r} is not implemented yet")

