"""Outbound pacing: buckets, low-priority skipping and RetryAfter handling."""

import asyncio

import pytest
from telegram.error import RetryAfter, TimedOut

from sbahelper import pacing
from sbahelper.pacing import LOW, Pacer, Throttled


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Sleeper:
    """Records sleeps and advances the fake clock instead of waiting."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.now += seconds


def make_pacer(**kwargs) -> tuple[Pacer, Clock, Sleeper]:
    clock = Clock()
    sleeper = Sleeper(clock)
    return Pacer(clock=clock, sleep=sleeper, **kwargs), clock, sleeper


def call(pacer: Pacer, callback, *, chat_id=-100, endpoint="sendMessage", low=False):
    return pacer.process_request(
        callback, (), {}, endpoint, {"chat_id": chat_id}, LOW if low else None
    )


def ok_callback(calls: list):
    async def callback():
        calls.append(1)
        return {"ok": True}

    return callback


def test_low_priority_is_skipped_when_the_group_budget_is_spent() -> None:
    pacer, _, _ = make_pacer()
    calls: list = []

    async def scenario() -> None:
        for _ in range(5):  # the group burst
            await call(pacer, ok_callback(calls))
        with pytest.raises(Throttled):
            await call(pacer, ok_callback(calls), low=True)

    asyncio.run(scenario())
    assert len(calls) == 5 and pacer.skipped == 1


def test_normal_priority_waits_for_budget() -> None:
    pacer, clock, _ = make_pacer()
    calls: list = []

    async def scenario() -> None:
        for _ in range(6):
            await call(pacer, ok_callback(calls))

    asyncio.run(scenario())
    assert len(calls) == 6 and clock.now > 1000.0  # the 6th waited for a refill


def test_retry_after_is_honoured_then_retried() -> None:
    pacer, _, sleeper = make_pacer()
    attempts: list = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RetryAfter(7)
        return {"ok": True}

    assert asyncio.run(call(pacer, flaky)) == {"ok": True}
    assert len(attempts) == 2 and any(s >= 7 for s in sleeper.slept) and pacer.flood_waits == 1


def test_retry_after_gives_up_after_the_bound() -> None:
    pacer, _, _ = make_pacer(max_retries=1)

    async def always():
        raise RetryAfter(1)

    with pytest.raises(RetryAfter):
        asyncio.run(call(pacer, always))


def test_low_priority_is_never_retried() -> None:
    pacer, _, _ = make_pacer()
    attempts: list = []

    async def flood():
        attempts.append(1)
        raise RetryAfter(30)

    with pytest.raises(Throttled):
        asyncio.run(call(pacer, flood, low=True))
    assert len(attempts) == 1


def test_timeouts_are_not_retried() -> None:
    """A timed-out upload may have arrived; sending it again could duplicate it."""
    pacer, _, _ = make_pacer()
    attempts: list = []

    async def slow():
        attempts.append(1)
        raise TimedOut

    with pytest.raises(TimedOut):
        asyncio.run(call(pacer, slow))
    assert len(attempts) == 1


def test_flood_cooldown_applies_to_uncounted_calls_too() -> None:
    pacer, clock, _ = make_pacer()

    async def scenario() -> None:
        pacer._chat_bucket(-100).blocked_until = clock.now + 20
        with pytest.raises(Throttled):
            await call(pacer, ok_callback([]), endpoint="sendChatAction", low=True)

    asyncio.run(scenario())


def test_private_chats_and_groups_have_separate_budgets() -> None:
    pacer, _, _ = make_pacer()
    calls: list = []

    async def scenario() -> None:
        for _ in range(5):
            await call(pacer, ok_callback(calls), chat_id=-100)
        await call(pacer, ok_callback(calls), chat_id=42, low=True)  # another chat: fine

    asyncio.run(scenario())
    assert len(calls) == 6


def test_retry_after_seconds_reads_ints_and_timedeltas() -> None:
    assert pacing.retry_after_seconds(RetryAfter(5)) == 5.0
