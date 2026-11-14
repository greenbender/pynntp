"""pynntp against the nntp-testkit fake server, with injected faults.

Scenarios marked as known fail on the current code; each tracks an open bug.
Such a scenario fails with the reason named by its known_why, and any other
failure of it fails the run. Fixing the bug makes the scenario pass, which is
reported as XPASS and fails the run, so remove its known reason in the same
change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.faults.scenarios import Scenario, run, scenarios

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            sc,
            id=sc.name,
            marks=[
                pytest.mark.xfail(
                    reason=sc.known, strict=sc.known_strict, raises=AssertionError
                )
            ]
            if sc.known
            else [],
        )
        for sc in scenarios()
    ],
)
def test_fault(scenario: Scenario, fault_server: Path) -> None:
    res = run(scenario, fault_server)
    reason = res["reason"]
    details = "\n".join(
        [
            f"result: {res.get('result')} (state {res.get('state')})",
            *res["wire"],
            f"client lines: {res['client_lines']}",
        ]
    )
    # pytest.fail is not an AssertionError, so the xfail does not apply to it
    if scenario.known and "server side:" in reason:
        pytest.fail(f"server-side problem in a known scenario: {reason}\n{details}")
    if scenario.known and reason and scenario.known_why not in reason:
        pytest.fail(
            f"fails, but not for the known bug ({scenario.known_why!r}): "
            f"{reason}\n{details}"
        )
    assert not reason, f"{reason}\n{details}"
