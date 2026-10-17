"""
scenario_lab/run_all.py
-----------------------
CLI runner to execute all baseline scenarios in the Scenario Lab,
compute deterministic evaluation results, and output structured reports.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import List

# Ensure workspace root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scenario_lab.models import Scenario, ScenarioResult
from scenario_lab.runner import ScenarioRunner


async def run_all_scenarios(scenarios_dir: Path) -> List[ScenarioResult]:
    runner = ScenarioRunner()
    scenario_files = sorted(scenarios_dir.glob("*.json"))

    results: List[ScenarioResult] = []
    print("\n" + "=" * 80)
    print(" CORTEX SCENARIO LAB — EXECUTING DISPOSABLE BASELINE VALIDATION HARNESS")
    print("=" * 80)

    for sf in scenario_files:
        scenario = Scenario.from_file(sf)
        print(f"\n[RUNNING] {scenario.name} ({sf.name})...")
        res = await runner.run_scenario(scenario)
        results.append(res)
        _print_scenario_result(res)

    _print_aggregate_summary(results)
    return results


def _print_scenario_result(res: ScenarioResult) -> None:
    print("-" * 60)
    print(f"SCENARIO NAME:      {res.scenario_name}")
    print(f"RESULT:             {res.result}")
    print(f"GOAL STATE:         {res.goal_state}")
    print(f"STEPS PLANNED:      {res.steps_planned}")
    print(f"STEPS EXECUTED:     {res.steps_executed}")
    print(f"APPROVALS:          {res.approvals}")
    print(f"REPLANS:            {res.replans}")
    print(f"WAITS:              {res.waits}")
    print(f"SAFETY VIOLATIONS:  {len(res.safety_violations)}")
    if res.safety_violations:
        for v in res.safety_violations:
            print(f"  - [SAFETY VIOLATION] {v}")
    print(f"FAILED ASSERTIONS:  {len(res.failed_assertions)}")
    if res.failed_assertions:
        for f in res.failed_assertions:
            print(f"  - [FAILED ASSERTION] {f}")
    print(f"DURATION:           {res.duration:.3f}s")
    print("-" * 60)


def _print_aggregate_summary(results: List[ScenarioResult]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.result == "PASS")
    failed = sum(1 for r in results if r.result == "FAIL")
    blocked = sum(1 for r in results if r.result == "BLOCKED")
    violations = sum(len(r.safety_violations) for r in results)
    # Success rate: scenarios that behaved as expected (pass + expected blocked)
    # Or strict passed / total
    strict_rate = (passed / total * 100) if total > 0 else 0.0

    print("\n" + "=" * 80)
    print(" AGGREGATE SCENARIO LAB RESULTS SUMMARY")
    print("=" * 80)
    print(f"TOTAL SCENARIOS:    {total}")
    print(f"PASSED:             {passed}")
    print(f"FAILED:             {failed}")
    print(f"BLOCKED:            {blocked}")
    print(f"SAFETY VIOLATIONS:  {violations}")
    print(f"SUCCESS RATE:       {strict_rate:.1f}%")
    print("=" * 80 + "\n")


def main():
    base_dir = Path(__file__).parent
    scenarios_dir = base_dir / "scenarios"
    asyncio.run(run_all_scenarios(scenarios_dir))


if __name__ == "__main__":
    main()
