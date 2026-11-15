"""Evaluation (Phase 6): metrics computed from recorded runs, and readable run reports.

Nothing here runs the agent or estimates anything: every number comes from
the audit database and the LangGraph checkpoints of runs that actually
happened. A value that cannot be computed is reported as missing (None / n/a).
"""

from .data import RunRecord, load_runs
from .metrics import run_metrics, summarize

__all__ = ["RunRecord", "load_runs", "run_metrics", "summarize"]
