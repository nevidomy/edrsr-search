from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

from court_search.domain.enums import Jurisdiction
from court_search.domain.models import DecisionRecord, Paragraph, SourceRef


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = _connect(path)
        self._lock = threading.RLock()
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS generations (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                ingested_at TEXT,
                selection_name TEXT,
                source_manifest_json TEXT NOT NULL,
                counts_json TEXT NOT NULL,
                notes_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS active_generation (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT REFERENCES generations(name)
            );
            INSERT OR IGNORE INTO active_generation(id, name) VALUES (1, NULL);
            CREATE TABLE IF NOT EXISTS decisions (
                generation TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                document_version TEXT NOT NULL,
                case_number TEXT,
                case_number_raw TEXT,
                court_code TEXT,
                judge_raw TEXT,
                decision_date TEXT,
                decision_date_raw TEXT,
                source_year INTEGER,
                decision_year INTEGER,
                jurisdiction TEXT NOT NULL,
                justice_kind INTEGER,
                justice_kind_name TEXT,
                judgment_code TEXT,
                category_code TEXT,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                paragraphs_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                registry_url TEXT NOT NULL,
                PRIMARY KEY (generation, doc_id)
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_case
                ON decisions(generation, case_number, decision_date, doc_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
                doc_id,
                case_number,
                court_code,
                text,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            CREATE TABLE IF NOT EXISTS fts_generation (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT
            );
            INSERT OR IGNORE INTO fts_generation(id, name) VALUES (1, NULL);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def active_generation(self) -> str | None:
        row = self.conn.execute("SELECT name FROM active_generation WHERE id = 1").fetchone()
        return row["name"] if row else None

    def get_generation(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM generations WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def list_generations(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM generations ORDER BY created_at").fetchall()
        return [row["name"] for row in rows]

    def save_generation(
        self,
        name: str,
        *,
        selection_name: str,
        source_manifest: dict[str, Any],
        counts: dict[str, Any],
        notes: list[str],
        ingested_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO generations(name, created_at, ingested_at, selection_name,
                                    source_manifest_json, counts_json, notes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                ingested_at=excluded.ingested_at,
                source_manifest_json=excluded.source_manifest_json,
                counts_json=excluded.counts_json,
                notes_json=excluded.notes_json
            """,
            (
                name,
                now,
                ingested_at.isoformat() if ingested_at else now,
                selection_name,
                json.dumps(source_manifest, ensure_ascii=False),
                json.dumps(counts, ensure_ascii=False),
                json.dumps(notes, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def activate(self, name: str) -> None:
        if not self.get_generation(name):
            raise ValueError(f"unknown generation: {name}")
        self.conn.execute("UPDATE active_generation SET name = ? WHERE id = 1", (name,))
        self.conn.commit()

    def clear_decisions(self, generation: str) -> None:
        self.conn.execute("DELETE FROM decisions WHERE generation = ?", (generation,))
        self.conn.commit()

    def insert_decision(self, generation: str, record: DecisionRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO decisions(
                generation, doc_id, document_version, case_number, case_number_raw,
                court_code, judge_raw, decision_date, decision_date_raw, source_year,
                decision_year, jurisdiction, justice_kind, justice_kind_name,
                judgment_code, category_code, text, text_hash, paragraphs_json,
                source_refs_json, registry_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation,
                record.doc_id,
                record.document_version,
                record.case_number,
                record.case_number_raw,
                record.court_code,
                record.judge_raw,
                record.decision_date.isoformat() if record.decision_date else None,
                record.decision_date_raw,
                record.source_year,
                record.decision_year,
                record.jurisdiction.value,
                record.justice_kind,
                record.justice_kind_name,
                record.judgment_code,
                record.category_code,
                record.text,
                record.text_hash,
                json.dumps([p.model_dump() for p in record.paragraphs], ensure_ascii=False),
                json.dumps([s.model_dump(mode="json") for s in record.source_refs], ensure_ascii=False),
                record.registry_url,
            ),
        )

    def replace_decisions(self, generation: str, records: Iterable[DecisionRecord]) -> int:
        self.clear_decisions(generation)
        count = 0
        for record in records:
            self.insert_decision(generation, record)
            count += 1
        self.conn.commit()
        return count

    def rebuild_fts(self, generation: str) -> None:
        self.conn.execute("DELETE FROM decisions_fts")
        self.conn.execute(
            """
            INSERT INTO decisions_fts(doc_id, case_number, court_code, text)
            SELECT doc_id, COALESCE(case_number, ''), COALESCE(court_code, ''), text
            FROM decisions WHERE generation = ?
            """,
            (generation,),
        )
        self.conn.execute("UPDATE fts_generation SET name = ? WHERE id = 1", (generation,))
        self.conn.commit()

    def iter_decisions(self, generation: str) -> Iterable[DecisionRecord]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE generation = ? ORDER BY doc_id",
            (generation,),
        )
        for row in rows:
            yield _row_to_record(row)

    def get_decision(self, generation: str, doc_id: str) -> DecisionRecord | None:
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE generation = ? AND doc_id = ?",
            (generation, doc_id),
        ).fetchone()
        return _row_to_record(row) if row else None

    def count_decisions(self, generation: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE generation = ?",
            (generation,),
        ).fetchone()
        return int(row["n"])

    def jurisdiction_counts(self, generation: str) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT jurisdiction, COUNT(*) AS n
            FROM decisions WHERE generation = ?
            GROUP BY jurisdiction
            """,
            (generation,),
        ).fetchall()
        return {row["jurisdiction"]: int(row["n"]) for row in rows}

    def date_bounds(self, generation: str) -> tuple[date | None, date | None]:
        row = self.conn.execute(
            """
            SELECT MIN(decision_date) AS dmin, MAX(decision_date) AS dmax
            FROM decisions WHERE generation = ? AND decision_date IS NOT NULL
            """,
            (generation,),
        ).fetchone()
        return _parse_date(row["dmin"]), _parse_date(row["dmax"])

    def search(
        self,
        generation: str,
        query: str,
        *,
        filters: dict[str, Any],
        limit: int,
    ) -> list[tuple[DecisionRecord, float]]:
        where = ["d.generation = ?"]
        params: list[Any] = [generation]
        _append_filters(where, params, filters, alias="d")
        match = _fts_match(query)
        sql = f"""
            SELECT d.*, bm25(decisions_fts) AS rank_score
            FROM decisions_fts
            JOIN decisions d ON d.doc_id = decisions_fts.doc_id AND d.generation = ?
            WHERE {' AND '.join(where)} AND decisions_fts MATCH ?
            ORDER BY rank_score
            LIMIT ?
        """
        rows = self.conn.execute(sql, [generation, *params, match, limit]).fetchall()
        return [(_row_to_record(row), float(row["rank_score"])) for row in rows]

    def list_by_case(
        self,
        generation: str,
        case_number: str,
        *,
        court_codes: list[str] | None,
        jurisdictions: list[str] | None,
        limit: int,
        after_date: str | None,
        after_id: str | None,
    ) -> list[DecisionRecord]:
        where = ["generation = ?", "case_number = ?"]
        params: list[Any] = [generation, case_number]
        if court_codes:
            where.append(f"court_code IN ({_placeholders(court_codes)})")
            params.extend(court_codes)
        if jurisdictions:
            where.append(f"jurisdiction IN ({_placeholders(jurisdictions)})")
            params.extend(jurisdictions)
        if after_date is not None or after_id:
            where.append(
                """
                (
                    COALESCE(decision_date, '9999-12-31') > COALESCE(?, '')
                    OR (
                        COALESCE(decision_date, '9999-12-31') = COALESCE(?, '9999-12-31')
                        AND doc_id > ?
                    )
                )
                """
            )
            params.extend([after_date or "", after_date or "9999-12-31", after_id or ""])
        sql = f"""
            SELECT * FROM decisions
            WHERE {' AND '.join(where)}
            ORDER BY CASE WHEN decision_date IS NULL THEN 1 ELSE 0 END,
                     decision_date, doc_id
            LIMIT ?
        """
        params.append(limit)
        return [_row_to_record(row) for row in self.conn.execute(sql, params).fetchall()]

    def harvest_codes(self, generation: str) -> dict[str, set[str]]:
        out = {"court": set(), "judgment": set(), "category": set()}
        rows = self.conn.execute(
            "SELECT court_code, judgment_code, category_code FROM decisions WHERE generation = ?",
            (generation,),
        ).fetchall()
        for row in rows:
            if row["court_code"]:
                out["court"].add(str(row["court_code"]))
            if row["judgment_code"]:
                out["judgment"].add(str(row["judgment_code"]))
            if row["category_code"]:
                out["category"].add(str(row["category_code"]))
        return out


def _append_filters(where: list[str], params: list[Any], filters: dict[str, Any], alias: str) -> None:
    if jurisdictions := filters.get("jurisdictions"):
        where.append(f"{alias}.jurisdiction IN ({_placeholders(jurisdictions)})")
        params.extend(jurisdictions)
    if court_codes := filters.get("court_codes"):
        where.append(f"{alias}.court_code IN ({_placeholders(court_codes)})")
        params.extend(court_codes)
    if judgment_codes := filters.get("judgment_codes"):
        where.append(f"{alias}.judgment_code IN ({_placeholders(judgment_codes)})")
        params.extend(judgment_codes)
    if category_codes := filters.get("category_codes"):
        where.append(f"{alias}.category_code IN ({_placeholders(category_codes)})")
        params.extend(category_codes)
    if case_number := filters.get("case_number"):
        where.append(f"{alias}.case_number = ?")
        params.append(case_number)
    if date_from := filters.get("date_from"):
        where.append(f"{alias}.decision_date IS NOT NULL AND {alias}.decision_date >= ?")
        params.append(date_from)
    if date_to := filters.get("date_to"):
        where.append(f"{alias}.decision_date IS NOT NULL AND {alias}.decision_date <= ?")
        params.append(date_to)
    if phrases := filters.get("required_phrases"):
        for phrase in phrases:
            where.append(f"{alias}.text LIKE ?")
            params.append(f"%{phrase}%")


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def _fts_match(query: str) -> str:
    tokens = [token for token in query.replace('"', " ").split() if token]
    if not tokens:
        return '""'
    return " AND ".join(f'"{token}"' for token in tokens[:32])


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _row_to_record(row: sqlite3.Row) -> DecisionRecord:
    paragraphs = [Paragraph.model_validate(item) for item in json.loads(row["paragraphs_json"])]
    source_refs = [SourceRef.model_validate(item) for item in json.loads(row["source_refs_json"])]
    return DecisionRecord(
        doc_id=str(row["doc_id"]),
        document_version=row["document_version"],
        case_number_raw=row["case_number_raw"],
        case_number=row["case_number"],
        court_code=row["court_code"],
        judge_raw=row["judge_raw"],
        decision_date_raw=row["decision_date_raw"],
        decision_date=_parse_date(row["decision_date"]),
        source_year=row["source_year"],
        decision_year=row["decision_year"],
        jurisdiction=Jurisdiction(row["jurisdiction"]),
        justice_kind=row["justice_kind"],
        justice_kind_name=row["justice_kind_name"],
        judgment_code=row["judgment_code"],
        category_code=row["category_code"],
        text=row["text"],
        text_hash=row["text_hash"],
        paragraphs=paragraphs,
        source_refs=source_refs,
        registry_url=row["registry_url"],
    )
