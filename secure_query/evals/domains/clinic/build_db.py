"""Deterministic synthetic clinic database — a second eval domain unrelated to Chinook.

Exists to catch fixes that only work on Chinook: different vocabulary, snake_case
names, PII spread across tables, and a business rule (revenue = completed visits
only) that lives in the catalog's instructions, not in code.

Seeded: the same rows on every machine, so reference answers are stable.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "data" / "clinic.duckdb"

_DEPARTMENTS = [
    ("Cardiology", 9),
    ("Dermatology", 4),
    ("Pediatrics", 7),
    ("Orthopedics", 5),
    ("Neurology", 3),
    ("General Practice", 12),
]
_FIRST = ["Ana", "Ben", "Chen", "Dara", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jo",
          "Kai", "Lena", "Mo", "Nia", "Omar", "Pia", "Quinn", "Ravi", "Sara", "Tom"]
_LAST = ["Abbott", "Baker", "Costa", "Diaz", "Evans", "Fischer", "Garcia", "Hughes",
         "Ito", "Jensen", "Khan", "Lopez", "Meyer", "Novak", "Okafor", "Patel",
         "Quist", "Rossi", "Silva", "Tanaka", "Ueda", "Vance", "Weber", "Young"]
_CITIES = [("Springfield", 30), ("Riverton", 18), ("Lakeside", 11), ("Hillcrest", 6), ("Oakdale", 3)]
_PLANS = [("Basic", 50), ("Plus", 32), ("Premium", 18)]
_MEDICATIONS = [
    ("Amoxicillin", 12.5), ("Ibuprofen", 4.25), ("Atorvastatin", 18.0), ("Lisinopril", 9.75),
    ("Metformin", 7.5), ("Cetirizine", 5.0), ("Hydrocortisone", 11.25), ("Sumatriptan", 24.0),
]
_STATUS = [("completed", 78), ("cancelled", 13), ("no_show", 9)]
_FEE_BY_DEPT = {1: 220.0, 2: 140.0, 3: 110.0, 4: 180.0, 5: 260.0, 6: 90.0}


def _weighted(rng: random.Random, pairs):
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def build(path: Path = DB_PATH) -> Path:
    import duckdb

    rng = random.Random(20261004)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE departments (department_id INTEGER, name VARCHAR, floor INTEGER);
            CREATE TABLE doctors (doctor_id INTEGER, first_name VARCHAR, last_name VARCHAR,
                department_id INTEGER, hire_date DATE, email VARCHAR);
            CREATE TABLE patients (patient_id INTEGER, first_name VARCHAR, last_name VARCHAR,
                birth_date DATE, phone VARCHAR, email VARCHAR, city VARCHAR, insurance_plan VARCHAR);
            CREATE TABLE appointments (appointment_id INTEGER, patient_id INTEGER, doctor_id INTEGER,
                appointment_date TIMESTAMP, status VARCHAR, fee DOUBLE, duration_minutes INTEGER);
            CREATE TABLE medications (medication_id INTEGER, name VARCHAR, unit_price DOUBLE);
            CREATE TABLE prescriptions (prescription_id INTEGER, appointment_id INTEGER,
                medication_id INTEGER, quantity INTEGER);
            """
        )
        con.executemany(
            "INSERT INTO departments VALUES (?, ?, ?)",
            [(i, name, 1 + i % 3) for i, (name, _) in enumerate(_DEPARTMENTS, start=1)],
        )

        doctors = []
        doctor_id = 0
        for dept_id, (_, n_doctors) in enumerate(_DEPARTMENTS, start=1):
            for _ in range(max(1, n_doctors // 3)):
                doctor_id += 1
                first, last = rng.choice(_FIRST), _LAST[(doctor_id * 5) % len(_LAST)]
                hired = date(2010, 1, 1) + timedelta(days=rng.randint(0, 4500))
                doctors.append((doctor_id, first, last, dept_id, hired,
                                f"{first.lower()}.{last.lower()}{doctor_id}@clinic.test"))
        con.executemany("INSERT INTO doctors VALUES (?, ?, ?, ?, ?, ?)", doctors)
        dept_weight = {d: w for d, (_, w) in enumerate(_DEPARTMENTS, start=1)}
        doctor_weights = [(d[0], dept_weight[d[3]] + (d[0] % 4)) for d in doctors]

        patients = []
        for pid in range(1, 241):
            first, last = rng.choice(_FIRST), rng.choice(_LAST)
            born = date(1940, 1, 1) + timedelta(days=rng.randint(0, 29000))
            patients.append((pid, first, last, born, f"555-{rng.randint(1000, 9999)}",
                             f"p{pid}@mail.test", _weighted(rng, _CITIES), _weighted(rng, _PLANS)))
        con.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?, ?)", patients)

        appointments = []
        start = datetime(2023, 1, 1, 8, 0)
        for aid in range(1, 1601):
            doctor = _weighted(rng, doctor_weights)
            dept = next(d[3] for d in doctors if d[0] == doctor)
            when = start + timedelta(days=rng.randint(0, 1094), hours=rng.randint(0, 9))
            status = _weighted(rng, _STATUS)
            fee = round(_FEE_BY_DEPT[dept] * rng.choice([0.8, 1.0, 1.0, 1.25]), 2)
            appointments.append((aid, rng.randint(1, 240), doctor, when, status, fee,
                                 rng.choice([15, 20, 30, 30, 45, 60])))
        con.executemany("INSERT INTO appointments VALUES (?, ?, ?, ?, ?, ?, ?)", appointments)

        con.executemany(
            "INSERT INTO medications VALUES (?, ?, ?)",
            [(i, name, price) for i, (name, price) in enumerate(_MEDICATIONS, start=1)],
        )
        med_weights = [(i, w) for i, w in zip(range(1, 9), [30, 26, 14, 11, 9, 6, 3, 1])]
        prescriptions = []
        pres_id = 0
        for aid, _p, _d, _when, status, _fee, _dur in appointments:
            if status != "completed" or rng.random() > 0.55:
                continue
            for _ in range(rng.choice([1, 1, 2])):
                pres_id += 1
                prescriptions.append((pres_id, aid, _weighted(rng, med_weights), rng.choice([1, 1, 2, 3])))
        con.executemany("INSERT INTO prescriptions VALUES (?, ?, ?, ?)", prescriptions)
    finally:
        con.close()
    return path


if __name__ == "__main__":
    print(f"Wrote {build()}")
