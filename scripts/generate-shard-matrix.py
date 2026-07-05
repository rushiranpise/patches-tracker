#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--max-apps-per-shard", type=int, default=40)
    args = parser.parse_args()
    if args.max_apps_per_shard < 1:
        parser.error("--max-apps-per-shard must be at least 1")

    app_count = len(load_config(args.config).apps)
    shard_total = max(1, math.ceil(app_count / args.max_apps_per_shard))
    matrix = {
        "include": [
            {
                "shard_index": shard_index,
                "shard_total": shard_total,
            }
            for shard_index in range(shard_total)
        ]
    }
    print(json.dumps(matrix, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
