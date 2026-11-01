-- Migration 001: Add phone_number column to patients table
-- Branch: feature/add-phone-number
-- MR: !2
--
-- Risk: Schema change on production patients table.
-- Use Time-Traveler to preview this migration in a Shadow Clone before merging.

ALTER TABLE patients
  ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);
