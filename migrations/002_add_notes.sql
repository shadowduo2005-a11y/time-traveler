-- Migration 002: Add free-text clinical notes to patient records
-- Safe to re-run (IF NOT EXISTS guard on column add)

BEGIN;

ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS notes TEXT;

COMMIT;
