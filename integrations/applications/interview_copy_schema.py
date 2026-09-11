"""Operator import receipts survive destination rollback; no automatic imports."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_INTERVIEW_IMPORTS (
 app_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 request_json TEXT NOT NULL, conversation_id INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('copied','rolled_back')),
 result_json TEXT NOT NULL, destination_hash TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 PRIMARY KEY(app_id,operation_id)
);
"""
