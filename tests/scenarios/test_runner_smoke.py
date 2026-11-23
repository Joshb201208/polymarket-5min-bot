"""Smoke tests for the scenario runner against today's scheduler."""

from datetime import date, datetime, time

import pytest

from tests.fakes import ET
from tests.fakes.bot import position
from tests.scenarios import ScenarioRunner


def et(*a):
    return datetime(*a, tzinfo=ET)


def test_runner_steps_a_quiet_day_minute_by_minute(world):
    world.price("AAA", 100.0)
    runner = ScenarioRunner(world)
    runner.run_day(date(2026, 11, 23), start=time(9, 0), end=time(10, 0))
    assert runner.steps == 60
    assert world.clock.now_et() == et(2026, 11, 23, 10, 0)
    assert runner.builds == 1


def test_runner_fires_events_and_restarts_with_same_data_dir(world):
    seen = []
    world.price("AAA", 100.0)
    world.alpaca.seed_position("AAA", 10, 100.0)
    runner = ScenarioRunner(world)

    def seed_ledger(w, r):
        r.agent.portfolio.add_position(position("AAA", 10, 100.0))

    runner.at(et(2026, 11, 23, 10, 5), seed_ledger, "seed ledger")
    runner.at(et(2026, 11, 23, 10, 10), lambda w, r: seen.append(w.clock.now_et()), "probe")
    runner.at(et(2026, 11, 23, 10, 20), runner.restart, "restart")
    runner.run_until(et(2026, 11, 23, 10, 30))
    assert seen == [et(2026, 11, 23, 10, 10)]
    assert runner.builds == 2
    assert runner.agent.portfolio.get_position("AAA") is not None  # reloaded from DATA_DIR
    kinds = [e.kind for e in runner.timeline]
    assert kinds.count("restart") == 1


def test_runner_records_tick_errors_and_continues(world):
    calls = []

    async def step(agent, now):
        calls.append(now)
        if len(calls) == 2:
            raise RuntimeError("boom")

    runner = ScenarioRunner(world, build=lambda w: object(), startup=None, shutdown=None, step=step)
    runner.run_until(world.clock.now_et().replace(second=0, microsecond=0) + (et(2026, 1, 1, 0, 5) - et(2026, 1, 1)))
    assert len(calls) >= 4
    assert len(runner.errors) == 1 and "boom" in repr(runner.errors[0].error)


def test_runner_holiday_day_sends_no_orders_without_signals(world):
    world.price("AAA", 100.0)
    runner = ScenarioRunner(world)
    runner.run_day(date(2026, 11, 26), start=time(9, 25), end=time(10, 30))
    world.assert_no_orders()


@pytest.mark.world_start(et(2026, 11, 23, 7, 0))
def test_e2e_week_script_runs_against_todays_scheduler(world):
    """The scripted week runs end to end; behaviour is asserted by the lead later."""
    from tests.scenarios.week import E2EWeek

    assert world.clock.now_et() == E2EWeek().times["first_run"]
    runner = ScenarioRunner(world)
    week = E2EWeek()
    week.run(runner)
    assert world.clock.now_et().date() == date(2026, 11, 27)
    labels = [e.detail for e in runner.timeline if e.kind == "event"]
    assert any("BBB -12%" in s for s in labels) and any("CCC T-2" in s for s in labels)
    assert world.alpaca.position("AAA") is not None  # today's bot never sells AAA on news
    assert not runner.errors, runner.summary()


def test_restart_after_lost_order_rebuilds_agent(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50.0)
    runner = ScenarioRunner(world)
    runner.restart_after_lost_order()

    def place(w, r):  # the order reaches the broker; the reply is lost
        return r.agent.executor.place_buy("AAA", 5, 47.0)

    runner.at(et(2026, 11, 23, 10, 2), place, "buy AAA")
    runner.run_until(et(2026, 11, 23, 10, 10))
    assert runner.builds == 2
    assert world.alpaca.position("AAA").qty == 5  # broker filled although the client saw a timeout
