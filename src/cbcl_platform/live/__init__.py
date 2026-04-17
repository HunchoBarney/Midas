from __future__ import annotations

__all__ = ["RealFeedPaperRuntime"]


def __getattr__(name: str):
    if name == "RealFeedPaperRuntime":
        from cbcl_platform.live.paper_runtime import RealFeedPaperRuntime

        return RealFeedPaperRuntime
    raise AttributeError(name)
