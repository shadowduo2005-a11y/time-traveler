-- Migration 004: Add appointments table linking patients and doctors
-- Branch: feature/add-appointments
-- Risk: New table with FKs to patients and doctors. Use Time-Traveler to preview before merging.

BEGIN;

CREATE TABLE IF NOT EXISTS appointments (
    id               SERIAL PRIMARY KEY,
    patient_id       INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    doctor_id        INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    appointment_date TIMESTAMP NOT NULL,
    reason           VARCHAR(200),
    status           VARCHAR(20) DEFAULT 'scheduled',
    created_at       TIMESTAMP DEFAULT NOW()
);

COMMIT;
