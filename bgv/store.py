"""SQLite persistence for the verifier (separate from the API cache in crustdata_client).

Tables
  employer_sightings  every employer seen on every resume -> "same low-footprint employer on 2+ past resumes"
  runs                one row per verification run: status, decision, report, human-review state
  steps               checkpointed output of each step, so a rerun continues where it stopped
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

from .utils import now_iso

DB_PATH_ENV = "BGV_DB_PATH"


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv(DB_PATH_ENV, "bgv_runs.sqlite")
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS employer_sightings (
                employer_key TEXT, candidate_key TEXT, employer_name TEXT, run_id TEXT,
                low_footprint INTEGER, seen_at TEXT,
                PRIMARY KEY (employer_key, candidate_key));
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, candidate TEXT, candidate_key TEXT, source_name TEXT,
                status TEXT, decision TEXT, review_status TEXT, reviewer_notes TEXT,
                report_json TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT, step_key TEXT, ok INTEGER, output_json TEXT, updated_at TEXT,
                PRIMARY KEY (run_id, step_key));
            """
        )
        self.db.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur

    # ---------- employer sightings ----------

    def record_employer(self, employer_key: str, candidate_key: str, employer_name: str, run_id: str,
                        low_footprint: bool) -> None:
        self._exec(
            "INSERT INTO employer_sightings VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(employer_key, candidate_key) DO UPDATE SET low_footprint=excluded.low_footprint",
            (employer_key, candidate_key, employer_name, run_id, int(low_footprint), now_iso()),
        )

    def other_candidates_with_employer(self, employer_key: str, candidate_key: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT candidate_key, run_id, seen_at FROM employer_sightings "
            "WHERE employer_key = ? AND candidate_key != ? ORDER BY seen_at",
            (employer_key, candidate_key),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- runs ----------

    def start_run(self, run_id: str, source_name: str = "") -> None:
        now = now_iso()
        self._exec(
            "INSERT INTO runs (run_id, source_name, status, created_at, updated_at) VALUES (?, ?, 'running', ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET status='running', updated_at=excluded.updated_at",
            (run_id, source_name, now, now),
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE runs SET {cols}, updated_at = ? WHERE run_id = ?",
                   (*fields.values(), now_iso(), run_id))

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, review_status: str | None = None) -> list[dict]:
        if review_status:
            rows = self.db.execute("SELECT * FROM runs WHERE review_status = ? ORDER BY updated_at DESC",
                                   (review_status,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM runs ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------- step checkpoints ----------

    def save_step(self, run_id: str, step_key: str, output: Any, ok: bool = True) -> None:
        self._exec("INSERT OR REPLACE INTO steps VALUES (?, ?, ?, ?, ?)",
                   (run_id, step_key, int(ok), json.dumps(output), now_iso()))

    def load_step(self, run_id: str, step_key: str) -> Any | None:
        """Only returns steps that completed cleanly; failed steps are redone on rerun."""
        row = self.db.execute("SELECT output_json FROM steps WHERE run_id = ? AND step_key = ? AND ok = 1",
                              (run_id, step_key)).fetchone()
        return json.loads(row[0]) if row else None

    def list_steps(self, run_id: str) -> list[dict]:
        rows = self.db.execute("SELECT step_key, ok, updated_at FROM steps WHERE run_id = ? ORDER BY rowid",
                               (run_id,)).fetchall()
        return [dict(r) for r in rows]
