-- Patient Records System - Production Schema
-- This file is the source of truth for the database structure.
-- The Time-Traveler Architect agent reads this file to detect schema changes.

CREATE TABLE IF NOT EXISTS doctors (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(100) NOT NULL,
    specialty    VARCHAR(100) NOT NULL,
    email        VARCHAR(150) UNIQUE NOT NULL,
    phone        VARCHAR(20),
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS patients (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(100)        NOT NULL,
    date_of_birth DATE                NOT NULL,
    email         VARCHAR(150)        UNIQUE NOT NULL,
    phone_number  VARCHAR(20),
    notes         TEXT,
    doctor_id     INT                 REFERENCES doctors(id) ON DELETE SET NULL,
    created_at    TIMESTAMP           DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS appointments (
    id               SERIAL PRIMARY KEY,
    patient_id       INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    doctor_id        INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    appointment_date TIMESTAMP NOT NULL,
    reason           VARCHAR(200),
    status           VARCHAR(20) DEFAULT 'scheduled',
    created_at       TIMESTAMP DEFAULT NOW()
);

-- No seed data here — shadow clones use agent-generated seed SQL; production uses seeds/demo.sql
