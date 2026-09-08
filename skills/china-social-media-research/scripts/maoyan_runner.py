#!/usr/bin/env python3
"""Run the external Maoyan tag-pool collector without persisting its salt."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def load_backend(root: Path) -> Any:
    script = root / "tag_pool_runner.py"
    if not script.is_file():
        raise ValueError(f"Maoyan tag-pool runner was not found: {script}")
    spec = importlib.util.spec_from_file_location("maoyan_tag_pool_backend", script)
    if spec is None or spec.loader is None:
        raise ValueError(f"could not load Maoyan tag-pool runner: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_backend(root: Path, backend_args: list[str]) -> int:
    root = root.expanduser().resolve()
    sys.path.insert(0, str(root))
    try:
        module = load_backend(root)
        base_collector = module.TagPoolCollector

        class SaltSafeTagPoolCollector(base_collector):
            def _save_checkpoint(self) -> None:
                self.state["updated_at"] = module.utc_now()
                payload = dict(self.state)
                payload.pop("pseudonym_salt", None)
                temporary = self.checkpoint_path.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(self.checkpoint_path)

        module.TagPoolCollector = SaltSafeTagPoolCollector
        return int(module.main(backend_args) or 0)
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, backend_args = parser.parse_known_args(argv)
    if backend_args and backend_args[0] == "--":
        backend_args = backend_args[1:]
    if not backend_args:
        parser.error("backend arguments are required after --")
    try:
        return run_backend(Path(args.backend_root), backend_args)
    except (AttributeError, ImportError, OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
