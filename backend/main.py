import os
import time
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from psycopg2.pool import ThreadedConnectionPool
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from datetime import date, datetime

DB_CONFIG = {
    "host":     os.getenv("POSTGRES_HOST", "database"),
    "dbname":   os.getenv("POSTGRES_DB", "patient_records"),
    "user":     os.getenv("POSTGRES_USER", "admin"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}

_pool: ThreadedConnectionPool = None


def wait_for_db(retries: int = 10, delay: int = 2):
    for attempt in range(retries):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            conn.close()
            return
        except psycopg2.OperationalError:
            print(f"DB not ready, retrying ({attempt + 1}/{retries})...")
            time.sleep(delay)
    raise RuntimeError("Could not connect to database after multiple retries.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    wait_for_db()
    _pool = ThreadedConnectionPool(1, 10, **DB_CONFIG)
    yield
    _pool.closeall()


app = FastAPI(title="Patient Records API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def get_conn():
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


class DoctorIn(BaseModel):
    name:      str
    specialty: str
    email:     EmailStr
    phone:     str | None = None


class PatientIn(BaseModel):
    name:          str
    date_of_birth: date
    email:         EmailStr
    phone_number:  str | None = None
    notes:         str | None = None
    doctor_id:     int | None = None


@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Doctors ───────────────────────────────────────────────────────────────────

@app.get("/doctors")
def list_doctors():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, specialty, email, phone, created_at FROM doctors ORDER BY id;")
            return {"doctors": cur.fetchall()}


@app.post("/doctors", status_code=201)
def create_doctor(doctor: DoctorIn):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    "INSERT INTO doctors (name, specialty, email, phone) VALUES (%s, %s, %s, %s)"
                    " RETURNING id, name, specialty, email, phone, created_at;",
                    (doctor.name, doctor.specialty, doctor.email, doctor.phone),
                )
                return cur.fetchone()
            except psycopg2.errors.UniqueViolation:
                raise HTTPException(status_code=409, detail="Email already exists")


@app.delete("/doctors/{doctor_id}", status_code=204)
def delete_doctor(doctor_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doctors WHERE id = %s RETURNING id;", (doctor_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Doctor not found")


# ── Patients ──────────────────────────────────────────────────────────────────

@app.get("/patients")
def list_patients():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.id, p.name, p.date_of_birth, p.email, p.phone_number,
                       p.notes, p.doctor_id, d.name AS doctor_name,
                       d.specialty AS doctor_specialty, p.created_at
                FROM patients p
                LEFT JOIN doctors d ON p.doctor_id = d.id
                ORDER BY p.id;
            """)
            return {"patients": cur.fetchall()}


@app.get("/patients/{patient_id}")
def get_patient(patient_id: int):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.id, p.name, p.date_of_birth, p.email, p.phone_number,
                       p.notes, p.doctor_id, d.name AS doctor_name,
                       d.specialty AS doctor_specialty, p.created_at
                FROM patients p
                LEFT JOIN doctors d ON p.doctor_id = d.id
                WHERE p.id = %s;
            """, (patient_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Patient not found")
    return row


@app.post("/patients", status_code=201)
def create_patient(patient: PatientIn):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    "INSERT INTO patients (name, date_of_birth, email, phone_number, notes, doctor_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " RETURNING id, name, date_of_birth, email, phone_number, notes, doctor_id, created_at;",
                    (patient.name, patient.date_of_birth, patient.email,
                     patient.phone_number, patient.notes, patient.doctor_id),
                )
                return cur.fetchone()
            except psycopg2.errors.UniqueViolation:
                raise HTTPException(status_code=409, detail="Email already exists")


@app.delete("/patients/{patient_id}", status_code=204)
def delete_patient(patient_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM patients WHERE id = %s RETURNING id;", (patient_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Patient not found")


# ── Appointments ───────────────────────────────────────────────────────────────

class AppointmentIn(BaseModel):
    patient_id:       int
    doctor_id:        int
    appointment_date: datetime
    reason:           str | None = None
    status:           str = "scheduled"


@app.get("/appointments")
def list_appointments():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT a.id, a.appointment_date, a.reason, a.status, a.created_at,
                       p.id AS patient_id, p.name AS patient_name,
                       d.id AS doctor_id, d.name AS doctor_name, d.specialty AS doctor_specialty
                FROM appointments a
                JOIN patients p ON a.patient_id = p.id
                JOIN doctors  d ON a.doctor_id  = d.id
                ORDER BY a.appointment_date DESC;
            """)
            return {"appointments": cur.fetchall()}


@app.post("/appointments", status_code=201)
def create_appointment(appt: AppointmentIn):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO appointments (patient_id, doctor_id, appointment_date, reason, status)"
                " VALUES (%s, %s, %s, %s, %s)"
                " RETURNING id, patient_id, doctor_id, appointment_date, reason, status, created_at;",
                (appt.patient_id, appt.doctor_id, appt.appointment_date, appt.reason, appt.status),
            )
            return cur.fetchone()


@app.delete("/appointments/{appointment_id}", status_code=204)
def delete_appointment(appointment_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM appointments WHERE id = %s RETURNING id;", (appointment_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Appointment not found")
