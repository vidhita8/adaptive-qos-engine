from __future__ import annotations

import numpy as np

from experiments.schema import Scenario


def build_run_plan(scenario: Scenario) -> list[dict]:
    """Build a reproducible paired, blocked experiment schedule."""
    rng = np.random.default_rng(scenario.seed)
    plan = []

    enabled = [
        item.name
        for item in scenario.configurations
        if item.enabled
    ]

    if set(enabled) != {"no_qos", "static_qos"}:
        raise ValueError(
            "Experiment requires enabled no_qos and static_qos configurations"
        )

    for repetition in range(1, scenario.repetitions + 1):
        block_seed = scenario.seed + repetition - 1
        block_order = list(enabled)
        rng.shuffle(block_order)

        for configuration in block_order:
            plan.append({
                "repetition": repetition,
                "block": repetition,
                "configuration": configuration,
                "seed": block_seed,
                "run_id": (
                    f"{configuration}-"
                    f"rep-{repetition:02d}-attempt-1"
                ),
                "attempt": 1,
            })

    return plan


def read_run_status(run_dir):
    import json

    path = run_dir / "status.json"
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_aggregate_and_report(experiment_dir, scenario):
    """Regenerate aggregate data, plots, and Markdown report."""
    import json

    from experiments.aggregate import aggregate_experiment
    from experiments.report import generate_all

    result = aggregate_experiment(experiment_dir, scenario)

    output = experiment_dir / "aggregate.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    temporary.replace(output)

    generate_all(experiment_dir)

    print(f"[AGGREGATE] Saved: {output}")
    print(f"[REPORT] Generated plots and report in {experiment_dir}")

    return result


def execute_campaign(
    scenario,
    scenario_path,
    experiment_id,
    results_root,
    executor,
):
    """Execute a blocked campaign with one retry and resume support."""
    from pathlib import Path

    experiment_dir = Path(results_root) / experiment_id
    experiment_dir.mkdir(parents=True, exist_ok=True)

    plan = build_run_plan(scenario)
    outcomes = []

    for item in plan:
        configuration = item["configuration"]
        repetition = item["repetition"]
        final_status = None
        attempts_used = 0

        for attempt in (1, 2):
            attempts_used = attempt

            run_id = (
                f"{configuration}-rep-{repetition:02d}"
                f"-attempt-{attempt}"
            )
            run_dir = experiment_dir / run_id
            existing = read_run_status(run_dir)

            # A completed valid run never needs to be repeated.
            if (
                existing
                and existing.get("state") == "completed"
                and existing.get("valid") is True
            ):
                final_status = existing
                print(f"[RESUME] Skipping valid run: {run_id}")
                break

            # Never overwrite an existing attempt directory.
            # An incomplete or invalid first attempt proceeds to attempt 2.
            if run_dir.exists():
                print(f"[RETRY] Existing attempt is incomplete or invalid: {run_id}")
                final_status = existing
                continue

            try:
                executor(
                    scenario=scenario,
                    scenario_path=scenario_path,
                    experiment_id=experiment_id,
                    configuration=configuration,
                    repetition=repetition,
                    seed=item["seed"],
                    run_id=run_id,
                )
            except Exception as exc:
                print(f"[ATTEMPT ERROR] {run_id}: {exc}")

            final_status = read_run_status(run_dir)

            if (
                final_status
                and final_status.get("state") == "completed"
                and final_status.get("valid") is True
            ):
                break

            print(f"[RETRY] Attempt {attempt} was not valid: {run_id}")

        successful = bool(
            final_status
            and final_status.get("state") == "completed"
            and final_status.get("valid") is True
        )

        outcomes.append({
            **item,
            "attempts": attempts_used,
            "successful": successful,
        })

    aggregate = write_aggregate_and_report(experiment_dir, scenario)

    successful_runs = sum(
        outcome["successful"] for outcome in outcomes
    )
    unsuccessful_runs = len(outcomes) - successful_runs

    return {
        "experiment_id": experiment_id,
        "planned_runs": len(plan),
        "successful_runs": successful_runs,
        "unsuccessful_runs": unsuccessful_runs,
        "discovered_runs": aggregate["discovered_runs"],
        "outcomes": outcomes,
        "aggregate_path": str(experiment_dir / "aggregate.json"),
        "report_path": str(experiment_dir / "report.md"),
    }