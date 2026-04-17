import json

from cbcl_platform.models import RuntimeMode
from cbcl_platform.replay import replay_summary
from cbcl_platform.runtime import build_runtime


def test_replay_summary_reads_recorded_events(tmp_path, monkeypatch) -> None:
    replay_path = tmp_path / "replay.jsonl"
    replay_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "coinbase_price", "recorded_at": "2026-04-15T00:00:00+00:00"}),
                json.dumps({"type": "order_lifecycle", "recorded_at": "2026-04-15T00:00:01+00:00"}),
                json.dumps({"type": "order_lifecycle", "recorded_at": "2026-04-15T00:00:02+00:00"}),
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("CBCL_RECORDER_PATH", str(replay_path))

    payload = replay_summary(build_runtime(RuntimeMode.REPLAY))

    assert payload["status"] == "ready"
    assert payload["event_count"] == 3
    assert payload["event_types"]["order_lifecycle"] == 2
    assert payload["latest_events"][-1]["recorded_at"] == "2026-04-15T00:00:02+00:00"

