#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from experiments.schema import Scenario


def main():
    parser = argparse.ArgumentParser(
        description="Validate an experiment scenario YAML file"
    )
    parser.add_argument("scenario", help="Path to scenario YAML")
    args = parser.parse_args()

    path = Path(args.scenario)

    if not path.is_file():
        print(f"[INVALID] File not found: {path}", file=sys.stderr)
        return 1

    try:
        data = yaml.safe_load(path.read_text())
        scenario = Scenario.model_validate(data)
    except yaml.YAMLError as exc:
        print(f"[INVALID] YAML syntax error:\n{exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"[INVALID] Scenario validation failed:\n{exc}", file=sys.stderr)
        return 1
    except (TypeError, ValueError) as exc:
        print(f"[INVALID] {exc}", file=sys.stderr)
        return 1

    enabled = [
        config.name
        for config in scenario.configurations
        if config.enabled
    ]

    print("[VALID] Scenario passed schema validation")
    print(f"Name: {scenario.name}")
    print(f"Duration: {scenario.duration_s}s")
    print(f"Warm-up: {scenario.warmup_s}s")
    print(f"Repetitions: {scenario.repetitions}")
    print(f"Enabled configurations: {', '.join(enabled)}")
    print(f"Traffic flows: {len(scenario.traffic)}")
    print(f"Analysis phases: {len(scenario.phases)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
