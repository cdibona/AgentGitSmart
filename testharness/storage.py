"""SQLite-backed storage for benchmark runs.

Schema
------
  runs
    run_id      TEXT  PRIMARY KEY
    created_at  TEXT  ISO-8601
    status      TEXT  running | complete | error
    repo_name   TEXT
    branch      TEXT
    config_json TEXT  full RunConfig as JSON
    results_json TEXT | NULL   list[ApproachResult] as JSON
    use_docker  INTEGER NOT NULL DEFAULT 1
    latency_ms  INTEGER NOT NULL DEFAULT 0

Smart queries:
  - list_runs() returns newest-first summary rows (no heavy JSON)
  - get_run() returns full detail including parsed results
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ApproachResult, RunConfig, RunDetail, RunSummary

log = logging.getLogger(__name__)


class ResultStorage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id       TEXT PRIMARY KEY,
                    created_at   TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'running',
                    repo_name    TEXT NOT NULL,
                    branch       TEXT NOT NULL,
                    target_paths TEXT NOT NULL,   -- JSON array
                    approaches   TEXT NOT NULL,   -- JSON array
                    num_runs     INTEGER NOT NULL DEFAULT 3,
                    results_json TEXT,
                    use_docker   INTEGER NOT NULL DEFAULT 1,
                    latency_ms   INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_runs_created
                ON runs (created_at DESC)
            """)
        self._migrate()

    def _migrate(self) -> None:
        """Add new columns to existing databases that pre-date them."""
        migrations = [
            ("use_docker", "ALTER TABLE runs ADD COLUMN use_docker INTEGER NOT NULL DEFAULT 1"),
            ("latency_ms", "ALTER TABLE runs ADD COLUMN latency_ms INTEGER NOT NULL DEFAULT 0"),
            ("use_real_agent", "ALTER TABLE runs ADD COLUMN use_real_agent INTEGER NOT NULL DEFAULT 0"),
            ("agent_pct", "ALTER TABLE runs ADD COLUMN agent_pct REAL NOT NULL DEFAULT 1.0"),
            ("agent_seed", "ALTER TABLE runs ADD COLUMN agent_seed INTEGER NOT NULL DEFAULT 42"),
            ("completed_at", "ALTER TABLE runs ADD COLUMN completed_at TEXT"),
        ]
        with self._conn() as conn:
            # Get existing column names
            cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            for col_name, ddl in migrations:
                if col_name not in cols:
                    try:
                        conn.execute(ddl)
                        log.info("storage: migrated — added column %s", col_name)
                    except Exception as e:
                        log.warning("storage: migration failed for %s: %s", col_name, e)

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def create_run(self, run_id: str, created_at: str, config: RunConfig) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO runs
                  (run_id, created_at, status, repo_name, branch,
                   target_paths, approaches, num_runs, use_docker, latency_ms,
                   use_real_agent, agent_pct, agent_seed)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    config.repo_name,
                    config.branch,
                    json.dumps(config.target_paths),
                    json.dumps(config.approaches),
                    config.num_runs,
                    int(config.use_docker),
                    config.latency_ms,
                    int(config.use_real_agent),
                    config.agent_pct,
                    config.agent_seed,
                ),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        results: Optional[List[ApproachResult]] = None,
    ) -> None:
        results_json = (
            json.dumps([r.model_dump() for r in results]) if results else None
        )
        completed_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET status=?, results_json=?, completed_at=? WHERE run_id=?",
                (status, results_json, completed_at, run_id),
            )

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def list_runs(self) -> List[RunSummary]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT run_id, created_at, status, repo_name, branch,
                       target_paths, approaches
                FROM runs
                ORDER BY created_at DESC
                LIMIT 100
                """
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                RunSummary(
                    run_id=r["run_id"],
                    created_at=r["created_at"],
                    status=r["status"],
                    repo_name=r["repo_name"],
                    branch=r["branch"],
                    target_paths=json.loads(r["target_paths"]),
                    approaches=json.loads(r["approaches"]),
                )
            )
        return out

    def get_run(self, run_id: str) -> Optional[RunDetail]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        results: List[ApproachResult] = []
        if row["results_json"]:
            for item in json.loads(row["results_json"]):
                try:
                    results.append(ApproachResult(**item))
                except Exception:
                    # Tolerate old-style dicts missing new fields (graceful degradation)
                    results.append(ApproachResult(approach=item.get("approach", "?"),
                                                  elapsed_s=item.get("elapsed_s", 0.0),
                                                  **{k: v for k, v in item.items()
                                                     if k not in ("approach", "elapsed_s")}))

        # use_docker / latency_ms / real-agent fields may be absent from old rows
        try:
            use_docker = bool(row["use_docker"])
        except (IndexError, KeyError):
            use_docker = True
        try:
            latency_ms = int(row["latency_ms"])
        except (IndexError, KeyError):
            latency_ms = 0
        try:
            use_real_agent = bool(row["use_real_agent"])
        except (IndexError, KeyError):
            use_real_agent = False
        try:
            agent_pct = float(row["agent_pct"])
        except (IndexError, KeyError):
            agent_pct = 1.0
        try:
            agent_seed = int(row["agent_seed"])
        except (IndexError, KeyError):
            agent_seed = 42

        return RunDetail(
            run_id=row["run_id"],
            created_at=row["created_at"],
            status=row["status"],
            repo_name=row["repo_name"],
            branch=row["branch"],
            target_paths=json.loads(row["target_paths"]),
            approaches=json.loads(row["approaches"]),
            num_runs=row["num_runs"],
            results=results,
            use_docker=use_docker,
            latency_ms=latency_ms,
            use_real_agent=use_real_agent,
            agent_pct=agent_pct,
            agent_seed=agent_seed,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Summary stats across all runs (for dashboard cards)."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            complete = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status='complete'"
            ).fetchone()[0]
        return {"total_runs": total, "complete_runs": complete}
