from datetime import date
from io import BytesIO
import os
import sqlite3
import re
from pathlib import Path
import uuid
from flask import Flask, g, has_request_context, redirect, render_template, request, send_file, send_from_directory, url_for

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
DATABASE = Path(app.root_path) / "school_fees.db"
SCHOOLS_DATABASE = Path(app.root_path) / "schools.sqlite"
IMAGE_DIRECTORY = Path(app.root_path) / "images"
ACADEMIC_YEAR = "2026/2027"
TERMS = {1: "First term", 2: "Second term", 3: "Third term"}
CLASS_LEVELS = [("creche", "Creche"), ("nursery-1", "Nursery 1"), ("nursery-2", "Nursery 2"), ("kg-1", "KG 1"), ("kg-2", "KG 2")] + [(f"basic-{level}", f"Basic {level}") for level in range(1, 10)]


@app.get("/style.css")
def static_css():
    """Serve the shared offline stylesheet."""
    return send_from_directory(app.static_folder, "style.css")


@app.get("/sw.js")
def service_worker():
    """Serve the service worker with permission to control the whole app."""
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/images/<path:filename>")
def class_image(filename):
    """Serve images from the workspace images directory."""
    return send_from_directory(IMAGE_DIRECTORY, filename)


def add_class_images(classes):
    """Attach images whose filename matches a class id or normalized name."""
    extensions = (".webp", ".png", ".jpg", ".jpeg")
    for school_class in classes:
        name_slug = re.sub(r"[^a-z0-9]+", "-", school_class["name"].casefold()).strip("-")
        for stem in (school_class["id"], name_slug):
            image = next((f"{stem}{extension}" for extension in extensions if (IMAGE_DIRECTORY / f"{stem}{extension}").is_file()), None)
            if image:
                school_class["image_url"] = url_for("class_image", filename=image)
                break


@app.get("/search-students")
def search_students():
    """Return matching student names for the top-bar live search suggestions."""
    query = request.args.get("q", "").strip().casefold()
    if not query:
        return {"students": []}
    rows = db().execute("SELECT id, name, initials FROM students ORDER BY name").fetchall()
    return {"students": [dict(row) for row in rows if query in row["name"].casefold() or query in row["id"].casefold()][:8]}


@app.post("/theme")
def set_theme():
    """Save the user's preferred light, dark, or system theme in a cookie."""
    theme = request.form.get("theme", "system")
    response = redirect(request.referrer or url_for("dashboard"))
    response.set_cookie("theme", theme if theme in {"light", "dark", "system"} else "system", max_age=31536000, samesite="Lax")
    return response


@app.get("/settings")
def settings_page():
    """Render administrator, school, and database recovery settings."""
    values = {row["key"]: row["value"] for row in db().execute(
        "SELECT key, value FROM settings WHERE key IN ('school_name', 'admin_name')"
    ).fetchall()}
    return render_template(
        "settings.html",
        page="Settings",
        school_name=values.get("school_name", "Mikelin School Complex"),
        admin_name=values.get("admin_name", ""),
        notice=request.args.get("notice", ""),
        notice_type=request.args.get("type", "success"),
    )


@app.post("/settings/profile")
def save_profile():
    """Save the school and administrator names shown throughout the app."""
    school_name = request.form.get("school_name", "").strip()
    admin_name = request.form.get("admin_name", "").strip()
    if not school_name:
        return redirect(url_for("settings_page", notice="Enter a school name before saving.", type="error"))
    connection = db()
    connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('school_name', ?)", (school_name,))
    connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('admin_name', ?)", (admin_name,))
    connection.commit()
    registry = school_registry_connection()
    registry.execute("UPDATE schools SET name=? WHERE id=?", (school_name, active_school_id()))
    registry.commit()
    registry.close()
    return redirect(url_for("settings_page", notice="Settings saved."))


@app.post("/settings/profile/delete")
def delete_profile():
    """Clear the active school's administrator profile."""
    if request.form.get("confirm_delete") != "yes":
        return redirect(url_for("settings_page", notice="Confirm before clearing the administrator profile.", type="error"))
    db().execute("UPDATE settings SET value='' WHERE key='admin_name'")
    db().commit()
    return redirect(url_for("settings_page", notice="Administrator profile cleared."))


@app.post("/settings/appearance")
def save_school_appearance():
    """Save or remove the active school's logo and background image."""
    allowed_types = {
        ".png": ("image/png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
        ".jpg": ("image/jpeg", lambda data: data.startswith(b"\xff\xd8\xff")),
        ".jpeg": ("image/jpeg", lambda data: data.startswith(b"\xff\xd8\xff")),
        ".webp": ("image/webp", lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP"),
    }
    uploads = (("logo", "logo"), ("background", "background"))
    pending_assets = {}
    for field, kind in uploads:
        uploaded = request.files.get(field)
        if not uploaded or not uploaded.filename:
            continue
        extension = Path(uploaded.filename).suffix.casefold()
        image_type = allowed_types.get(extension)
        data = uploaded.read(5 * 1024 * 1024 + 1)
        if image_type is None or len(data) > 5 * 1024 * 1024 or not image_type[1](data):
            return redirect(url_for("settings_page", notice="Use a valid PNG, JPEG, or WebP image up to 5 MB.", type="error"))
        pending_assets[kind] = (image_type[0], data)

    connection = db()
    for _field, kind in uploads:
        if request.form.get(f"clear_{kind}") == "yes":
            connection.execute("DELETE FROM school_assets WHERE kind=?", (kind,))
    for kind, (mime_type, data) in pending_assets.items():
        connection.execute(
            "INSERT INTO school_assets(kind, mime_type, data) VALUES (?, ?, ?) ON CONFLICT(kind) DO UPDATE SET mime_type=excluded.mime_type, data=excluded.data",
            (kind, mime_type, data),
        )
    connection.commit()
    return redirect(url_for("settings_page", notice="School images updated."))


@app.get("/school-asset/<kind>")
def school_asset(kind):
    """Serve the active school's logo or background from its database."""
    if kind not in {"logo", "background"}:
        return "Not found", 404
    asset = db().execute("SELECT mime_type, data FROM school_assets WHERE kind=?", (kind,)).fetchone()
    if asset is None:
        return "Not found", 404
    return send_file(BytesIO(asset["data"]), mimetype=asset["mime_type"], max_age=0)


@app.post("/settings/schools")
def add_school():
    """Create an isolated database and register a new school workspace."""
    school_name = request.form.get("school_name", "").strip()
    if not school_name:
        return redirect(url_for("settings_page", notice="Enter a school name.", type="error"))

    school_id = uuid.uuid4().hex
    database_filename = f"school_{school_id}.sqlite"
    database_path = SCHOOLS_DATABASE.parent / database_filename
    init_db(database_path, seed_students=False)
    connection = connect_db(database_path)
    connection.execute("UPDATE settings SET value=? WHERE key='school_name'", (school_name,))
    connection.commit()
    connection.close()

    registry = school_registry_connection()
    registry.execute(
        "INSERT INTO schools(id, name, database_file) VALUES (?, ?, ?)",
        (school_id, school_name, database_filename),
    )
    registry.commit()
    registry.close()
    response = redirect(url_for("settings_page", notice=f"{school_name} was added."))
    response.set_cookie("school_id", school_id, max_age=31536000, samesite="Lax")
    return response


@app.post("/settings/switch-school")
def switch_school():
    """Switch the active school database using a validated registry id."""
    school_id = request.form.get("school_id", "")
    registry = school_registry_connection()
    school = registry.execute("SELECT id FROM schools WHERE id=?", (school_id,)).fetchone()
    registry.close()
    if not school:
        return redirect(url_for("settings_page", notice="Choose a valid school.", type="error"))
    response = redirect(url_for("settings_page"))
    response.set_cookie("school_id", school["id"], max_age=31536000, samesite="Lax")
    return response


@app.post("/settings/schools/<school_id>/delete")
def delete_school(school_id):
    """Delete a confirmed added school without allowing removal of the default school."""
    if request.form.get("confirm_delete") != "yes":
        return redirect(url_for("settings_page", notice="Confirm before deleting the school.", type="error"))
    registry = school_registry_connection()
    school = registry.execute("SELECT id, database_file FROM schools WHERE id=?", (school_id,)).fetchone()
    registry.close()
    if not school or school["id"] == "default":
        return redirect(url_for("settings_page", notice="The default school cannot be deleted.", type="error"))

    was_active = active_school_id() == school_id
    if was_active:
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()
    database_path = SCHOOLS_DATABASE.parent / school["database_file"]
    try:
        database_path.unlink(missing_ok=True)
    except OSError:
        return redirect(url_for("settings_page", notice="The school database could not be removed.", type="error"))

    registry = school_registry_connection()
    registry.execute("DELETE FROM schools WHERE id=?", (school_id,))
    registry.commit()
    registry.close()
    response = redirect(url_for("settings_page", notice="School and its records were deleted."))
    if was_active:
        response.set_cookie("school_id", "default", max_age=31536000, samesite="Lax")
    return response


@app.get("/settings/backup")
def download_backup():
    """Download a consistent SQLite snapshot of the active school."""
    snapshot = sqlite3.connect(":memory:")
    db().backup(snapshot)
    backup_data = snapshot.serialize()
    snapshot.close()
    school_name = db().execute("SELECT value FROM settings WHERE key='school_name'").fetchone()[0]
    school_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", school_name).strip("_") or "school"
    return send_file(
        BytesIO(backup_data),
        mimetype="application/vnd.sqlite3",
        as_attachment=True,
        download_name=f"{school_slug}_backup_{date.today().isoformat()}.sqlite",
    )


@app.post("/settings/restore")
def restore_backup():
    """Restore a validated SQLite backup after explicit confirmation."""
    backup_file = request.files.get("backup_file")
    if not backup_file or not backup_file.filename or request.form.get("confirm_restore") != "yes":
        return redirect(url_for("settings_page", notice="Choose a backup and confirm before restoring.", type="error"))

    candidate = sqlite3.connect(":memory:")
    try:
        candidate.deserialize(backup_file.read())
        integrity = candidate.execute("PRAGMA quick_check").fetchone()[0]
        tables = {row[0] for row in candidate.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required_tables = {"settings", "classes", "students", "fees", "payments", "term_balances"}
        if integrity != "ok" or not required_tables.issubset(tables):
            raise sqlite3.DatabaseError("Invalid school-fees backup.")
        candidate.execute("CREATE TABLE IF NOT EXISTS families (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '')")
        candidate.execute("CREATE TABLE IF NOT EXISTS school_assets (kind TEXT PRIMARY KEY, mime_type TEXT NOT NULL, data BLOB NOT NULL)")
        student_columns = {row[1] for row in candidate.execute("PRAGMA table_info(students)").fetchall()}
        if "gender" not in student_columns:
            candidate.execute("ALTER TABLE students ADD COLUMN gender TEXT NOT NULL DEFAULT 'unspecified'")
        if "family_id" not in student_columns:
            candidate.execute("ALTER TABLE students ADD COLUMN family_id TEXT REFERENCES families(id) ON DELETE SET NULL")
        candidate.execute("CREATE TABLE IF NOT EXISTS student_fee_overrides (student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE, academic_year TEXT NOT NULL, term INTEGER NOT NULL, amount INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(student_id, academic_year, term))")
        candidate.backup(db())
        db().commit()
    except (sqlite3.DatabaseError, ValueError):
        return redirect(url_for("settings_page", notice="Backup was not restored: invalid database file.", type="error"))
    finally:
        candidate.close()

    return redirect(url_for("settings_page", notice="Backup restored successfully."))


@app.post("/academic-year")
def switch_academic_year():
    """Set an open-ended YYYY/YYYY academic year and create its fee periods."""
    year = request.form.get("academic_year", ACADEMIC_YEAR)
    if re.fullmatch(r"\d{4}/\d{4}", year):
        connection = db()
        connection.execute("UPDATE settings SET value=? WHERE key='academic_year'", (year,))
        for class_id, _name in CLASS_LEVELS:
            for term in TERMS:
                connection.execute("INSERT OR IGNORE INTO fees(class_id, academic_year, term) VALUES (?, ?, ?)", (class_id, year, term))
        connection.commit()
    return redirect(request.referrer or url_for("dashboard"))


def school_registry_connection():
    connection = sqlite3.connect(SCHOOLS_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def list_schools():
    connection = school_registry_connection()
    schools = [dict(row) for row in connection.execute("SELECT id, name FROM schools ORDER BY name").fetchall()]
    connection.close()
    return schools


def active_school_id():
    if not has_request_context():
        return "default"
    requested_id = request.cookies.get("school_id", "default")
    connection = school_registry_connection()
    school = connection.execute("SELECT id FROM schools WHERE id=?", (requested_id,)).fetchone()
    connection.close()
    return school["id"] if school else "default"


def school_database_path():
    if not has_request_context():
        return DATABASE
    school_id = active_school_id()
    connection = school_registry_connection()
    school = connection.execute("SELECT database_file FROM schools WHERE id=?", (school_id,)).fetchone()
    connection.close()
    return SCHOOLS_DATABASE.parent / school["database_file"] if school and school_id != "default" else DATABASE


def connect_db(database_path=None):
    """Open a SQLite connection with named-row access and foreign keys enabled."""
    connection = sqlite3.connect(database_path or school_database_path())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def db():
    """Return the request-scoped database connection."""
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    """Close the request-scoped SQLite connection after each request."""
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db(database_path=None, seed_students=True):
    """Create the local schema and seed school classes and starter students."""
    connection = connect_db(database_path)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS school_assets (kind TEXT PRIMARY KEY, mime_type TEXT NOT NULL, data BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS classes (id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_order INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS families (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS students (id TEXT PRIMARY KEY, name TEXT NOT NULL, initials TEXT NOT NULL, gender TEXT NOT NULL DEFAULT 'unspecified', family_id TEXT REFERENCES families(id) ON DELETE SET NULL, class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS fees (class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE, academic_year TEXT NOT NULL, term INTEGER NOT NULL, fees INTEGER NOT NULL DEFAULT 0, textbooks INTEGER NOT NULL DEFAULT 0, exercise_books INTEGER NOT NULL DEFAULT 0, notebooks INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(class_id, academic_year, term));
        CREATE TABLE IF NOT EXISTS student_fee_overrides (student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE, academic_year TEXT NOT NULL, term INTEGER NOT NULL, amount INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(student_id, academic_year, term));
        CREATE TABLE IF NOT EXISTS term_balances (student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE, academic_year TEXT NOT NULL, term INTEGER NOT NULL, opening_arrears INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(student_id, academic_year, term));
        CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE, academic_year TEXT NOT NULL, term INTEGER NOT NULL, amount INTEGER NOT NULL, payment_date TEXT NOT NULL, method TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'fees');
    """)
    payment_columns = {column["name"] for column in connection.execute("PRAGMA table_info(payments)").fetchall()}
    if "category" not in payment_columns:
        connection.execute("ALTER TABLE payments ADD COLUMN category TEXT NOT NULL DEFAULT 'fees'")
    student_columns = {column["name"] for column in connection.execute("PRAGMA table_info(students)").fetchall()}
    if "gender" not in student_columns:
        connection.execute("ALTER TABLE students ADD COLUMN gender TEXT NOT NULL DEFAULT 'unspecified'")
    if "family_id" not in student_columns:
        connection.execute("ALTER TABLE students ADD COLUMN family_id TEXT REFERENCES families(id) ON DELETE SET NULL")
    connection.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('academic_year', ?)", (ACADEMIC_YEAR,))
    connection.execute("UPDATE settings SET value=? WHERE key='academic_year' AND value='2025/26'", (ACADEMIC_YEAR,))
    connection.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('term', '1')")
    connection.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('school_name', 'Mikelin School Complex')")
    connection.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('admin_name', '')")
    for order, (class_id, name) in enumerate(CLASS_LEVELS):
        connection.execute("INSERT OR IGNORE INTO classes(id, name, sort_order) VALUES (?, ?, ?)", (class_id, name, order))
        for term in TERMS:
            connection.execute("INSERT OR IGNORE INTO fees(class_id, academic_year, term) VALUES (?, ?, ?)", (class_id, ACADEMIC_YEAR, term))
    if seed_students and connection.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0:
        connection.executemany("INSERT INTO students(id, name, initials, class_id) VALUES (?, ?, ?, ?)", [("MG-2401", "Amelia Nartey", "AN", "basic-1"), ("MG-2402", "Daniel Mensah", "DM", "basic-1")])
    connection.commit()
    connection.close()


def reset_school_state_for_fresh_deploy():
    """Reset the app to a single default database when a clean deployment is requested."""
    if os.environ.get("RESET_DATA_ON_START", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return

    for database_path in sorted(Path(app.root_path).glob("school_*.sqlite")):
        if database_path.name != "school_fees.db":
            database_path.unlink(missing_ok=True)

    if SCHOOLS_DATABASE.exists():
        SCHOOLS_DATABASE.unlink()
    if DATABASE.exists():
        DATABASE.unlink()

    init_db(DATABASE, seed_students=True)
    init_school_registry()


def init_school_registry():
    """Register the existing database as the first school workspace."""
    school_database = connect_db(DATABASE)
    school_name = school_database.execute(
        "SELECT value FROM settings WHERE key='school_name'"
    ).fetchone()[0]
    school_database.close()
    registry = school_registry_connection()
    registry.execute("CREATE TABLE IF NOT EXISTS schools (id TEXT PRIMARY KEY, name TEXT NOT NULL, database_file TEXT NOT NULL UNIQUE)")
    registry.execute(
        "INSERT OR IGNORE INTO schools(id, name, database_file) VALUES ('default', ?, 'school_fees.db')",
        (school_name,),
    )
    registry.commit()
    registry.close()


def migrate_registered_schools():
    """Apply additive schema migrations to every registered school database."""
    registry = school_registry_connection()
    schools = registry.execute("SELECT id, database_file FROM schools").fetchall()
    registry.close()
    for school in schools:
        if school["id"] != "default":
            init_db(SCHOOLS_DATABASE.parent / school["database_file"], seed_students=False)


def current_period():
    """Return the active academic year and term from saved settings."""
    rows = db().execute("SELECT key, value FROM settings WHERE key IN ('academic_year', 'term')").fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    return settings.get("academic_year", ACADEMIC_YEAR), int(settings.get("term", 1))


def fee_total(class_id, academic_year, term):
    """Calculate a class student's combined fees and book charges."""
    row = db().execute("SELECT fees, textbooks, exercise_books, notebooks FROM fees WHERE class_id=? AND academic_year=? AND term=?", (class_id, academic_year, term)).fetchone()
    return sum(row[key] for key in ("fees", "textbooks", "exercise_books", "notebooks")) if row else 0

def opening_arrears(student_id, academic_year, term):
    """Return arrears carried into the selected term for one student."""
    row = db().execute("SELECT opening_arrears FROM term_balances WHERE student_id=? AND academic_year=? AND term=?", (student_id, academic_year, term)).fetchone()
    return row["opening_arrears"] if row else 0

def payment_total(student_id, academic_year, term, category="fees"):
    """Sum payments in one category for one student and term."""
    row = db().execute("SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE student_id=? AND academic_year=? AND term=? AND category=?", (student_id, academic_year, term, category)).fetchone()
    return row["total"]


def student_fee_amount(student_id, class_id, academic_year, term):
    """Return a student's term fee override, or their class fee when unset."""
    row = db().execute(
        "SELECT amount FROM student_fee_overrides WHERE student_id=? AND academic_year=? AND term=?",
        (student_id, academic_year, term),
    ).fetchone()
    return row["amount"] if row else fee_config(class_id, academic_year, term)["fees"]

def class_row(class_id):
    """Fetch one class record by its stable identifier."""
    return db().execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()


def student_row(student_id):
    """Fetch one student record by its stable identifier."""
    return db().execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()


def decorate_class(row, academic_year, term):
    """Add calculated student count, expected fees, and collection totals to a class."""
    class_id = row["id"]
    students = db().execute("SELECT COUNT(*) AS count FROM students WHERE class_id=?", (class_id,)).fetchone()["count"]
    config = fee_config(class_id, academic_year, term)
    total = config["fees"]
    arrears = db().execute("""
        SELECT COALESCE(SUM(tb.opening_arrears), 0) AS total
        FROM term_balances tb JOIN students s ON s.id=tb.student_id
        WHERE s.class_id=? AND tb.academic_year=? AND tb.term=?
    """, (class_id, academic_year, term)).fetchone()["total"]
    expected_fees = db().execute("""
        SELECT COALESCE(SUM(COALESCE(sfo.amount, f.fees, 0)), 0) AS total
        FROM students s
        LEFT JOIN fees f ON f.class_id=s.class_id AND f.academic_year=? AND f.term=?
        LEFT JOIN student_fee_overrides sfo ON sfo.student_id=s.id AND sfo.academic_year=? AND sfo.term=?
        WHERE s.class_id=?
    """, (academic_year, term, academic_year, term, class_id)).fetchone()["total"]
    expected = expected_fees + arrears
    paid = db().execute("SELECT COALESCE(SUM(p.amount), 0) AS total FROM payments p JOIN students s ON s.id=p.student_id WHERE s.class_id=? AND p.academic_year=? AND p.term=? AND p.category='fees'", (class_id, academic_year, term)).fetchone()["total"]
    result = dict(row)
    result.update(students=students, fee_total=total, expected=expected, paid=paid, fees=config, books_total=config["textbooks"] + config["exercise_books"] + config["notebooks"])
    return result


def fee_config(class_id, academic_year, term):
    """Return the four configured fee components for a class and term."""
    row = db().execute("SELECT fees, textbooks, exercise_books, notebooks FROM fees WHERE class_id=? AND academic_year=? AND term=?", (class_id, academic_year, term)).fetchone()
    return dict(row) if row else {"fees": 0, "textbooks": 0, "exercise_books": 0, "notebooks": 0}


def decorate_student(row, academic_year, term):
    """Add paid amount, arrears, and payment status to a student record."""
    result = dict(row)
    config = fee_config(row["class_id"], academic_year, term)
    override = db().execute(
        "SELECT amount FROM student_fee_overrides WHERE student_id=? AND academic_year=? AND term=?",
        (row["id"], academic_year, term),
    ).fetchone()
    result["fee_override"] = override is not None
    result["total"] = override["amount"] if override else config["fees"]
    result["books_total"] = config["textbooks"] + config["exercise_books"] + config["notebooks"]
    result["opening_arrears"] = opening_arrears(row["id"], academic_year, term)
    result["paid"] = payment_total(row["id"], academic_year, term, "fees")
    result["books_paid"] = payment_total(row["id"], academic_year, term, "books")
    result["owing"] = max(result["total"] + result["opening_arrears"] - result["paid"], 0)
    result["books_owing"] = max(result["books_total"] - result["books_paid"], 0)
    result["status"], result["status_class"] = (("Not paid", "unpaid") if result["paid"] == 0 else ("Partial payment", "partial") if result["owing"] else ("Paid up", "paid"))
    return result


def family_balance_summary(students):
    """Recalculate the family total from each child’s effective fee, payments, and arrears."""
    expected = sum(student["total"] + student["opening_arrears"] for student in students)
    paid = sum(student["paid"] for student in students)
    owing = max(expected - paid, 0)
    return {"students": len(students), "expected": expected, "paid": paid, "owing": owing}


@app.template_filter("money")
def money(value):
    """Format a numeric amount as Ghana cedis."""
    return f"GH₵{value:,.0f}"


@app.context_processor
def shared_values():
    """Provide active-period, theme, and school-wide totals to every template."""
    academic_year, term = current_period()
    profile = {row["key"]: row["value"] for row in db().execute(
        "SELECT key, value FROM settings WHERE key IN ('school_name', 'admin_name')"
    ).fetchall()}
    admin_name = profile.get("admin_name", "")
    rows = db().execute("SELECT id, name FROM classes ORDER BY sort_order").fetchall()
    totals = [decorate_class(row, academic_year, term) for row in rows]
    families = db().execute("SELECT id, name FROM families ORDER BY name").fetchall()
    assets = {row["kind"] for row in db().execute("SELECT kind FROM school_assets").fetchall()}
    return {"academic_year": academic_year, "term": term, "term_name": TERMS[term], "terms": TERMS, "theme": request.cookies.get("theme", "system"), "school_name": profile.get("school_name", "Mikelin School Complex"), "admin_name": admin_name, "admin_initials": "".join(part[0] for part in admin_name.split()[:2]).upper(), "school_logo_url": url_for("school_asset", kind="logo") if "logo" in assets else "", "school_background_url": url_for("school_asset", kind="background") if "background" in assets else "", "schools": list_schools(), "active_school_id": active_school_id(), "families": families, "class_count": len(rows), "expected": sum(item["expected"] for item in totals), "collected": sum(item["paid"] for item in totals), "outstanding": sum(item["expected"] - item["paid"] for item in totals)}


@app.get("/")
def dashboard():
    """Render the overview without embedding the class or enrollment pages."""
    if request.args.get("view") == "classes":
        return redirect(url_for("classes_page"))
    academic_year, term = current_period()
    query = request.args.get("q", "").strip().casefold()
    student_rows = db().execute("SELECT * FROM students ORDER BY name").fetchall()
    students = [dict(student) for student in student_rows if query and (query in student["name"].casefold() or query in student["id"].casefold())]
    return render_template("dashboard.html", students=students, query=request.args.get("q", ""), page="Overview")


@app.get("/classes")
def classes_page():
    """Render the class directory on its own page."""
    academic_year, term = current_period()
    query = request.args.get("q", "").strip().casefold()
    rows = db().execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
    classes = [decorate_class(row, academic_year, term) for row in rows if query in row["name"].casefold()]
    add_class_images(classes)
    return render_template("classes.html", classes=classes, query=request.args.get("q", ""), page="Classes")


@app.get("/enrollment")
def enrollment_page():
    """Show student totals by class and recorded gender."""
    classes = db().execute("""
        SELECT c.id, c.name, COUNT(s.id) AS total,
            SUM(CASE WHEN s.gender='male' THEN 1 ELSE 0 END) AS boys,
            SUM(CASE WHEN s.gender='female' THEN 1 ELSE 0 END) AS girls,
            SUM(CASE WHEN s.id IS NOT NULL AND s.gender NOT IN ('male', 'female') THEN 1 ELSE 0 END) AS unrecorded
        FROM classes c LEFT JOIN students s ON s.class_id=c.id
        GROUP BY c.id, c.name, c.sort_order
        ORDER BY c.sort_order
    """).fetchall()
    totals = {"students": sum(row["total"] for row in classes), "boys": sum(row["boys"] for row in classes), "girls": sum(row["girls"] for row in classes), "unrecorded": sum(row["unrecorded"] for row in classes)}
    return render_template("enrollment.html", classes=classes, totals=totals, page="Enrollment")


@app.get("/help")
def help_page():
    """Show working guidance for enrollment, school switching, and recovery."""
    return render_template("help.html", page="Help")


@app.get("/families")
def families_page():
    """List household profiles with current combined outstanding balances."""
    academic_year, term = current_period()
    family_rows = db().execute("SELECT * FROM families ORDER BY name").fetchall()
    families = []
    for row in family_rows:
        members = db().execute("SELECT * FROM students WHERE family_id=? ORDER BY name", (row["id"],)).fetchall()
        decorated = [decorate_student(member, academic_year, term) for member in members]
        family = dict(row)
        family.update(members=len(decorated), owing=sum(member["owing"] for member in decorated), paid=sum(member["paid"] for member in decorated))
        families.append(family)
    return render_template("families.html", families=families, page="Families", notice=request.args.get("notice", ""), notice_type=request.args.get("type", "success"))


@app.post("/families")
def create_family():
    """Create a family profile for siblings enrolled at the school."""
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("families_page", notice="Enter a family name.", type="error"))
    family_id = uuid.uuid4().hex
    db().execute(
        "INSERT INTO families(id, name, phone, address) VALUES (?, ?, ?, ?)",
        (family_id, name, request.form.get("phone", "").strip(), request.form.get("address", "").strip()),
    )
    db().commit()
    return redirect(url_for("family_details", family_id=family_id))


@app.get("/family/<family_id>")
def family_details(family_id):
    """Show family members, combined balances, and family payment entry."""
    family = db().execute("SELECT * FROM families WHERE id=?", (family_id,)).fetchone()
    if family is None:
        return redirect(url_for("families_page"))
    academic_year, term = current_period()
    rows = db().execute("SELECT * FROM students WHERE family_id=? ORDER BY name", (family_id,)).fetchall()
    students = [decorate_student(row, academic_year, term) for row in rows]
    classes = db().execute("SELECT id, name FROM classes ORDER BY sort_order").fetchall()
    unassigned_students = db().execute("""
        SELECT s.id, s.name, s.class_id, c.name AS class_name
        FROM students s JOIN classes c ON c.id=s.class_id
        WHERE s.family_id IS NULL ORDER BY s.name
    """).fetchall()
    payments = db().execute("""
        SELECT p.*, s.name AS student_name
        FROM payments p JOIN students s ON s.id=p.student_id
        WHERE s.family_id=? AND p.academic_year=? AND p.term=?
        ORDER BY p.payment_date DESC, p.id DESC
    """, (family_id, academic_year, term)).fetchall()
    summary = family_balance_summary(students)
    return render_template(
        "family.html", family=family, students=students, classes=classes,
        unassigned_students=unassigned_students, payments=payments,
        notice=request.args.get("notice", ""), notice_type=request.args.get("type", "success"),
        summary=summary, page=family["name"],
    )


@app.post("/family/<family_id>/students/create")
def create_family_student(family_id):
    """Create and enroll a new student directly into a family profile."""
    family = db().execute("SELECT id FROM families WHERE id=?", (family_id,)).fetchone()
    class_id = request.form.get("class_id", "")
    name = request.form.get("name", "").strip()
    gender = request.form.get("gender", "")
    if family is None or class_row(class_id) is None or not name or gender not in {"male", "female"}:
        return redirect(url_for("family_details", family_id=family_id, notice="Enter a name, class, and gender.", type="error"))
    initials = "".join(part[0] for part in name.split()[:2]).upper()
    next_id = db().execute("SELECT COALESCE(MAX(CAST(SUBSTR(id, 4) AS INTEGER)), 2400) + 1 FROM students").fetchone()[0]
    student_id = f"MG-{next_id}"
    db().execute(
        "INSERT INTO students(id, name, initials, gender, family_id, class_id) VALUES (?, ?, ?, ?, ?, ?)",
        (student_id, name, initials, gender, family_id, class_id),
    )
    db().commit()
    return redirect(url_for("family_details", family_id=family_id, notice="Student added to family."))


@app.post("/family/<family_id>/students/add-existing")
def add_existing_family_student(family_id):
    """Link an existing unassigned student to a family without moving fee history."""
    student_id = request.form.get("student_id", "")
    class_id = request.form.get("class_id", "")
    cursor = db().execute(
        "UPDATE students SET family_id=? WHERE id=? AND class_id=? AND family_id IS NULL AND EXISTS (SELECT 1 FROM families WHERE id=?) AND EXISTS (SELECT 1 FROM classes WHERE id=?)",
        (family_id, student_id, class_id, family_id, class_id),
    )
    db().commit()
    notice = "Student added to family." if cursor.rowcount else "Choose an unassigned student."
    return redirect(url_for("family_details", family_id=family_id, notice=notice))


@app.post("/family/<family_id>/students/<student_id>/remove")
def remove_family_student(family_id, student_id):
    """Remove a student from a family while preserving their school records."""
    db().execute("UPDATE students SET family_id=NULL WHERE id=? AND family_id=?", (student_id, family_id))
    db().commit()
    return redirect(url_for("family_details", family_id=family_id, notice="Student removed from this family."))


@app.post("/family/<family_id>/students/<student_id>/fee")
def update_family_student_fee(family_id, student_id):
    """Set an individual student's fee for the active year and term."""
    student = db().execute(
        "SELECT id FROM students WHERE id=? AND family_id=?",
        (student_id, family_id),
    ).fetchone()
    try:
        amount = int(request.form.get("amount", ""))
    except ValueError:
        amount = -1
    if student is None or amount < 0:
        return redirect(url_for("family_details", family_id=family_id, notice="Enter a valid non-negative fee for a family member.", type="error"))
    academic_year, term = current_period()
    db().execute(
        "INSERT INTO student_fee_overrides(student_id, academic_year, term, amount) VALUES (?, ?, ?, ?) ON CONFLICT(student_id, academic_year, term) DO UPDATE SET amount=excluded.amount",
        (student_id, academic_year, term, amount),
    )
    db().commit()
    return redirect(url_for("family_details", family_id=family_id, notice="Student fee updated."))


@app.post("/family/<family_id>/delete")
def delete_family(family_id):
    """Delete a family profile and leave its students enrolled and unassigned."""
    if request.form.get("confirm_delete") != "yes":
        return redirect(url_for("family_details", family_id=family_id, notice="Confirm before deleting this family.", type="error"))
    db().execute("UPDATE students SET family_id=NULL WHERE family_id=?", (family_id,))
    db().execute("DELETE FROM families WHERE id=?", (family_id,))
    db().commit()
    return redirect(url_for("families_page", notice="Family profile deleted; students and fee history were kept."))


@app.post("/family/<family_id>/payment")
def record_family_payment(family_id):
    """Allocate a family payment across children with outstanding balances."""
    family = db().execute("SELECT id FROM families WHERE id=?", (family_id,)).fetchone()
    try:
        amount = max(int(request.form.get("amount", 0)), 0)
    except ValueError:
        amount = 0
    if family is None:
        return redirect(url_for("families_page"))
    academic_year, term = current_period()
    category = request.form.get("category", "fees")
    category = category if category in {"fees", "books"} else "fees"
    balance_key = "owing" if category == "fees" else "books_owing"
    students = [
        decorate_student(row, academic_year, term)
        for row in db().execute(
            "SELECT * FROM students WHERE family_id=? ORDER BY name", (family_id,)
        ).fetchall()
    ]
    family_due = sum(student[balance_key] for student in students)
    if amount <= 0 or amount > family_due:
        notice = "Enter an amount from 1 to the family's outstanding balance."
        return redirect(url_for("family_details", family_id=family_id, notice=notice, type="error"))

    method = request.form.get("method", "Cash")
    method = method if method in {"MoMo", "Cash", "Bank transfer"} else "Cash"
    payment_date = request.form.get("payment_date") or date.today().isoformat()
    remaining = amount
    for student in students:
        allocation = min(remaining, student[balance_key])
        if allocation:
            db().execute(
                "INSERT INTO payments(student_id, academic_year, term, amount, payment_date, method, category) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (student["id"], academic_year, term, allocation, payment_date, method, category),
            )
            remaining -= allocation
        if remaining == 0:
            break
    db().commit()
    return redirect(url_for("family_details", family_id=family_id, notice="Family payment recorded."))


@app.get("/fees-status")
def fees_status_page():
    """List students by class with fee paid/owing status."""
    academic_year, term = current_period()
    selected_status = request.args.get("status", "all")
    if selected_status not in {"all", "owing", "paid"}:
        selected_status = "all"
    class_id = request.args.get("class_id", "")
    classes = db().execute("SELECT id, name FROM classes ORDER BY sort_order").fetchall()
    students = []
    for school_class in classes:
        if class_id and class_id != school_class["id"]:
            continue
        rows = db().execute("SELECT * FROM students WHERE class_id=? ORDER BY name", (school_class["id"],)).fetchall()
        for row in rows:
            student = decorate_student(row, academic_year, term)
            is_paid = student["status"] == "Paid up"
            if selected_status == "owing" and not student["owing"]:
                continue
            if selected_status == "paid" and not is_paid:
                continue
            student["class_name"] = school_class["name"]
            student["amount_due"] = student["total"] + student["opening_arrears"]
            students.append(student)
    return render_template("fees_status.html", students=students, classes=classes, class_id=class_id, selected_status=selected_status, page="Fees status")


@app.get("/class/<class_id>")
def class_details(class_id):
    """Render a class with its students, balances, and class fee settings."""
    academic_year, term = current_period()
    row = class_row(class_id)
    if row is None:
        return redirect(url_for("dashboard"))
    school_class = decorate_class(row, academic_year, term)
    query = request.args.get("q", "").strip().casefold()
    student_rows = db().execute("SELECT * FROM students WHERE class_id=? ORDER BY name", (class_id,)).fetchall()
    students = [decorate_student(student, academic_year, term) for student in student_rows if query in student["name"].casefold()]
    return render_template("class.html", school_class=school_class, students=students, query=request.args.get("q", ""), page=row["name"])


@app.get("/student/<student_id>")
def student_details(student_id):
    """Render one student's current balance and term payment history."""
    academic_year, term = current_period()
    row = student_row(student_id)
    if row is None:
        return redirect(url_for("dashboard"))
    school_class = decorate_class(class_row(row["class_id"]), academic_year, term)
    student = decorate_student(row, academic_year, term)
    payments = db().execute("SELECT * FROM payments WHERE student_id=? AND academic_year=? AND term=? ORDER BY payment_date DESC, id DESC", (student_id, academic_year, term)).fetchall()
    return render_template("student.html", student=student, school_class=school_class, payments=payments, page=row["name"])


@app.post("/term-switch")
def switch_term():
    """Switch terms and carry unpaid balances into the newly selected term."""
    academic_year, old_term = current_period()
    new_term = int(request.form.get("term", old_term))
    if new_term != old_term and new_term in TERMS:
        connection = db()
        for student in connection.execute("SELECT * FROM students").fetchall():
            old_total = student_fee_amount(student["id"], student["class_id"], academic_year, old_term)
            unpaid = max(old_total + opening_arrears(student["id"], academic_year, old_term) - payment_total(student["id"], academic_year, old_term, "fees"), 0)
            connection.execute("INSERT OR IGNORE INTO term_balances(student_id, academic_year, term, opening_arrears) VALUES (?, ?, ?, ?)", (student["id"], academic_year, new_term, unpaid))
        connection.execute("UPDATE settings SET value=? WHERE key='term'", (str(new_term),))
        connection.commit()
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/payment")
def record_payment():
    """Validate and save a payment in the active student's active term."""
    student = student_row(request.form.get("student_id", ""))
    if student:
        try:
            amount = max(int(request.form.get("amount", 0)), 0)
        except ValueError:
            amount = 0
        if amount:
            academic_year, term = current_period()
            method = request.form.get("method", "Cash") if request.form.get("method") in {"MoMo", "Cash", "Bank transfer"} else "Cash"
            category = request.form.get("category", "fees") if request.form.get("category") in {"fees", "books"} else "fees"
            db().execute("INSERT INTO payments(student_id, academic_year, term, amount, payment_date, method, category) VALUES (?, ?, ?, ?, ?, ?, ?)", (student["id"], academic_year, term, amount, request.form.get("payment_date") or date.today().isoformat(), method, category))
            db().commit()
    return redirect(url_for("student_details", student_id=student["id"])) if student else redirect(url_for("dashboard"))


@app.post("/payment/<int:payment_id>/delete")
def delete_payment(payment_id):
    """Delete one payment entry after an administrator marks it as false."""
    payment = db().execute("SELECT student_id FROM payments WHERE id=?", (payment_id,)).fetchone()
    if payment:
        db().execute("DELETE FROM payments WHERE id=?", (payment_id,))
        db().commit()
        return redirect(url_for("student_details", student_id=payment["student_id"]))
    return redirect(url_for("dashboard"))


@app.post("/payment/<int:payment_id>/edit")
def edit_payment(payment_id):
    """Correct the amount, date, method, or category of one payment entry."""
    payment = db().execute("SELECT student_id FROM payments WHERE id=?", (payment_id,)).fetchone()
    if payment:
        try:
            amount = max(int(request.form.get("amount", 0)), 0)
        except ValueError:
            amount = 0
        method = request.form.get("method", "Cash") if request.form.get("method") in {"MoMo", "Cash", "Bank transfer"} else "Cash"
        category = request.form.get("category", "fees") if request.form.get("category") in {"fees", "books"} else "fees"
        if amount:
            db().execute("UPDATE payments SET amount=?, payment_date=?, method=?, category=? WHERE id=?", (amount, request.form.get("payment_date") or date.today().isoformat(), method, category, payment_id))
            db().commit()
        return redirect(url_for("student_details", student_id=payment["student_id"]))
    return redirect(url_for("dashboard"))


@app.post("/class/<class_id>/fees")
def update_class_fees(class_id):
    """Save the four fee components for a class in the active term."""
    academic_year, term = current_period()
    if class_row(class_id) is None:
        return redirect(url_for("dashboard"))
    values = []
    for field in ("fees", "textbooks", "exercise_books", "notebooks"):
        try:
            values.append(max(int(request.form.get(field, 0)), 0))
        except ValueError:
            values.append(0)
    db().execute("UPDATE fees SET fees=?, textbooks=?, exercise_books=?, notebooks=? WHERE class_id=? AND academic_year=? AND term=?", (*values, class_id, academic_year, term))
    db().commit()
    return redirect(url_for("class_details", class_id=class_id))


@app.post("/class/<class_id>/student")
def add_student(class_id):
    """Create a student in the selected class with no payments yet."""
    if class_row(class_id) is None:
        return redirect(url_for("dashboard"))
    name = request.form.get("name", "").strip()
    gender = request.form.get("gender", "")
    family_id = request.form.get("family_id", "") or None
    if gender not in {"male", "female"}:
        return redirect(url_for("class_details", class_id=class_id, notice="Choose a gender for the student.", type="error"))
    if name:
        initials = "".join(part[0] for part in name.split()[:2]).upper()
        student_id = f"MG-{db().execute('SELECT COALESCE(MAX(CAST(SUBSTR(id, 4) AS INTEGER)), 2400) + 1 FROM students').fetchone()[0]}"
        family = db().execute("SELECT id FROM families WHERE id=?", (family_id,)).fetchone() if family_id else None
        db().execute("INSERT INTO students(id, name, initials, gender, family_id, class_id) VALUES (?, ?, ?, ?, ?, ?)", (student_id, name, initials, gender, family["id"] if family else None, class_id))
        db().commit()
    return redirect(url_for("class_details", class_id=class_id))


@app.post("/student/<student_id>/delete")
def delete_student(student_id):
    """Remove a student and their linked records when they leave the school."""
    student = student_row(student_id)
    if student:
        class_id = student["class_id"]
        db().execute("DELETE FROM students WHERE id=?", (student_id,))
        db().commit()
        return redirect(url_for("class_details", class_id=class_id))
    return redirect(url_for("dashboard"))


if os.environ.get("RESET_DATA_ON_START", "").strip().lower() in {"1", "true", "yes", "on"}:
    reset_school_state_for_fresh_deploy()
else:
    init_db()
    init_school_registry()
    migrate_registered_schools()

if __name__ == "__main__":
    app.run(debug=True, port=5000)

