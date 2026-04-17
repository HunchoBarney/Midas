from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from cbcl_platform.models import PendingPaperOrder
from cbcl_platform.paper import ExecutionTiming, RealisticPaperExecutionAdapter


@dataclass(order=True)
class _PendingItem:
    submit_ts_ns: int
    seq: int
    pending: PendingPaperOrder = field(compare=False)
    timing: ExecutionTiming = field(compare=False)


class PolymarketPaperExecClient:
    def __init__(self, adapter: RealisticPaperExecutionAdapter) -> None:
        self.adapter = adapter
        self._pending: list[_PendingItem] = []
        self._seq = 0

    def schedule(self, intent) -> PendingPaperOrder:
        timing = self.adapter.sample_timing(intent.decision_ts_ns)
        metadata = {
            "internal_ms": timing.internal_ms,
            "signing_ms": timing.signing_ms,
            "submit_rtt_ms": timing.submit_rtt_ms,
            "slow_tail_ms": timing.slow_tail_ms,
            "ack_delay_ms": timing.ack_delay_ms,
        }
        pending = PendingPaperOrder(
            intent=intent,
            submit_ts_ns=timing.submit_ts_ns,
            ack_ts_ns=timing.ack_ts_ns,
            confirm_delay_ms=timing.confirm_delay_ms,
            timing_metadata=metadata,
        )
        self._seq += 1
        heapq.heappush(
            self._pending,
            _PendingItem(
                submit_ts_ns=pending.submit_ts_ns,
                seq=self._seq,
                pending=pending,
                timing=timing,
            ),
        )
        return pending

    def process_due(self, *, now_ns: int, book_timeline, matching_engine_blocked: bool = False):
        lifecycles = []
        while self._pending and self._pending[0].submit_ts_ns <= now_ns:
            item = heapq.heappop(self._pending)
            lifecycles.append(
                self.adapter.execute_intent(
                    item.pending.intent,
                    book_timeline=book_timeline,
                    matching_engine_blocked=matching_engine_blocked,
                    timing=item.timing,
                ),
            )
        return lifecycles

    def has_pending_for_market(self, market_id: str) -> bool:
        return any(item.pending.intent.market_id == market_id for item in self._pending)
