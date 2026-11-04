#!/usr/bin/env python3
"""Plugin entry point; implementation lives in china_media_crawler."""

import sys

from _bootstrap import bootstrap

bootstrap()

from china_media_crawler.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["login", *sys.argv[1:]]))
