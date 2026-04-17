import asyncio

from cbcl_platform.live.paper_runtime import RealFeedPaperRuntime
from cbcl_platform.models import RuntimeMode
from cbcl_platform.runtime import build_runtime
from cbcl_platform.state_store import RuntimeStateStore


def test_live_runtime_preserves_zero_trade_calibration(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    store = RuntimeStateStore(str(tmp_path / "runtime_state.json"))
    paper_runtime = RealFeedPaperRuntime(runtime=runtime, state_store=store)

    calibration = paper_runtime._calibration()  # noqa: SLF001

    assert calibration.trade_count == 0
    assert calibration.win_rate == 0.0


def test_live_runtime_drops_oldest_event_when_queue_is_full(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    store = RuntimeStateStore(str(tmp_path / "runtime_state.json"))
    paper_runtime = RealFeedPaperRuntime(runtime=runtime, state_store=store)
    paper_runtime._events = asyncio.Queue(maxsize=2)  # noqa: SLF001
    paper_runtime._events.put_nowait({"id": "oldest"})  # noqa: SLF001
    paper_runtime._events.put_nowait({"id": "newer"})  # noqa: SLF001

    paper_runtime._emit({"id": "latest"})  # noqa: SLF001

    assert paper_runtime._events.get_nowait()["id"] == "newer"  # noqa: SLF001
    assert paper_runtime._events.get_nowait()["id"] == "latest"  # noqa: SLF001


def test_live_runtime_surfaces_tracked_market_slots_without_discovery(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    store = RuntimeStateStore(str(tmp_path / "runtime_state.json"))
    paper_runtime = RealFeedPaperRuntime(runtime=runtime, state_store=store)

    rows = paper_runtime._merged_opportunity_rows()  # noqa: SLF001
    slots = {(row["coin"], row["interval"], row["signal_state"]) for row in rows}
    btc_5m = next(row for row in rows if row["coin"] == "BTC" and row["interval"] == "5m")

    assert ("BTC", "5m", "awaiting live market") in slots
    assert ("BTC", "15m", "awaiting live market") in slots
    assert ("ETH", "5m", "awaiting live market") in slots
    assert ("ETH", "15m", "awaiting live market") in slots
    assert btc_5m["minutes_to_close"] == "awaiting market"
    assert btc_5m["best_ask"] == "awaiting book"
