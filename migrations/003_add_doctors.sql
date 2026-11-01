-- Migration 003: Add doctors table and assign patients to doctors
-- Branch: feature/add-doctors
-- Risk: New table + FK column on patients. Use Time-Traveler to preview before merging.

BEGIN;

CREATE TABLE IF NOT EXISTS doctors (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(100) NOT NULL,
    specialty    VARCHAR(100) NOT NULL,
    email        VARCHAR(150) UNIQUE NOT NULL,
    phone        VARCHAR(20),
    created_at   TIMESTAMP DEFAULT NOW()
);

ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS doctor_id INT REFERENCES doctors(id) ON DELETE SET NULL;

COMMIT;
