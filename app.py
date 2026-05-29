def execute_safe(cur, sql, params=None):
    """
    Execute SQL with basic placeholder/param mismatch handling.
    If params is None or not a tuple, normalize it to a tuple. If there's a mismatch between
    sql.count('%s') and len(params) we log and pad/truncate params to avoid ProgrammingError.
    This is a temporary debugging helper — prefer fixing the real mismatch.
    """
    if params is None:
        params = ()
    # ensure params is a tuple (MySQL connector expects tuple for paramization)
    if not isinstance(params, tuple):
        try:
            params = tuple(params)
        except Exception:
            params = (params,)
    placeholders = sql.count('%s')
    if placeholders != len(params):
        import inspect
        stack = inspect.stack()[1:4]
        trace = ' -> '.join([f"{frame.filename}:{frame.lineno}" for frame in stack])
        msg = f"execute_safe: placeholder mismatch: placeholders={placeholders} len(params)={len(params)} SQL={sql} params={params} callstack={trace}"
        app.logger.error(msg)
        try:
            print(msg)
        except Exception:
            pass
        # If fewer params than placeholders - pad with None. If more - truncate.
        if len(params) < placeholders:
            params = tuple(list(params) + [None] * (placeholders - len(params)))
        elif len(params) > placeholders:
            params = params[:placeholders]
    result = cur.execute(sql, params)
    if DEBUG_SQL_MISMATCH:
        try:
            app.logger.info('execute_safe: placeholders=%s params_len=%s SQL=%s params=%s', placeholders, len(params), sql, params)
        except Exception:
            pass
    return result
from flask import Flask, render_template, request, redirect, flash, url_for, session, jsonify, Response
from datetime import datetime, timedelta, date
import math
import re
import os
import sys
import psycopg2
import mimetypes
import requests
import base64
import hashlib
import hmac
import json
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import secrets
import uuid
try:
    import psycopg2
    from psycopg2 import extras as psycopg2_extras
except ImportError:
    psycopg2 = None
    psycopg2_extras = None


def load_local_env(env_path=".env", override=False):
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and (override or key not in os.environ):
                    os.environ[key] = value
    except Exception:
        pass


APP_DIR = os.path.dirname(os.path.abspath(__file__))
load_local_env(os.path.join(APP_DIR, ".env"), override=True)
load_local_env()

UPLOAD_FOLDER = "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "jfif", "bmp"}
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_PRODUCT_IMAGE_BUCKET = os.getenv("SUPABASE_PRODUCT_IMAGE_BUCKET", "product-images").strip() or "product-images"
SUPABASE_DOCUMENT_BUCKET = os.getenv("SUPABASE_DOCUMENT_BUCKET", "mamas-kitchen-documents").strip() or "mamas-kitchen-documents"
try:
    LOW_STOCK_THRESHOLD = max(int(os.getenv("MAMAS_KITCHEN_LOW_STOCK_THRESHOLD", "5") or 5), 0)
except ValueError:
    LOW_STOCK_THRESHOLD = 5

# Gmail SMTP Configuration
GMAIL_USER = os.getenv("GMAIL_USER", "mamaskitchen@gmail.com")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "gget hsmt vtye vsso")

# Print email config on startup for debugging
print(f"[STARTUP] Email configured with: {GMAIL_USER}")

app = Flask(__name__)
app.secret_key = "secret123"
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 120000


def password_strength(password):
    password = password or ""
    score = 0
    if len(password) >= 8:
        score += 1
    if re.search(r"[a-z]", password) and re.search(r"[A-Z]", password):
        score += 1
    if re.search(r"\d", password):
        score += 1
    if re.search(r"[^A-Za-z0-9]", password):
        score += 1
    if len(password) >= 12:
        score += 1
    if score >= 4:
        return "strong"
    if score >= 3:
        return "medium"
    return "weak"


def validate_strong_password(password):
    if password_strength(password) != "strong":
        return (
            "Password must be strong: use at least 8 characters with uppercase, "
            "lowercase, number, and special character."
        )
    return None


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def verify_password(stored_password, password):
    stored_password = (stored_password or "").strip()
    password = password or ""
    parts = stored_password.split("$")
    if len(parts) == 4 and parts[0] == PASSWORD_HASH_ALGORITHM:
        try:
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = parts[3]
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt,
                iterations,
            ).hex()
            return hmac.compare_digest(digest, expected)
        except Exception:
            return False
    return hmac.compare_digest(stored_password, password)


def is_hashed_password(stored_password):
    return (stored_password or "").startswith(f"{PASSWORD_HASH_ALGORITHM}$")


def infer_supabase_url(db_url):
    if not db_url:
        return ""

    try:
        parsed = urlparse(db_url)
        host = (parsed.hostname or "").strip()
        if host.startswith("db.") and ".supabase.co" in host:
            project_ref = host.split(".")[1]
            if project_ref:
                return f"https://{project_ref}.supabase.co"

        username = (parsed.username or "").strip()
        match = re.search(r"postgres\.([a-z0-9]{20})", username, re.IGNORECASE)
        if match:
            return f"https://{match.group(1)}.supabase.co"
    except Exception:
        return ""

    return ""


if not SUPABASE_URL:
    SUPABASE_URL = infer_supabase_url(SUPABASE_DB_URL)
DB_BACKEND = os.getenv("DB_BACKEND", "postgres").lower()
DB_IS_POSTGRES = True
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '',
    'database': 'mamas_kitchen_d',
    'autocommit': False
}


def translate_sql_for_postgres(sql):
    if not isinstance(sql, str):
        return sql

    normalized = sql.replace("`", "")
    normalized = re.sub(r"\bCURDATE\(\)", "CURRENT_DATE", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bRAND\(\)", "RANDOM()", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bIFNULL\(", "COALESCE(", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"INTERVAL\s+(\d+)\s+DAY", r"INTERVAL '\1 days'", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"DATE_FORMAT\(\s*([^,]+?)\s*,\s*'%Y-%m'\s*\)",
        r"TO_CHAR(\1, 'YYYY-MM')",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"DATE_FORMAT\(\s*([^,]+?)\s*,\s*'%b %Y'\s*\)",
        r"TO_CHAR(\1, 'Mon YYYY')",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"SELECT COUNT\(\*\) AS cnt FROM information_schema\.tables WHERE table_schema = DATABASE\(\) AND table_name = '([^']+)'",
        r"SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = '\1'",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+ON UPDATE CURRENT_TIMESTAMP", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bDATETIME\b", "TIMESTAMP", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bTINYINT\(1\)\b", "SMALLINT", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\bINT\s+AUTO_INCREMENT\s+PRIMARY\s+KEY\b",
        "SERIAL PRIMARY KEY",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\)\s*ENGINE=InnoDB DEFAULT CHARSET=utf8mb4\s*;?", ")", normalized, flags=re.IGNORECASE)

    show_columns = re.fullmatch(r"\s*SHOW\s+COLUMNS\s+FROM\s+([a-zA-Z_][\w]*)\s+LIKE\s+'([^']+)'\s*;?\s*", normalized, flags=re.IGNORECASE)
    if show_columns:
        table_name, column_name = show_columns.groups()
        return (
            "SELECT column_name "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s"
        ), (table_name, column_name)

    show_tables = re.fullmatch(r"\s*SHOW\s+TABLES\s+LIKE\s+'([^']+)'\s*;?\s*", normalized, flags=re.IGNORECASE)
    if show_tables:
        table_name = show_tables.group(1)
        return (
            "SELECT tablename "
            "FROM pg_catalog.pg_tables "
            "WHERE schemaname = current_schema() AND tablename = %s"
        ), (table_name,)

    collapsed = " ".join(normalized.split())
    if collapsed.upper() == "UPDATE ORDER_ITEMS OI JOIN PRODUCTS P ON OI.PRODUCT_ID = P.ID SET OI.SELLER_ID = P.SELLER_ID WHERE OI.SELLER_ID IS NULL":
        normalized = (
            "UPDATE order_items AS oi "
            "SET seller_id = p.seller_id "
            "FROM products AS p "
            "WHERE oi.product_id = p.id AND oi.seller_id IS NULL"
        )

    return normalized


def _normalize_sql_and_params(sql, params=None):
    translated = sql
    translated_params = params
    if DB_IS_POSTGRES:
        translated = translate_sql_for_postgres(sql)
        if isinstance(translated, tuple):
            translated, extra_params = translated
            translated_params = extra_params
    return translated, translated_params


def _should_append_returning_id(sql):
    if not isinstance(sql, str):
        return False
    compact = sql.strip().rstrip(";")
    return compact.upper().startswith("INSERT INTO ") and " RETURNING " not in compact.upper()


class CursorWrapper:
    def __init__(self, cursor):
        self._cur = cursor
        self._buffered_rows = None
        self.lastrowid = None

    def execute(self, sql, params=None):
        translated_sql, translated_params = _normalize_sql_and_params(sql, params)
        if params is not None and translated_params is None:
            translated_params = params

        sql_to_run = translated_sql
        capture_lastrowid = DB_IS_POSTGRES and _should_append_returning_id(sql_to_run)
        if capture_lastrowid:
            sql_to_run = sql_to_run.strip().rstrip(";") + " RETURNING id"

        try:
            result = self._cur.execute(sql_to_run, translated_params)
            if capture_lastrowid:
                row = self._cur.fetchone()
                self._buffered_rows = [row] if row is not None else []
                if row is not None:
                    self.lastrowid = row.get("id") if isinstance(row, dict) else row[0]
            else:
                self._buffered_rows = None
            return result
        except Exception as exc:
            try:
                self._cur.connection.rollback()
            except Exception:
                pass

            if isinstance(exc, DB_PROGRAMMING_ERROR):
                import traceback
                placeholders = sql.count('%s') if isinstance(sql, str) else 'N/A'
                plen = len(params) if isinstance(params, (tuple, list)) else (1 if params is not None else 0)
                app.logger.error('ProgrammingError (cursor wrapper): placeholders=%s params_len=%s SQL=%s translated_sql=%s', placeholders, plen, sql, sql_to_run)
                app.logger.error('params=%s translated_params=%s', params, translated_params)
                app.logger.error('Stack (most recent call last):\n%s', ''.join(traceback.format_stack()))
            raise

    def fetchone(self):
        if self._buffered_rows is not None:
            if not self._buffered_rows:
                self._buffered_rows = None
                return None
            row = self._buffered_rows.pop(0)
            if not self._buffered_rows:
                self._buffered_rows = None
            return row
        return self._cur.fetchone()

    def fetchall(self):
        if self._buffered_rows is not None:
            rows = list(self._buffered_rows)
            self._buffered_rows = None
            return rows
        return self._cur.fetchall()

    def close(self):
        return self._cur.close()

    def __iter__(self):
        return iter(self.fetchall())

    def __getattr__(self, name):
        return getattr(self._cur, name)


class SafePostgresConnection:
    """Connection wrapper for Supabase Postgres with compatibility helpers."""
    def __init__(self, dsn):
        self.dsn = dsn
        self._connect()

    def _connect(self):
        self.conn = psycopg2.connect(self.dsn)
        self.conn.autocommit = False
        with self.conn.cursor() as tz_cur:
            tz_cur.execute("SET SESSION TIME ZONE 'Asia/Manila'")
        self.conn.commit()

    def cursor(self, *args, **kwargs):
        dictionary = kwargs.pop("dictionary", False)
        try:
            if self.conn.closed:
                self._connect()
            else:
                with self.conn.cursor() as ping_cur:
                    ping_cur.execute("SELECT 1")
        except Exception:
            self._connect()

        cursor_factory = psycopg2_extras.RealDictCursor if dictionary else None
        raw_cursor = self.conn.cursor(*args, cursor_factory=cursor_factory, **kwargs)
        return CursorWrapper(raw_cursor)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        return self.conn.close()

    def __getattr__(self, name):
        return getattr(self.conn, name)


if psycopg2 is None:
    raise RuntimeError("Supabase/Postgres mode requires `psycopg2-binary` to be installed.")
if not SUPABASE_DB_URL:
    raise RuntimeError("Set `SUPABASE_DB_URL` or `DATABASE_URL` to your Supabase Postgres connection string.")
db = SafePostgresConnection(SUPABASE_DB_URL)
DB_PROGRAMMING_ERROR = (psycopg2.ProgrammingError,)
DB_RETRY_EXCEPTIONS = (psycopg2.InterfaceError, psycopg2.OperationalError)


# Use a safe connection wrapper so routes don't crash on a dropped connection
# Enable verbose SQL placeholder logging when needed by setting env DEBUG_SQL_MISMATCH=1
DEBUG_SQL_MISMATCH = os.environ.get('DEBUG_SQL_MISMATCH', '0') == '1'


@app.context_processor
def inject_notifications():
    """Inject role-specific notifications into template context.
    - admin: receives notifications where type != 'order_seller' (system-wide)
    - seller: receives notifications tagged with [seller:id]
    - rider: receives notifications tagged with [rider:id]
    - customer: receives notifications tagged with [customer:id]
    """
    role = session.get('role')
    user_id = session.get('user_id')
    notifs = []
    try:
        cursor = db.cursor(dictionary=True)
        if role == 'admin':
            # Admin sees system-wide notifications, not role-targeted inbox items.
            execute_safe(
                cursor,
                """
                SELECT * FROM notifications
                WHERE message IS NULL OR (
                    message NOT LIKE %s
                    AND message NOT LIKE %s
                    AND message NOT LIKE %s
                    AND message NOT LIKE %s
                )
                ORDER BY created_at DESC LIMIT 50
                """,
                ("%[seller:%", "%[customer:%", "%[rider:%", "%[riders:%"),
            )
            notifs = cursor.fetchall() or []
        elif role == 'seller':
            if user_id:
                tag = f"[seller:{user_id}]"
                execute_safe(cursor, "SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
                notifs = cursor.fetchall() or []
        elif role == 'rider':
            if user_id:
                tag = f"[rider:{user_id}]"
                execute_safe(
                    cursor,
                    "SELECT * FROM notifications WHERE message LIKE %s OR message LIKE %s OR message LIKE %s ORDER BY created_at DESC LIMIT 50",
                    (f"%{tag}%", "%[rider:all]%", "%[riders:all]%"),
                )
                notifs = cursor.fetchall() or []
        else:
            # default: customer / user / buyer
            if user_id:
                tag = f"[customer:{user_id}]"
                execute_safe(cursor, "SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
                notifs = cursor.fetchall() or []
        cursor.close()
    except Exception:
        notifs = []
    return dict(notifications=notifs, low_stock_threshold=LOW_STOCK_THRESHOLD)


def _notification_message(text):
    return (text or "").strip()


def create_notification(notif_type, message, target_url=None, cursor=None, commit=True):
    """Create a notification without letting notification failures break the user action."""
    message = _notification_message(message)
    if not message:
        return False

    own_cursor = cursor is None
    cur = cursor or db.cursor()
    try:
        execute_safe(
            cur,
            "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
            (notif_type, message, target_url),
        )
        if own_cursor and commit:
            db.commit()
        return True
    except Exception as exc:
        if own_cursor:
            try:
                db.rollback()
            except Exception:
                pass
        try:
            app.logger.warning("Failed to create notification: %s", exc)
        except Exception:
            pass
        return False
    finally:
        if own_cursor:
            try:
                cur.close()
            except Exception:
                pass


def display_status(status):
    return (status or "").replace("_", " ").strip().title() or "Updated"


def notify_admin_user_registration(role, fullname, email, cursor=None):
    role_label = (role or "user").strip().lower()
    return create_notification(
        "user_registration",
        f"New {role_label} registered: {fullname} ({email})",
        "/admin",
        cursor=cursor,
        commit=False if cursor else True,
    )


def notify_seller_product_status(seller_id, product_id, product_name, status_label, cursor=None):
    if not seller_id:
        return False
    return create_notification(
        "product_seller",
        f"[seller:{seller_id}] Your product \"{product_name}\" has been {status_label}.",
        f"/seller?product_id={product_id}" if product_id else "/seller#products",
        cursor=cursor,
        commit=False if cursor else True,
    )


def notify_seller_low_stock(seller_id, product_id, product_name, remaining_stock, threshold, cursor=None):
    if not seller_id:
        return False
    return create_notification(
        "low_stock_seller",
        (
            f"[seller:{seller_id}] Low stock alert: \"{product_name}\" has "
            f"{remaining_stock} item(s) remaining. Restock threshold: {threshold}."
        ),
        f"/seller?product_id={product_id}#products",
        cursor=cursor,
        commit=False if cursor else True,
    )


def decrement_ordered_stock(item, cursor):
    """Atomically reduce stock for one checkout item and alert on threshold crossing."""
    quantity = int(item.get("quantity") or 0)
    if quantity <= 0:
        raise ValueError("Invalid order quantity.")

    product_id = item.get("product_id")
    variant_id = item.get("variant_id")

    if variant_id:
        cursor.execute(
            """
            SELECT pv.stock AS variant_stock,
                   p.id AS product_id,
                   p.name,
                   p.seller_id,
                   COALESCE(variant_totals.total_stock, pv.stock, 0) AS product_stock
            FROM product_variants pv
            JOIN products p ON p.id = pv.product_id
            LEFT JOIN (
                SELECT product_id, SUM(stock) AS total_stock
                FROM product_variants
                GROUP BY product_id
            ) AS variant_totals ON variant_totals.product_id = p.id
            WHERE pv.id = %s
            FOR UPDATE OF pv, p
            """,
            (variant_id,),
        )
        row = cursor.fetchone()
        if not row or int(row.get("variant_stock") or 0) < quantity:
            raise ValueError("Insufficient stock. A variant is no longer available in the quantity you selected.")

        previous_product_stock = int(row.get("product_stock") or 0)
        threshold = LOW_STOCK_THRESHOLD
        cursor.execute(
            "UPDATE product_variants SET stock = stock - %s WHERE id = %s",
            (quantity, variant_id),
        )
        cursor.execute(
            "SELECT COALESCE(SUM(stock), 0) AS real_stock FROM product_variants WHERE product_id = %s",
            (row["product_id"],),
        )
        stock_row = cursor.fetchone() or {}
        new_product_stock = int(stock_row.get("real_stock") or 0)
        cursor.execute(
            "UPDATE products SET stock = %s WHERE id = %s",
            (new_product_stock, row["product_id"]),
        )
        if previous_product_stock > threshold and new_product_stock <= threshold:
            notify_seller_low_stock(
                row.get("seller_id"),
                row.get("product_id"),
                row.get("name"),
                new_product_stock,
                threshold,
                cursor=cursor,
            )
        return

    cursor.execute(
        """
        SELECT id, name, stock, seller_id
        FROM products
        WHERE id = %s
        FOR UPDATE
        """,
        (product_id,),
    )
    row = cursor.fetchone()
    if not row or int(row.get("stock") or 0) < quantity:
        raise ValueError("Insufficient stock. A product is no longer available in the quantity you selected.")

    previous_stock = int(row.get("stock") or 0)
    threshold = LOW_STOCK_THRESHOLD
    new_stock = previous_stock - quantity
    cursor.execute(
        "UPDATE products SET stock = stock - %s WHERE id = %s",
        (quantity, product_id),
    )
    if previous_stock > threshold and new_stock <= threshold:
        notify_seller_low_stock(
            row.get("seller_id"),
            row.get("id"),
            row.get("name"),
            new_stock,
            threshold,
            cursor=cursor,
        )


def notify_rider_order_completed(order_id, cursor=None):
    """Notify the assigned rider when the customer confirms delivery as complete."""
    own_cursor = cursor is None
    cur = cursor or db.cursor(dictionary=True)
    try:
        execute_safe(cur, "SELECT rider_id FROM order_riders WHERE order_id=%s LIMIT 1", (order_id,))
        row = cur.fetchone()
        rider_id = row.get("rider_id") if isinstance(row, dict) else (row[0] if row else None)
        if not rider_id:
            return False
        return create_notification(
            "order_rider",
            f"[rider:{rider_id}] Customer confirmed order #{order_id} is complete.",
            f"/rider?order_id={order_id}",
            cursor=cur,
            commit=False if cursor else True,
        )
    except Exception as exc:
        try:
            app.logger.warning("Failed to notify rider about completed order %s: %s", order_id, exc)
        except Exception:
            pass
        return False
    finally:
        if own_cursor:
            try:
                cur.close()
            except Exception:
                pass


def notify_matching_riders_delivery_available(order_id, vehicle_type=None, return_pickup=False, cursor=None):
    """Notify approved riders whose vehicle can handle the available delivery."""
    own_cursor = cursor is None
    cur = cursor or db.cursor(dictionary=True)
    try:
        if vehicle_type:
            execute_safe(
                cur,
                """
                SELECT r.user_id
                FROM riders r
                JOIN users u ON u.id = r.user_id
                WHERE u.status = %s AND LOWER(r.vehicle_type) = LOWER(%s)
                """,
                ("approved", vehicle_type),
            )
        else:
            execute_safe(
                cur,
                """
                SELECT r.user_id
                FROM riders r
                JOIN users u ON u.id = r.user_id
                WHERE u.status = %s
                """,
                ("approved",),
            )
        riders = cur.fetchall() or []
        sent = 0
        label = "return pickup" if return_pickup else "delivery"
        for rider in riders:
            rider_id = rider.get("user_id") if isinstance(rider, dict) else rider[0]
            if not rider_id:
                continue
            message = f"[rider:{rider_id}] New {label} available: Order #{order_id}."
            create_notification(
                "order_rider",
                message,
                f"/rider?order_id={order_id}",
                cursor=cur,
                commit=False,
            )
            sent += 1
        if own_cursor:
            db.commit()
        return sent
    except Exception as exc:
        if own_cursor:
            try:
                db.rollback()
            except Exception:
                pass
        try:
            app.logger.warning("Failed to notify riders about order %s: %s", order_id, exc)
        except Exception:
            pass
        return 0
    finally:
        if own_cursor:
            try:
                cur.close()
            except Exception:
                pass


# Ensure optional schema additions exist for rejection logging
def ensure_order_schema_extensions():
    if DB_IS_POSTGRES:
        app.logger.info("Skipping legacy MySQL schema bootstrap because Supabase/Postgres is enabled.")
        return
    c = db.cursor()
    try:
        # Add columns for cancellation rejection details if missing
        c.execute("SHOW COLUMNS FROM orders LIKE 'cancel_rejection_reason'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN cancel_rejection_reason VARCHAR(255) NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'cancel_rejection_notes'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN cancel_rejection_notes TEXT NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'cancel_rejected_at'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN cancel_rejected_at DATETIME NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'cancel_rejected_by'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN cancel_rejected_by INT NULL")
        # If users table exists, try to add a FK for cancel_rejected_by as well
        try:
            c.execute("SHOW TABLES LIKE 'users'")
            if c.fetchone():
                # Fix any orphaned references first
                c.execute('''
                    SELECT COUNT(*) AS cnt
                    FROM orders o
                    LEFT JOIN users u ON o.cancel_rejected_by = u.id
                    WHERE o.cancel_rejected_by IS NOT NULL AND u.id IS NULL
                ''')
                orphan = c.fetchone()
                if orphan and orphan[0] > 0:
                    c.execute('''
                        UPDATE orders o
                        LEFT JOIN users u ON o.cancel_rejected_by = u.id
                        SET o.cancel_rejected_by = NULL
                        WHERE o.cancel_rejected_by IS NOT NULL AND u.id IS NULL
                    ''')
                try:
                    c.execute("ALTER TABLE orders ADD CONSTRAINT fk_orders_cancel_rejected_by FOREIGN KEY (cancel_rejected_by) REFERENCES users(id) ON DELETE SET NULL")
                except Exception:
                    pass


                # Conversation helper functions are declared at module level - see below.

        except Exception:
            # If users table doesn't exist or error occurs, skip FK creation for now
            pass
        db.commit()
        # Ensure refund columns exist
        c.execute("SHOW COLUMNS FROM orders LIKE 'refund_reason'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN refund_reason VARCHAR(255) NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'refund_requested_at'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN refund_requested_at DATETIME NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'refund_rejection_reason'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN refund_rejection_reason VARCHAR(255) NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'refund_rejection_notes'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN refund_rejection_notes TEXT NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'refund_rejected_at'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN refund_rejected_at DATETIME NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'refund_rejected_by'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN refund_rejected_by INT NULL")
        c.execute("SHOW COLUMNS FROM orders LIKE 'refunded_at'")
        if not c.fetchone():
            c.execute("ALTER TABLE orders ADD COLUMN refunded_at DATETIME NULL")
        # Try adding FK for refund_rejected_by
        try:
            c.execute("SHOW TABLES LIKE 'users'")
            if c.fetchone():
                # Clean up orphaned references
                c.execute('''
                    SELECT COUNT(*) FROM orders o
                    LEFT JOIN users u ON o.refund_rejected_by = u.id
                    WHERE o.refund_rejected_by IS NOT NULL AND u.id IS NULL
                ''')
                orphan = c.fetchone()
                if orphan and orphan[0] > 0:
                    c.execute('''
                        UPDATE orders o
                        LEFT JOIN users u ON o.refund_rejected_by = u.id
                        SET o.refund_rejected_by = NULL
                        WHERE o.refund_rejected_by IS NOT NULL AND u.id IS NULL
                    ''')
                try:
                    c.execute("ALTER TABLE orders ADD CONSTRAINT fk_orders_refund_rejected_by FOREIGN KEY (refund_rejected_by) REFERENCES users(id) ON DELETE SET NULL")
                except Exception:
                    pass
        except Exception:
            pass
        # Try adding FK for refund_complied_by
        try:
            c.execute("SHOW TABLES LIKE 'users'")
            if c.fetchone():
                # Clean up orphaned references to users in refund_complied_by
                c.execute('''
                    SELECT COUNT(*) FROM orders o
                    LEFT JOIN users u ON o.refund_complied_by = u.id
                    WHERE o.refund_complied_by IS NOT NULL AND u.id IS NULL
                ''')
                orphan = c.fetchone()
                if orphan and orphan[0] > 0:
                    c.execute('''
                        UPDATE orders o
                        LEFT JOIN users u ON o.refund_complied_by = u.id
                        SET o.refund_complied_by = NULL
                        WHERE o.refund_complied_by IS NOT NULL AND u.id IS NULL
                    ''')
                try:
                    c.execute("ALTER TABLE orders ADD CONSTRAINT fk_orders_refund_complied_by FOREIGN KEY (refund_complied_by) REFERENCES users(id) ON DELETE SET NULL")
                except Exception:
                    pass
        except Exception:
            pass
        except Exception:
            pass
        # If users table exists, try to ensure performed_by references users(id)
        try:
            c.execute("SHOW TABLES LIKE 'users'")
            if c.fetchone():
                # Clean up any orphaned performed_by values (pointing to non-existent users)
                c.execute('''
                    SELECT COUNT(*) AS cnt
                    FROM order_action_logs l
                    LEFT JOIN users u ON l.performed_by = u.id
                    WHERE l.performed_by IS NOT NULL AND u.id IS NULL
                ''')
                orphan = c.fetchone()
                if orphan and orphan[0] > 0:
                    c.execute('''
                        UPDATE order_action_logs l
                        LEFT JOIN users u ON l.performed_by = u.id
                        SET l.performed_by = NULL
                        WHERE l.performed_by IS NOT NULL AND u.id IS NULL
                    ''')
                # Add the FK constraint, if not already present
                try:
                    c.execute("ALTER TABLE order_action_logs ADD CONSTRAINT fk_order_action_logs_performed_by FOREIGN KEY (performed_by) REFERENCES users(id) ON DELETE SET NULL")
                except Exception:
                    # If the constraint already exists or other error, ignore it; the table is still usable
                    pass
        except Exception:
            # If users table isn't present or any other error occurs, skip adding FK
            pass
        # NOTE: Don't insert any action log here; this helper only adjusts schema. Action logs are created
        # during runtime operations (approvals/rejections), so avoid referencing variables that do not exist.
        # No log insert here (schema check only)
        # Insert action log for audit (approval)
        # No log insert here
        # No inserts here - table creation only
        
        # No inserts here - table creation only
        # No inserts here - table creation only

        # Create a simple order_action_logs table for audit events
        c.execute('''
            CREATE TABLE IF NOT EXISTS order_action_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id INT NOT NULL,
                action_type VARCHAR(50) NOT NULL,
                performed_by INT NULL,
                performed_role VARCHAR(20) NULL,
                reason VARCHAR(255) NULL,
                notes TEXT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        ''')
        
        # Create income tracking table
        c.execute('''
            CREATE TABLE IF NOT EXISTS income (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id INT NOT NULL,
                user_id INT NOT NULL,
                role VARCHAR(20) NOT NULL,
                amount DECIMAL(10, 2) NOT NULL,
                description VARCHAR(255) NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        ''')
        # Create earnings table (separate, unify reporting across roles)
        c.execute('''
            CREATE TABLE IF NOT EXISTS earnings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id INT NOT NULL,
                user_id INT NOT NULL,
                role VARCHAR(20) NOT NULL,
                amount DECIMAL(10, 2) NOT NULL,
                description VARCHAR(255) NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        ''')
        # Migrate existing income records into the earnings table if the table was just added
        try:
            mig_cur = db.cursor(dictionary=True)
            mig_cur.execute("SELECT id, order_id, user_id, role, amount, description, created_at FROM income")
            income_rows = mig_cur.fetchall() or []
            for r in income_rows:
                # check if exists in earnings for same order_id,user_id,role
                chk = db.cursor()
                chk.execute("SELECT COUNT(*) FROM earnings WHERE order_id=%s AND user_id=%s AND role=%s", (r.get('order_id'), r.get('user_id'), r.get('role')))
                if chk.fetchone()[0] == 0:
                    icur = db.cursor()
                    icur.execute("INSERT INTO earnings (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, %s)", (r.get('order_id'), r.get('user_id'), r.get('role'), r.get('amount'), r.get('description'), r.get('created_at')))
                    db.commit()
                    icur.close()
                chk.close()
            mig_cur.close()
        except Exception:
            try:
                mig_cur.close()
            except Exception:
                pass
        # Create a return_requests table to track return flows separate from orders
        c.execute('''
            CREATE TABLE IF NOT EXISTS return_requests (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id INT NOT NULL,
                requested_by INT NULL,
                requested_by_role VARCHAR(20) NULL,
                reason VARCHAR(255) NULL,
                notes TEXT NULL,
                status VARCHAR(50) DEFAULT 'requested', -- requested, pickup_requested, assigned, in_transit, delivered, seller_received, refunded, cancelled
                pickup_requested_at DATETIME NULL,
                assigned_at DATETIME NULL,
                pickup_rider_id INT NULL,
                delivered_at DATETIME NULL,
                seller_finalized_at DATETIME NULL,
                refund_processed_at DATETIME NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        ''')
        # Try to add FK for rider and requested_by to users if users exists
        try:
            c.execute("SHOW TABLES LIKE 'users'")
            if c.fetchone():
                try:
                    c.execute("ALTER TABLE return_requests ADD CONSTRAINT fk_return_requests_requested_by FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE SET NULL")
                except Exception:
                    pass
                try:
                    c.execute("ALTER TABLE return_requests ADD CONSTRAINT fk_return_requests_pickup_rider FOREIGN KEY (pickup_rider_id) REFERENCES users(id) ON DELETE SET NULL")
                except Exception:
                    pass
        except Exception:
            pass
        db.commit()

        # Ensure order_items has seller_id to snapshot product owner when order was placed
        try:
            c.execute("SHOW COLUMNS FROM order_items LIKE 'seller_id'")
            if not c.fetchone():
                c.execute("ALTER TABLE order_items ADD COLUMN seller_id INT NULL")
                # backfill seller_id from products.seller_id for existing rows
                c.execute("UPDATE order_items oi JOIN products p ON oi.product_id = p.id SET oi.seller_id = p.seller_id WHERE oi.seller_id IS NULL")
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        db.commit()

        # Ensure notifications table has a target_url column; create table if missing
        try:
            c.execute("SHOW TABLES LIKE 'notifications'")
            if not c.fetchone():
                # Create a minimal notifications table if missing
                c.execute('''
                    CREATE TABLE IF NOT EXISTS notifications (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        `type` VARCHAR(50) DEFAULT NULL,
                        `message` TEXT NULL,
                        `target_url` VARCHAR(255) NULL,
                        `is_read` TINYINT(1) DEFAULT 0,
                        `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                ''')
            else:
                # If notifications exists, ensure target_url column is present
                c.execute("SHOW COLUMNS FROM notifications LIKE 'target_url'")
                if not c.fetchone():
                    c.execute("ALTER TABLE notifications ADD COLUMN target_url VARCHAR(255) NULL")
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

                # --- Ensure messaging tables exist ---
                # conversations: hold conversation between seller and customer, optionally per product
                c.execute("SHOW TABLES LIKE 'conversations'")
                if not c.fetchone():
                    c.execute('''
                        CREATE TABLE conversations (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            seller_id INT NULL,
                            customer_id INT NULL,
                            product_id INT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            last_message TEXT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    ''')
                else:
                    # If conversations table exists, ensure product_id column exists (older schemas may have missed it)
                    try:
                        c.execute("SHOW COLUMNS FROM conversations LIKE 'product_id'")
                        if not c.fetchone():
                            try:
                                c.execute("ALTER TABLE conversations ADD COLUMN product_id INT NULL")
                            except Exception:
                                pass
                    except Exception:
                        pass
                # messages: each message belongs to a conversation
                c.execute("SHOW TABLES LIKE 'messages'")
                if not c.fetchone():
                    c.execute('''
                        CREATE TABLE messages (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            conversation_id INT NOT NULL,
                            sender_id INT NOT NULL,
                            sender_role VARCHAR(20) NOT NULL,
                            body TEXT NOT NULL,
                            is_read TINYINT(1) DEFAULT 0,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    ''')
                # If users exists, attempt to add FKs from conversations to users
                try:
                    c.execute("SHOW TABLES LIKE 'users'")
                    if c.fetchone():
                        try:
                            c.execute("ALTER TABLE conversations ADD CONSTRAINT fk_conversations_seller FOREIGN KEY (seller_id) REFERENCES users(id) ON DELETE SET NULL")
                        except Exception:
                            pass
                        try:
                            c.execute("ALTER TABLE conversations ADD CONSTRAINT fk_conversations_customer FOREIGN KEY (customer_id) REFERENCES users(id) ON DELETE SET NULL")
                        except Exception:
                            pass
                        try:
                            c.execute("ALTER TABLE messages ADD CONSTRAINT fk_messages_sender FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE SET NULL")
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            c.close()
        except Exception:
            pass


ensure_order_schema_extensions()


def normalize_order_processing_flow():
    """Migrate legacy pending orders into the current processing-first flow."""
    cur = None
    try:
        cur = db.cursor()
        execute_safe(cur, "UPDATE orders SET status=%s WHERE status=%s", ("processing", "pending"))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            cur.close()
        except Exception:
            pass


normalize_order_processing_flow()
# ----------------------------- Conversation Helper Functions (module-level) -----------------------------
def get_conversation(seller_id, customer_id, product_id=None):
    """Return a conversation dict if exists between seller and customer optionally filtering by product_id.
    If product_id is None, tries to find any conversation between them.
    """
    cursor = db.cursor(dictionary=True)
    try:
        if product_id is not None:
            try:
                cursor.execute("SELECT * FROM conversations WHERE seller_id=%s AND customer_id=%s AND product_id=%s", (seller_id, customer_id, product_id))
                row = cursor.fetchone()
                if row:
                    return row
            except Exception:
                # possible schema missing product_id; ignore and continue
                pass

        cursor.execute("SELECT * FROM conversations WHERE seller_id=%s AND customer_id=%s ORDER BY updated_at DESC LIMIT 1", (seller_id, customer_id))
        return cursor.fetchone()
    except Exception:
        return None
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def create_conversation(seller_id, customer_id, product_id=None):
    """Create conversation between seller and customer. Returns created conversation row or None on error."""
    cursor = db.cursor(dictionary=True)
    try:
        if product_id is not None:
            try:
                cursor.execute("INSERT INTO conversations (seller_id, customer_id, product_id, created_at, updated_at) VALUES (%s, %s, %s, NOW(), NOW())", (seller_id, customer_id, product_id))
            except Exception:
                cursor.execute("INSERT INTO conversations (seller_id, customer_id, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())", (seller_id, customer_id))
        else:
            cursor.execute("INSERT INTO conversations (seller_id, customer_id, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())", (seller_id, customer_id))
        db.commit()
        cursor.execute("SELECT * FROM conversations WHERE seller_id=%s AND customer_id=%s ORDER BY updated_at DESC LIMIT 1", (seller_id, customer_id))
        return cursor.fetchone()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def get_or_create_conversation(seller_id, customer_id, product_id=None):
    """Try to get an existing conversation, otherwise create one.
    Returns conversation row if found/created, else None.
    """
    convo = get_conversation(seller_id, customer_id, product_id)
    if convo:
        return convo
    return create_conversation(seller_id, customer_id, product_id)


def fetch_user_conversations(user_id):
    """Return a list of conversation dictionaries for a user (as seller or customer) including 'other' name and unread counts."""
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM conversations WHERE seller_id=%s OR customer_id=%s ORDER BY updated_at DESC", (user_id, user_id))
        convos = cursor.fetchall() or []
        out = []
        for c in convos:
            cd = conversation_to_dict(c, user_id)
            if cd:
                out.append(cd)
        return out
    except Exception:
        return []
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def fetch_conversation_messages(convo_id):
    """Return messages for conversation_id as dictionaries, without modifying read status."""
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT m.*, u.fullname as sender_name FROM messages m LEFT JOIN users u ON u.id = m.sender_id WHERE m.conversation_id=%s ORDER BY m.created_at ASC", (convo_id,))
        messages = cursor.fetchall() or []
        out = []
        for m in messages:
            out.append({
                'id': m.get('id'),
                'conversation_id': m.get('conversation_id'),
                'sender_id': m.get('sender_id'),
                'sender_role': m.get('sender_role'),
                'body': m.get('body'),
                'is_read': bool(m.get('is_read')),
                'created_at': m.get('created_at').isoformat() if m.get('created_at') else None,
                'sender_name': m.get('sender_name')
            })
        return out
    except Exception:
        return []
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def conversation_to_dict(convo_row, user_id):
    """Convert a conversation DB row into frontend-friendly dictionary.
    Fetches seller's shop_name and other party's name, computes unread count for user.
    """
    if not convo_row:
        return None
    cursor = db.cursor(dictionary=True)
    try:
        convo_id = convo_row.get('id')
        seller_id = convo_row.get('seller_id')
        # If seller_id is missing but product_id is present, attempt to resolve seller
        if not seller_id and convo_row.get('product_id'):
            try:
                cursor.execute("SELECT seller_id FROM products WHERE id=%s", (convo_row.get('product_id'),))
                prow = cursor.fetchone()
                if prow and prow.get('seller_id'):
                    seller_id = prow.get('seller_id')
            except Exception:
                pass
        customer_id = convo_row.get('customer_id')
        # fetch shop_name
        shop_name = None
        if seller_id:
            cursor.execute("SELECT u.id, u.fullname, s.business_name FROM users u LEFT JOIN sellers s ON s.user_id=u.id WHERE u.id=%s", (seller_id,))
            srow = cursor.fetchone()
            if srow:
                shop_name = srow.get('business_name') or srow.get('fullname')
        # other party name
        other_id = seller_id if customer_id == user_id else customer_id
        other_name = None
        if other_id:
            cursor.execute("SELECT fullname FROM users WHERE id=%s", (other_id,))
            orow = cursor.fetchone()
            if orow:
                other_name = orow.get('fullname')
        # unread count
        cursor.execute("SELECT COUNT(*) AS cnt FROM messages WHERE conversation_id=%s AND sender_id!=%s AND is_read=0", (convo_id, user_id))
        uc = cursor.fetchone()
        unread = int(uc.get('cnt') or 0) if uc else 0
        # fetch product name if exists
        product_name = None
        if convo_row.get('product_id'):
            try:
                cursor.execute("SELECT name FROM products WHERE id=%s", (convo_row.get('product_id'),))
                prow = cursor.fetchone()
                if prow:
                    product_name = prow.get('name')
            except Exception:
                pass
        return {
            'id': convo_id,
            'seller_id': seller_id,
            'customer_id': customer_id,
            'other_id': other_id,
            'other_name': other_name,
            'other_user_name': other_name,
            'shop_name': shop_name,
            'product_name': product_name,
            'last_message': convo_row.get('last_message'),
            'last_message_body': convo_row.get('last_message'),
            'updated_at': convo_row.get('updated_at').isoformat() if convo_row.get('updated_at') else None,
            'product_id': convo_row.get('product_id') if 'product_id' in convo_row else None,
            'unread_count': unread
        }
    except Exception:
        return None
    finally:
        try:
            cursor.close()
        except Exception:
            pass




# NOTE: The `delivery_proofs` table is NOT auto-created by the app.
# Create it manually in your database before testing uploads. Example SQL:
#
#   CREATE TABLE IF NOT EXISTS delivery_proofs (
#     id INT AUTO_INCREMENT PRIMARY KEY,
#     order_id INT NOT NULL,
#     file_path VARCHAR(255) NOT NULL,
#     uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
#     CONSTRAINT fk_delivery_proofs_order FOREIGN KEY (order_id) REFERENCES orders(id)
#       ON DELETE CASCADE ON UPDATE CASCADE
#   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
#
# Run this as a DBA or via your preferred database migration tooling.
 
# Backwards-compatible cursor proxy: some code uses a global `cursor` variable.
class CursorProxy:
    def __init__(self, dictionary=True):
        self.dictionary = dictionary
        self._cur = None

    def _ensure(self):
        if self._cur is None:
            self._cur = db.cursor(dictionary=self.dictionary)

    def execute(self, *args, **kwargs):
        self._ensure()
        try:
            return self._cur.execute(*args, **kwargs)
        except DB_RETRY_EXCEPTIONS as e:
            # Lost connection or similar transient network error. Reset cursor and retry once.
            try:
                if self._cur:
                    try:
                        self._cur.close()
                    except Exception:
                        pass
                self._cur = None
                # ensure will attempt to get a fresh cursor (and SafeMySQLConnection will reconnect)
                self._ensure()
                return self._cur.execute(*args, **kwargs)
            except Exception:
                # re-raise original exception to preserve traceback
                raise e

    def fetchone(self):
        self._ensure()
        return self._cur.fetchone()

    def fetchall(self):
        self._ensure()
        return self._cur.fetchall()

    def close(self):
        try:
            if self._cur:
                self._cur.close()
        finally:
            self._cur = None

    def __getattr__(self, name):
        self._ensure()
        return getattr(self._cur, name)

# global cursor for older code paths
cursor = CursorProxy(dictionary=True)

VEHICLE_MAX_WEIGHT = {
    'motorcycle': 10,
    'sedan': 30,
    'suv': 50,
    'van': 100,
    'light_truck': 200
}

VEHICLE_FEES = {
    'motorcycle': 50,
    'sedan': 100,
    'suv': 120,
    'van': 200,
    'light_truck': 250
}

# Assign vehicle based on total weight
def assign_vehicle(total_weight):
    for vehicle, max_w in VEHICLE_MAX_WEIGHT.items():
        if total_weight <= max_w:
            return vehicle
    return 'light_truck'

# Shipping calculation based on admin levels + weight
def calculate_shipping(customer_addr, seller_addr, total_weight):
    # Area-based fee
    if seller_addr['city_name'].lower() == customer_addr['city_name'].lower():
        area_fee = 40
    elif seller_addr['province_name'].lower() == customer_addr['province_name'].lower():
        area_fee = 55
    elif seller_addr['region_name'].lower() == customer_addr['region_name'].lower():
        area_fee = 80
    else:
        area_fee = 120

    # Vehicle-based fee
    vehicle = assign_vehicle(total_weight)
    vehicle_fee = VEHICLE_FEES.get(vehicle, 50)

    return round(area_fee + vehicle_fee, 2)

# Optional: get structured address from free text (via Nominatim)
def get_address_details(address):
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address,
        "format": "json",
        "addressdetails": 1,
        "limit": 1
    }
    headers = {"User-Agent": "YourApp"}
    resp = requests.get(url, params=params, headers=headers)
    data = resp.json()
    if not data:
        return {}
    addr = data[0].get("address", {})
    return {
        "region_name": addr.get("region", ""),
        "province_name": addr.get("state", ""),
        "city_name": addr.get("city") or addr.get("town") or addr.get("municipality", ""),
        "barangay_name": addr.get("suburb") or addr.get("village") or addr.get("neighbourhood", "")
    }

def _normalized_supabase_url():
    return SUPABASE_URL[:-1] if SUPABASE_URL.endswith("/") else SUPABASE_URL


def _jwt_role(token):
    if not token or token.count(".") < 2:
        return ""

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
        return str(data.get("role") or "").strip()
    except Exception:
        return ""


def _storage_enabled():
    return bool(
        _normalized_supabase_url()
        and SUPABASE_SERVICE_ROLE_KEY
        and _jwt_role(SUPABASE_SERVICE_ROLE_KEY) == "service_role"
    )


def _storage_headers(content_type):
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": content_type or "application/octet-stream",
        "x-upsert": "true",
    }


def _public_storage_url(bucket, object_path):
    base = _normalized_supabase_url()
    object_path = str(object_path).replace("\\", "/").lstrip("/")
    return f"{base}/storage/v1/object/public/{bucket}/{object_path}"


def _extract_storage_object(path):
    raw = str(path or "").strip()
    if not raw:
        return None, None

    normalized = raw.replace("\\", "/").lstrip("/")
    base = _normalized_supabase_url()

    if normalized.startswith("storage://"):
        remainder = normalized[len("storage://"):]
        parts = remainder.split("/", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[0], parts[1].lstrip("/")

    if raw.startswith("http://") or raw.startswith("https://"):
        if base and raw.startswith(base):
            normalized = raw[len(base):].lstrip("/")
        else:
            return None, None

    for prefix in ("storage/v1/object/public/", "storage/v1/object/sign/"):
        if normalized.startswith(prefix):
            remainder = normalized[len(prefix):]
            parts = remainder.split("/", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                object_path = parts[1].split("?", 1)[0].lstrip("/")
                return parts[0], object_path

    if normalized.startswith("static/"):
        normalized = normalized[len("static/"):]

    if normalized.startswith("uploads/"):
        bucket, object_path = _storage_target_from_path(normalized)
        return bucket, object_path

    return None, None


def _signed_storage_url(bucket, object_path, expires_in=604800):
    if not _storage_enabled() or not bucket or not object_path:
        return _public_storage_url(bucket, object_path) if bucket and object_path else ""

    object_path = str(object_path).replace("\\", "/").lstrip("/")
    sign_url = f"{_normalized_supabase_url()}/storage/v1/object/sign/{bucket}/{object_path}"
    response = requests.post(
        sign_url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        json={"expiresIn": int(expires_in)},
        timeout=15,
    )
    if response.status_code < 400:
        try:
            payload = response.json()
            signed_path = payload.get("signedURL") or payload.get("signedUrl")
            if signed_path:
                signed_path = str(signed_path)
                if signed_path.startswith("http://") or signed_path.startswith("https://"):
                    return signed_path
                return f"{_normalized_supabase_url()}{signed_path if signed_path.startswith('/') else '/' + signed_path}"
        except Exception:
            pass

    return _public_storage_url(bucket, object_path)


def _storage_reference(bucket, object_path):
    object_path = str(object_path).replace("\\", "/").lstrip("/")
    return f"storage://{bucket}/{object_path}"


def _storage_target_from_path(path_hint):
    normalized = str(path_hint or "").replace("\\", "/").strip("/")
    if "uploads/" in normalized:
        normalized = normalized.split("uploads/", 1)[1]
    if normalized.startswith("static/"):
        normalized = normalized[len("static/"):]
    if normalized.startswith("uploads/"):
        normalized = normalized[len("uploads/"):]

    if normalized == "products" or normalized.startswith("products/"):
        return SUPABASE_PRODUCT_IMAGE_BUCKET, normalized
    if normalized == "profile_pics" or normalized.startswith("profile_pics/"):
        return SUPABASE_PRODUCT_IMAGE_BUCKET, normalized

    if not normalized:
        normalized = "misc"
    return SUPABASE_DOCUMENT_BUCKET, normalized


def media_url(path, fallback_static=None):
    if not path:
        return url_for("static", filename=fallback_static) if fallback_static else ""

    bucket, object_path = _extract_storage_object(path)
    if bucket and object_path:
        return url_for("storage_media", bucket=bucket, object_path=object_path)

    raw = str(path).strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    normalized = raw.replace("\\", "/").lstrip("/")

    if normalized.startswith("static/"):
        normalized = normalized[len("static/"):]

    return url_for("static", filename=normalized)


@app.context_processor
def inject_media_helpers():
    return {"media_url": media_url}


@app.route("/media/<bucket>/<path:object_path>")
def storage_media(bucket, object_path):
    if not _storage_enabled():
        return "", 404

    object_path = str(object_path).replace("\\", "/").lstrip("/")
    fetch_url = f"{_normalized_supabase_url()}/storage/v1/object/{bucket}/{object_path}"
    response = requests.get(
        fetch_url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=30,
        stream=True,
    )
    if response.status_code >= 400:
        return "", 404

    headers = {}
    content_type = response.headers.get("Content-Type")
    cache_control = response.headers.get("Cache-Control")
    if content_type:
        headers["Content-Type"] = content_type
    if cache_control:
        headers["Cache-Control"] = cache_control
    return Response(response.content, headers=headers)


def save_file(file, folder):
    if file and file.filename != "":
        filename = secure_filename(file.filename)
        unique_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}_{filename}"
        if not _storage_enabled():
            raise RuntimeError(
                "Supabase Storage is not configured for server uploads. "
                "Set SUPABASE_URL and a valid SUPABASE_SERVICE_ROLE_KEY in Mama's Kitchen/.env."
            )

        content_type = getattr(file, "mimetype", None) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        bucket, folder_key = _storage_target_from_path(folder)
        object_path = f"{folder_key.rstrip('/')}/{unique_name}" if folder_key else unique_name
        object_path = object_path.lstrip("/")

        upload_url = f"{_normalized_supabase_url()}/storage/v1/object/{bucket}/{object_path}"
        file.stream.seek(0)
        response = requests.post(
            upload_url,
            headers=_storage_headers(content_type),
            data=file.read(),
            timeout=30,
        )
        if response.status_code >= 400:
            if response.status_code == 400 and "Bucket not found" in response.text:
                raise RuntimeError(
                    f"Supabase Storage bucket '{bucket}' was not found. "
                    f"Create it in Supabase Storage or update the "
                    f"{'SUPABASE_PRODUCT_IMAGE_BUCKET' if bucket == SUPABASE_PRODUCT_IMAGE_BUCKET else 'SUPABASE_DOCUMENT_BUCKET'} "
                    f"environment variable."
                )
            raise RuntimeError(
                f"Supabase Storage upload failed ({response.status_code}): {response.text}"
            )
        return _storage_reference(bucket, object_path)
    return None

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def has_uploaded_file(file_obj):
    return bool(file_obj and getattr(file_obj, "filename", None) and str(file_obj.filename).strip())


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def fetch_scalar(sql, params):
    cur = db.cursor()
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()

def send_approval_email(email, fullname, role, status):
    """
    Send approval/rejection email to user.
    status: 'approved' or 'rejected'
    
    If Gmail SMTP fails, will print email content to console for testing.
    """
    try:
        print(f"[EMAIL DEBUG] Starting email send - email={email}, fullname={fullname}, role={role}, status={status}")
        print(f"[EMAIL DEBUG] Using GMAIL_USER={GMAIL_USER}")
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = f"Mama's Kitchen Account {status.title()} - {role.title()}"
        
        # Determine message content based on status
        if status == 'approved':
            subject_text = f"Welcome to Mama's Kitchen, {fullname}!"
            body_text = f"""
Hello {fullname},

Great news! Your {role} account on Mama's Kitchen has been approved by our admin team.

You can now log in and start using our platform:
- {role.title()}s can manage their inventory and orders
- Customers can browse and purchase products
- Riders can accept delivery orders

Please visit our website: http://127.0.0.1:5000

If you have any questions, feel free to contact our support team.

Best regards,
Mama's Kitchen Team
            """
            html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #FFA500;">Welcome to Mama's Kitchen, {fullname}!</h2>
      <p>Great news! Your <strong>{role}</strong> account on Mama's Kitchen has been <strong style="color: green;">APPROVED</strong> by our admin team.</p>
      <p>You can now log in and start using our platform:</p>
      <ul>
        <li>{role.title()}s can manage their inventory and orders</li>
        <li>Customers can browse and purchase products</li>
        <li>Riders can accept delivery orders</li>
      </ul>
      <p><a href="http://127.0.0.1:5000" style="background-color: #FFA500; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Visit Mama's Kitchen</a></p>
      <p>If you have any questions, feel free to contact our support team.</p>
      <p>Best regards,<br><strong>Mama's Kitchen Team</strong></p>
    </div>
  </body>
</html>
            """
        else:  # rejected
            subject_text = f"Mama's Kitchen Account Application - Information Needed"
            body_text = f"""
Hello {fullname},

Thank you for your interest in joining Mama's Kitchen as a {role}.

Unfortunately, your account application could not be approved at this time. This may be due to incomplete information or documentation issues.

Please review your submission and try again with complete and accurate information.

If you believe this is an error, please contact our support team.

Best regards,
Mama's Kitchen Team
            """
            html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #FFA500;">Mama's Kitchen Account Application Status</h2>
      <p>Hello {fullname},</p>
      <p>Thank you for your interest in joining Mama's Kitchen as a <strong>{role}</strong>.</p>
      <p>Unfortunately, your account application could not be approved at this time. This may be due to:</p>
      <ul>
        <li>Incomplete information or documentation</li>
        <li>Verification issues</li>
        <li>Non-compliance with our policies</li>
      </ul>
      <p>Please review your submission and try again with complete and accurate information.</p>
      <p>If you believe this is an error, please contact our support team.</p>
      <p>Best regards,<br><strong>Mama's Kitchen Team</strong></p>
    </div>
  </body>
</html>
            """
        
        # Attach both plain text and HTML versions
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        # Try to send email via Gmail SMTP
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            print("[EMAIL DEBUG] Connected to SMTP, attempting login...")
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            print("[EMAIL DEBUG] Login successful, sending message...")
            server.send_message(msg)
            print("[EMAIL DEBUG] Message sent, closing connection...")
            server.quit()
            print(f"[EMAIL SUCCESS] Sent {status} email to {email}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            print(f"[EMAIL WARNING] Gmail authentication failed: {str(e)}")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True  # Return True because we logged it
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True  # Return True because we logged it
            
    except Exception as e:
        import traceback
        print(f"[EMAIL ERROR] Unexpected error: {str(e)}")
        traceback.print_exc()
        return False

def send_suspension_email(email, fullname, role):
    """
    Send suspension email to user.
    Informs user that their account has been suspended.
    """
    try:
        print(f"[EMAIL DEBUG] Starting suspension email - email={email}, fullname={fullname}, role={role}")
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = f"Mama's Kitchen Account Suspended - {role.title()}"
        
        body_text = f"""
Hello {fullname},

We regret to inform you that your {role} account on Mama's Kitchen has been suspended by our admin team.

Account Suspension Reason:
Your account has been suspended due to violation of our platform policies or other administrative reasons. This is a temporary measure to protect the integrity of our platform and its community.

What This Means:
- Your account access has been restricted
- You will not be able to log in or perform any transactions
- Your account will remain suspended for 30 days
- After 30 days, your account will be automatically removed from our system

How to Appeal:
If you believe this suspension is a mistake or would like to dispute it, please contact our support team immediately with details about your case. We will review your appeal and take appropriate action.

Support Contact:
Email: support@mamas_kitchen.com
Phone: +1-800-MAMAS_KITCHEN

Suspension Details:
- Suspended On: Today's Date
- Account Role: {role.title()}
- Duration: 30 days

We appreciate your understanding. If you have any questions, please reach out to our support team.

Best regards,
Mama's Kitchen Support Team
        """
        
        html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #DC2626;">⚠️ Account Suspension Notice</h2>
      <p>Hello {fullname},</p>
      <p>We regret to inform you that your <strong>{role}</strong> account on Mama's Kitchen has been <strong style="color: #DC2626;">SUSPENDED</strong> by our admin team.</p>
      
      <h3 style="color: #374151;">Account Suspension Reason</h3>
      <p>Your account has been suspended due to violation of our platform policies or other administrative reasons. This is a temporary measure to protect the integrity of our platform and its community.</p>
      
      <h3 style="color: #374151;">What This Means</h3>
      <ul style="color: #666;">
        <li>Your account access has been restricted</li>
        <li>You will not be able to log in or perform any transactions</li>
        <li>Your account will remain suspended for 30 days</li>
        <li>After 30 days, your account will be automatically removed from our system</li>
      </ul>
      
      <h3 style="color: #374151;">How to Appeal</h3>
      <p>If you believe this suspension is a mistake or would like to dispute it, please <strong>contact our support team immediately</strong> with details about your case. We will review your appeal and take appropriate action.</p>
      
      <h3 style="color: #374151;">Suspension Details</h3>
      <table style="width: 100%; border-collapse: collapse;">
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Account Role</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">{role.title()}</td>
        </tr>
        <tr>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Suspension Duration</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">30 days</td>
        </tr>
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Auto-Removal Date</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">After suspension period</td>
        </tr>
      </table>
      
      <h3 style="color: #374151;">Support Contact</h3>
      <p style="color: #666;">
        Email: <strong>support@mamas_kitchen.com</strong><br>
        Phone: <strong>+1-800-MAMAS_KITCHEN</strong>
      </p>
      
      <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #9CA3AF; font-size: 12px;">
        We appreciate your understanding. If you have any questions, please reach out to our support team.<br>
        <strong>Mama's Kitchen Support Team</strong>
      </p>
    </div>
  </body>
</html>
        """
        
        # Attach both plain text and HTML versions
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        # Try to send email via Gmail SMTP
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP for suspension email...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            print("[EMAIL DEBUG] Connected to SMTP, attempting login...")
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            print("[EMAIL DEBUG] Login successful, sending suspension email...")
            server.send_message(msg)
            print("[EMAIL DEBUG] Suspension email sent, closing connection...")
            server.quit()
            print(f"[EMAIL SUCCESS] Sent suspension email to {email}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            print(f"[EMAIL WARNING] Gmail authentication failed: {str(e)}")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"SUSPENSION EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"SUSPENSION EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True
            
    except Exception as e:
        import traceback
        print(f"[EMAIL ERROR] Unexpected error in suspension email: {str(e)}")
        traceback.print_exc()
        return False

def send_restore_email(email, fullname, role):
    """
    Send restore email to user.
    Informs user that their account has been restored.
    """
    try:
        print(f"[EMAIL DEBUG] Starting restore email - email={email}, fullname={fullname}, role={role}")
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = f"Mama's Kitchen Account Restored - {role.title()}"
        
        body_text = f"""
Hello {fullname},

We are pleased to inform you that your {role} account on Mama's Kitchen has been restored and is now active!

Account Restoration Details:
Your account has been successfully restored by our admin team. Your account is now fully operational and you can resume all normal activities on the platform.

What You Can Now Do:
- Log in to your account
- Access all your listings and products
- Process orders and transactions
- Interact with customers and sellers
- View your account settings and profile

Getting Started Again:
1. Visit https://mamas_kitchen.com and log in with your credentials
2. Review your account to ensure all information is current
3. Check if any of your listings need updating
4. Start managing your activities

Important Reminder:
Please ensure that your account activities comply with our community guidelines and platform policies. Violation of these policies may result in future suspension.

Support Contact:
Email: support@mamas_kitchen.com
Phone: +1-800-MAMAS_KITCHEN

Restoration Details:
- Restored On: Today's Date
- Account Role: {role.title()}
- Status: Active

Welcome back to Mama's Kitchen! We're excited to have you back in our community.

Best regards,
Mama's Kitchen Support Team
        """
        
        html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #16A34A;">✅ Account Restoration Successful</h2>
      <p>Hello {fullname},</p>
      <p>We are pleased to inform you that your <strong>{role}</strong> account on Mama's Kitchen has been <strong style="color: #16A34A;">RESTORED</strong> and is now active!</p>
      
      <h3 style="color: #374151;">Account Restoration Details</h3>
      <p>Your account has been successfully restored by our admin team. Your account is now fully operational and you can resume all normal activities on the platform.</p>
      
      <h3 style="color: #374151;">What You Can Now Do</h3>
      <ul style="color: #666;">
        <li>Log in to your account</li>
        <li>Access all your listings and products</li>
        <li>Process orders and transactions</li>
        <li>Interact with customers and sellers</li>
        <li>View your account settings and profile</li>
      </ul>
      
      <h3 style="color: #374151;">Getting Started Again</h3>
      <ol style="color: #666;">
        <li>Visit <strong>https://mamas_kitchen.com</strong> and log in with your credentials</li>
        <li>Review your account to ensure all information is current</li>
        <li>Check if any of your listings need updating</li>
        <li>Start managing your activities</li>
      </ol>
      
      <h3 style="color: #374151;">Important Reminder</h3>
      <p style="background-color: #FEF3C7; border-left: 4px solid #F59E0B; padding: 12px; border-radius: 4px; color: #92400E;">
        Please ensure that your account activities comply with our community guidelines and platform policies. Violation of these policies may result in future suspension.
      </p>
      
      <h3 style="color: #374151;">Restoration Details</h3>
      <table style="width: 100%; border-collapse: collapse;">
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Account Role</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">{role.title()}</td>
        </tr>
        <tr>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Status</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong style="color: #16A34A;">ACTIVE</strong></td>
        </tr>
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Restoration Date</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">Today</td>
        </tr>
      </table>
      
      <h3 style="color: #374151;">Support Contact</h3>
      <p style="color: #666;">
        Email: <strong>support@mamas_kitchen.com</strong><br>
        Phone: <strong>+1-800-MAMAS_KITCHEN</strong>
      </p>
      
      <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #9CA3AF; font-size: 12px;">
        Welcome back to Mama's Kitchen! We're excited to have you back in our community.<br>
        <strong>Mama's Kitchen Support Team</strong>
      </p>
    </div>
  </body>
</html>
        """
        
        # Attach both plain text and HTML versions
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        # Try to send email via Gmail SMTP
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP for restore email...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            print("[EMAIL DEBUG] Connected to SMTP, attempting login...")
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            print("[EMAIL DEBUG] Login successful, sending restore email...")
            server.send_message(msg)
            print("[EMAIL DEBUG] Restore email sent, closing connection...")
            server.quit()
            print(f"[EMAIL SUCCESS] Restore email sent to {email}")
            return True
        except smtplib.SMTPAuthenticationError:
            print(f"[EMAIL WARNING] SMTP authentication failed for restore email")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"RESTORE EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"RESTORE EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[EMAIL ERROR] Failed to send restore email: {e}")
        return False

def send_forgot_password_email(email, fullname, reset_link):
    """
    Send forgot password email to user with reset link.
    """
    try:
        print(f"[EMAIL DEBUG] Starting forgot password email - email={email}, fullname={fullname}")
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = "Reset Your Mama's Kitchen Password"
        
        body_text = f"""
Hello {fullname},

We received a request to reset your Mama's Kitchen password. If you did not make this request, you can safely ignore this email.

To reset your password, click the link below:
{reset_link}

This link will expire in 24 hours. If it has expired, you can request a new password reset link.

If you did not request a password reset, please contact our support team immediately.

Best regards,
Mama's Kitchen Support Team
        """
        
        html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #FFA500;">Reset Your Mama's Kitchen Password</h2>
      <p>Hello {fullname},</p>
      <p>We received a request to reset your Mama's Kitchen password. If you did not make this request, you can safely ignore this email.</p>
      
      <h3 style="color: #374151;">Reset Your Password</h3>
      <p>Click the button below to reset your password:</p>
      <p style="text-align: center; margin: 20px 0;">
        <a href="{reset_link}" style="background-color: #FFA500; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">Reset Password</a>
      </p>
      
      <p style="color: #666;">Or copy and paste this link in your browser:</p>
      <p style="background-color: #F3F4F6; padding: 10px; border-radius: 4px; word-break: break-all; color: #666; font-size: 12px;">
        {reset_link}
      </p>
      
      <h3 style="color: #374151;">Link Expiration</h3>
      <p style="color: #666;">This link will expire in <strong>24 hours</strong>. If it has expired, you can request a new password reset link from the login page.</p>
      
      <h3 style="color: #374151;">Security Notice</h3>
      <p style="background-color: #FEE2E2; border-left: 4px solid #DC2626; padding: 12px; border-radius: 4px; color: #991B1B;">
        If you did not request this password reset, please contact our support team immediately at support@mamas_kitchen.com
      </p>
      
      <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #9CA3AF; font-size: 12px;">
        <strong>Mama's Kitchen Support Team</strong><br>
        Email: support@mamas_kitchen.com
      </p>
    </div>
  </body>
</html>
        """
        
        # Attach both plain text and HTML versions
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        # Try to send email via Gmail SMTP
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP for forgot password email...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            print("[EMAIL DEBUG] Connected to SMTP, attempting login...")
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            print("[EMAIL DEBUG] Login successful, sending forgot password email...")
            server.send_message(msg)
            print("[EMAIL DEBUG] Forgot password email sent, closing connection...")
            server.quit()
            print(f"[EMAIL SUCCESS] Forgot password email sent to {email}")
            return True
        except smtplib.SMTPAuthenticationError:
            print(f"[EMAIL WARNING] SMTP authentication failed for forgot password email")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"FORGOT PASSWORD EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"[EMAIL WARNING] Falling back to console output...")
            print(f"\n{'='*60}")
            print(f"FORGOT PASSWORD EMAIL WOULD BE SENT TO: {email}")
            print(f"SUBJECT: {msg['Subject']}")
            print(f"{'='*60}")
            print(body_text)
            print(f"{'='*60}\n")
            return True
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[EMAIL ERROR] Failed to send forgot password email: {e}")
        return False

def send_product_approval_email(email, seller_name, product_name):
    """
    Send product approval email to seller.
    Informs seller that their product has been approved and is now live.
    """
    try:
        print(f"[EMAIL DEBUG] Starting product approval email - email={email}, seller_name={seller_name}, product_name={product_name}")
        
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = f"Product Approved - {product_name}"
        
        body_text = f"""
Hello {seller_name},

Great news! Your product has been approved and is now live on Mama's Kitchen!

Product Approval Details:
Your product '{product_name}' has been reviewed and approved by our admin team. It is now visible to all customers on the platform.

What Happens Next:
- Your product is now live on Mama's Kitchen
- Customers can view, search, and purchase your product
- You'll receive notifications for new orders
- You can track sales and manage your inventory from your seller dashboard

Managing Your Product:
1. Log in to your seller account on Mama's Kitchen
2. Go to your Products section
3. View sales, customer reviews, and order updates
4. Update product details or pricing as needed

Tips for Success:
- Ensure your product description is clear and detailed
- Upload high-quality product images
- Respond promptly to customer inquiries
- Maintain competitive pricing
- Keep your inventory updated

Support:
If you have any questions or need assistance, contact us at:
Email: support@mamas_kitchen.com
Phone: +1-800-MAMAS_KITCHEN

Product Information:
- Product Name: {product_name}
- Status: APPROVED
- Next Step: Monitor sales and customer feedback

Thank you for choosing Mama's Kitchen as your selling platform!

Best regards,
Mama's Kitchen Admin Team
        """
        
        html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #16A34A;">🎉 Your Product Has Been Approved!</h2>
      <p>Hello {seller_name},</p>
      <p>Great news! Your product <strong>"{product_name}"</strong> has been <strong style="color: #16A34A;">APPROVED</strong> and is now live on Mama's Kitchen!</p>
      
      <h3 style="color: #374151;">Product Approval Details</h3>
      <p>Your product has been reviewed and approved by our admin team. It is now visible to all customers on the platform and ready for sales.</p>
      
      <h3 style="color: #374151;">What Happens Next</h3>
      <ul style="color: #666;">
        <li>Your product is now live on Mama's Kitchen</li>
        <li>Customers can view, search, and purchase your product</li>
        <li>You'll receive notifications for new orders</li>
        <li>You can track sales and manage your inventory from your seller dashboard</li>
      </ul>
      
      <h3 style="color: #374151;">Managing Your Product</h3>
      <ol style="color: #666;">
        <li>Log in to your seller account on Mama's Kitchen</li>
        <li>Go to your Products section</li>
        <li>View sales, customer reviews, and order updates</li>
        <li>Update product details or pricing as needed</li>
      </ol>
      
      <h3 style="color: #374151;">Tips for Success</h3>
      <div style="background-color: #DBEAFE; border-left: 4px solid #3B82F6; padding: 12px; border-radius: 4px; color: #1E40AF; margin: 10px 0;">
        <ul style="margin: 5px 0; padding-left: 20px;">
          <li>Ensure your product description is clear and detailed</li>
          <li>Upload high-quality product images</li>
          <li>Respond promptly to customer inquiries</li>
          <li>Maintain competitive pricing</li>
          <li>Keep your inventory updated</li>
        </ul>
      </div>
      
      <h3 style="color: #374151;">Product Approved Details</h3>
      <table style="width: 100%; border-collapse: collapse;">
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Product Name</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">{product_name}</td>
        </tr>
        <tr>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Status</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong style="color: #16A34A;">APPROVED</strong></td>
        </tr>
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Next Step</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">Monitor sales and customer feedback</td>
        </tr>
      </table>
      
      <h3 style="color: #374151;">Support</h3>
      <p style="color: #666;">
        If you have any questions or need assistance, contact us at:<br>
        Email: <strong>support@mamas_kitchen.com</strong><br>
        Phone: <strong>+1-800-MAMAS_KITCHEN</strong>
      </p>
      
      <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #9CA3AF; font-size: 12px;">
        Thank you for choosing Mama's Kitchen as your selling platform!<br>
        <strong>Mama's Kitchen Admin Team</strong>
      </p>
    </div>
  </body>
</html>
        """
        
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP for product approval email...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)
            server.quit()
            print(f"[EMAIL SUCCESS] Product approval email sent to {email}")
            return True
        except smtplib.SMTPAuthenticationError:
            print(f"[EMAIL WARNING] SMTP authentication failed for product approval email")
            print(f"\n{'='*60}\nPRODUCT APPROVAL EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"\n{'='*60}\nPRODUCT APPROVAL EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[EMAIL ERROR] Failed to send product approval email: {e}")
        return False

def send_product_rejection_email(email, seller_name, product_name, reason=""):
    """
    Send product rejection email to seller.
    Informs seller that their product has been rejected.
    """
    try:
        print(f"[EMAIL DEBUG] Starting product rejection email - email={email}, seller_name={seller_name}, product_name={product_name}")
        
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = f"Product Rejected - {product_name}"
        
        body_text = f"""
Hello {seller_name},

Unfortunately, your product '{product_name}' has been rejected and cannot be listed on Mama's Kitchen at this time.

Product Rejection Details:
After review by our admin team, your product did not meet our platform guidelines or quality standards.

Common Reasons for Rejection:
- Incomplete or unclear product information
- Product images are missing or of poor quality
- Pricing is not competitive or unrealistic
- Product violates platform policies
- Product description contains prohibited content
- Missing required certifications or licenses for certain product types

What You Can Do:
1. Review the product details carefully
2. Ensure all information is clear and accurate
3. Upload high-quality images from multiple angles
4. Verify pricing is competitive
5. Resubmit the product for review

Resubmitting Your Product:
Once you've made the necessary corrections, you can resubmit your product. Our team will review it again within 24-48 hours.

Need Help?
If you have questions about why your product was rejected or need clarification on our guidelines, please contact:
Email: support@mamas_kitchen.com
Phone: +1-800-MAMAS_KITCHEN

We appreciate your understanding and encourage you to resubmit your product after making the required improvements.

Best regards,
Mama's Kitchen Admin Team
        """
        
        html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #DC2626;">❌ Product Rejected</h2>
      <p>Hello {seller_name},</p>
      <p>Unfortunately, your product <strong>"{product_name}"</strong> has been <strong style="color: #DC2626;">REJECTED</strong> and cannot be listed on Mama's Kitchen at this time.</p>
      
      <h3 style="color: #374151;">Product Rejection Details</h3>
      <p>After review by our admin team, your product did not meet our platform guidelines or quality standards.</p>
      
      <h3 style="color: #374151;">Common Reasons for Rejection</h3>
      <ul style="color: #666;">
        <li>Incomplete or unclear product information</li>
        <li>Product images are missing or of poor quality</li>
        <li>Pricing is not competitive or unrealistic</li>
        <li>Product violates platform policies</li>
        <li>Product description contains prohibited content</li>
        <li>Missing required certifications or licenses for certain product types</li>
      </ul>
      
      <h3 style="color: #374151;">What You Can Do</h3>
      <ol style="color: #666;">
        <li>Review the product details carefully</li>
        <li>Ensure all information is clear and accurate</li>
        <li>Upload high-quality images from multiple angles</li>
        <li>Verify pricing is competitive</li>
        <li>Resubmit the product for review</li>
      </ol>
      
      <h3 style="color: #374151;">Resubmitting Your Product</h3>
      <p style="background-color: #FEF3C7; border-left: 4px solid #F59E0B; padding: 12px; border-radius: 4px; color: #92400E;">
        Once you've made the necessary corrections, you can resubmit your product. Our team will review it again within 24-48 hours.
      </p>
      
      <h3 style="color: #374151;">Need Help?</h3>
      <p style="color: #666;">
        If you have questions about why your product was rejected or need clarification on our guidelines, please contact:<br>
        Email: <strong>support@mamas_kitchen.com</strong><br>
        Phone: <strong>+1-800-MAMAS_KITCHEN</strong>
      </p>
      
      <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #9CA3AF; font-size: 12px;">
        We appreciate your understanding and encourage you to resubmit your product after making the required improvements.<br>
        <strong>Mama's Kitchen Admin Team</strong>
      </p>
    </div>
  </body>
</html>
        """
        
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP for product rejection email...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)
            server.quit()
            print(f"[EMAIL SUCCESS] Product rejection email sent to {email}")
            return True
        except smtplib.SMTPAuthenticationError:
            print(f"[EMAIL WARNING] SMTP authentication failed for product rejection email")
            print(f"\n{'='*60}\nPRODUCT REJECTION EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"\n{'='*60}\nPRODUCT REJECTION EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[EMAIL ERROR] Failed to send product rejection email: {e}")
        return False

def send_product_suspension_email(email, seller_name, product_name):
    """
    Send product suspension email to seller.
    Informs seller that their product has been archived/suspended.
    """
    try:
        print(f"[EMAIL DEBUG] Starting product suspension email - email={email}, seller_name={seller_name}, product_name={product_name}")
        
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = f"Product Suspended - {product_name}"
        
        body_text = f"""
Hello {seller_name},

We regret to inform you that your product '{product_name}' has been suspended from Mama's Kitchen.

Product Suspension Details:
Your product has been temporarily removed from the platform due to policy violations, customer complaints, or quality issues. The product will be archived for 30 days before automatic removal.

Reason for Suspension:
Your product violated one or more of our platform policies:
- Inaccurate or misleading product information
- Product quality concerns or customer complaints
- Pricing manipulation or unfair practices
- Prohibited items or content
- Other policy violations

What Happens Now:
- Your product is no longer visible to customers
- Existing orders may still be processed depending on status
- Your product will be automatically deleted after 30 days
- You can request manual deletion if needed

How to Appeal:
If you believe this suspension is a mistake or would like to dispute it, please contact our support team within 7 days:
Email: support@mamas_kitchen.com
Phone: +1-800-MAMAS_KITCHEN

Removing the Suspension:
1. Correct the issues that led to suspension
2. Resubmit the product for review
3. Our team will review and approve or provide feedback

Support:
For questions or assistance, please reach out to:
Email: support@mamas_kitchen.com
Phone: +1-800-MAMAS_KITCHEN

Suspension Details:
- Product Name: {product_name}
- Status: SUSPENDED
- Duration: 30 days before automatic deletion

We appreciate your understanding. Please take immediate action to resolve the issues.

Best regards,
Mama's Kitchen Admin Team
        """
        
        html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #DC2626;">⚠️ Product Suspended</h2>
      <p>Hello {seller_name},</p>
      <p>We regret to inform you that your product <strong>"{product_name}"</strong> has been <strong style="color: #DC2626;">SUSPENDED</strong> from Mama's Kitchen.</p>
      
      <h3 style="color: #374151;">Product Suspension Details</h3>
      <p>Your product has been temporarily removed from the platform due to policy violations, customer complaints, or quality issues. The product will be archived for 30 days before automatic removal.</p>
      
      <h3 style="color: #374151;">Reason for Suspension</h3>
      <p>Your product violated one or more of our platform policies:</p>
      <ul style="color: #666;">
        <li>Inaccurate or misleading product information</li>
        <li>Product quality concerns or customer complaints</li>
        <li>Pricing manipulation or unfair practices</li>
        <li>Prohibited items or content</li>
        <li>Other policy violations</li>
      </ul>
      
      <h3 style="color: #374151;">What Happens Now</h3>
      <ul style="color: #666;">
        <li>Your product is no longer visible to customers</li>
        <li>Existing orders may still be processed depending on status</li>
        <li>Your product will be automatically deleted after 30 days</li>
        <li>You can request manual deletion if needed</li>
      </ul>
      
      <h3 style="color: #374151;">How to Appeal</h3>
      <p style="background-color: #FEF3C7; border-left: 4px solid #F59E0B; padding: 12px; border-radius: 4px; color: #92400E;">
        If you believe this suspension is a mistake or would like to dispute it, please <strong>contact our support team within 7 days</strong>.
      </p>
      
      <h3 style="color: #374151;">Removing the Suspension</h3>
      <ol style="color: #666;">
        <li>Correct the issues that led to suspension</li>
        <li>Resubmit the product for review</li>
        <li>Our team will review and approve or provide feedback</li>
      </ol>
      
      <h3 style="color: #374151;">Suspension Details</h3>
      <table style="width: 100%; border-collapse: collapse;">
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Product Name</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">{product_name}</td>
        </tr>
        <tr>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Status</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong style="color: #DC2626;">SUSPENDED</strong></td>
        </tr>
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Duration</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">30 days before automatic deletion</td>
        </tr>
      </table>
      
      <h3 style="color: #374151;">Support</h3>
      <p style="color: #666;">
        For questions or assistance, please reach out to:<br>
        Email: <strong>support@mamas_kitchen.com</strong><br>
        Phone: <strong>+1-800-MAMAS_KITCHEN</strong>
      </p>
      
      <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #9CA3AF; font-size: 12px;">
        We appreciate your understanding. Please take immediate action to resolve the issues.<br>
        <strong>Mama's Kitchen Admin Team</strong>
      </p>
    </div>
  </body>
</html>
        """
        
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP for product suspension email...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)
            server.quit()
            print(f"[EMAIL SUCCESS] Product suspension email sent to {email}")
            return True
        except smtplib.SMTPAuthenticationError:
            print(f"[EMAIL WARNING] SMTP authentication failed for product suspension email")
            print(f"\n{'='*60}\nPRODUCT SUSPENSION EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"\n{'='*60}\nPRODUCT SUSPENSION EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[EMAIL ERROR] Failed to send product suspension email: {e}")
        return False

def send_product_restore_email(email, seller_name, product_name):
    """
    Send product restore email to seller.
    Informs seller that their product has been restored.
    """
    try:
        print(f"[EMAIL DEBUG] Starting product restore email - email={email}, seller_name={seller_name}, product_name={product_name}")
        
        msg = MIMEMultipart('alternative')
        msg['From'] = GMAIL_USER
        msg['To'] = email
        msg['Subject'] = f"Product Restored - {product_name}"
        
        body_text = f"""
Hello {seller_name},

Great news! Your product '{product_name}' has been restored and is now live on Mama's Kitchen again!

Product Restoration Details:
Your product has been restored by our admin team and is now visible to customers. You can resume selling this product on the platform.

What You Can Do Now:
- Your product is live and visible to customers
- You can receive new orders for this product
- Manage inventory and pricing from your dashboard
- Update product details and images as needed
- Monitor sales and customer feedback

Managing Your Restored Product:
1. Log in to your seller account on Mama's Kitchen
2. Go to your Products section
3. Verify all product information is current
4. Check inventory levels
5. Monitor incoming orders

Important Reminder:
Please ensure that your product continues to comply with our platform policies. Violations may result in future suspension. Maintain:
- Accurate and complete product information
- High-quality product images
- Fair and competitive pricing
- Timely responses to customer inquiries
- Compliance with all platform guidelines

Support:
If you have any questions or need assistance, contact us at:
Email: support@mamas_kitchen.com
Phone: +1-800-MAMAS_KITCHEN

Restoration Details:
- Product Name: {product_name}
- Status: RESTORED AND ACTIVE
- Next Step: Resume sales and monitor performance

Welcome back to Mama's Kitchen! Thank you for your commitment to maintaining quality standards.

Best regards,
Mama's Kitchen Admin Team
        """
        
        html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #16A34A;">✅ Product Restored!</h2>
      <p>Hello {seller_name},</p>
      <p>Great news! Your product <strong>"{product_name}"</strong> has been <strong style="color: #16A34A;">RESTORED</strong> and is now live on Mama's Kitchen again!</p>
      
      <h3 style="color: #374151;">Product Restoration Details</h3>
      <p>Your product has been restored by our admin team and is now visible to customers. You can resume selling this product on the platform.</p>
      
      <h3 style="color: #374151;">What You Can Do Now</h3>
      <ul style="color: #666;">
        <li>Your product is live and visible to customers</li>
        <li>You can receive new orders for this product</li>
        <li>Manage inventory and pricing from your dashboard</li>
        <li>Update product details and images as needed</li>
        <li>Monitor sales and customer feedback</li>
      </ul>
      
      <h3 style="color: #374151;">Managing Your Restored Product</h3>
      <ol style="color: #666;">
        <li>Log in to your seller account on Mama's Kitchen</li>
        <li>Go to your Products section</li>
        <li>Verify all product information is current</li>
        <li>Check inventory levels</li>
        <li>Monitor incoming orders</li>
      </ol>
      
      <h3 style="color: #374151;">Important Reminder</h3>
      <p style="background-color: #FEF3C7; border-left: 4px solid #F59E0B; padding: 12px; border-radius: 4px; color: #92400E;">
        Please ensure that your product continues to comply with our platform policies. Violations may result in future suspension. Maintain:
        <ul style="margin: 5px 0; padding-left: 20px;">
          <li>Accurate and complete product information</li>
          <li>High-quality product images</li>
          <li>Fair and competitive pricing</li>
          <li>Timely responses to customer inquiries</li>
          <li>Compliance with all platform guidelines</li>
        </ul>
      </p>
      
      <h3 style="color: #374151;">Restoration Details</h3>
      <table style="width: 100%; border-collapse: collapse;">
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Product Name</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">{product_name}</td>
        </tr>
        <tr>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Status</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong style="color: #16A34A;">RESTORED AND ACTIVE</strong></td>
        </tr>
        <tr style="background-color: #F3F4F6;">
          <td style="padding: 8px; border: 1px solid #E5E7EB;"><strong>Next Step</strong></td>
          <td style="padding: 8px; border: 1px solid #E5E7EB;">Resume sales and monitor performance</td>
        </tr>
      </table>
      
      <h3 style="color: #374151;">Support</h3>
      <p style="color: #666;">
        If you have any questions or need assistance, contact us at:<br>
        Email: <strong>support@mamas_kitchen.com</strong><br>
        Phone: <strong>+1-800-MAMAS_KITCHEN</strong>
      </p>
      
      <p style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #9CA3AF; font-size: 12px;">
        Welcome back to Mama's Kitchen! Thank you for your commitment to maintaining quality standards.<br>
        <strong>Mama's Kitchen Admin Team</strong>
      </p>
    </div>
  </body>
</html>
        """
        
        part1 = MIMEText(body_text, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        try:
            print("[EMAIL DEBUG] Connecting to Gmail SMTP for product restore email...")
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=5)
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)
            server.quit()
            print(f"[EMAIL SUCCESS] Product restore email sent to {email}")
            return True
        except smtplib.SMTPAuthenticationError:
            print(f"[EMAIL WARNING] SMTP authentication failed for product restore email")
            print(f"\n{'='*60}\nPRODUCT RESTORE EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
        except Exception as e:
            print(f"[EMAIL WARNING] SMTP failed: {str(e)}")
            print(f"\n{'='*60}\nPRODUCT RESTORE EMAIL WOULD BE SENT TO: {email}\nSUBJECT: {msg['Subject']}\n{'='*60}\n{body_text}\n{'='*60}\n")
            return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[EMAIL ERROR] Failed to send product restore email: {e}")
        return False

def cleanup_deleted_products(): 
    cursor = db.cursor()
    cursor.execute("""
        DELETE FROM products
        WHERE status = 'deleted' AND archived_at < NOW() - INTERVAL 30 DAY
    """)
    db.commit()
    cursor.close()

@app.route("/")
def homepage():
    user_id = session.get("user_id")
    user = None

    if user_id:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        cursor.close()

    # Fetch customer-targeted notifications (tagged with [customer:<id>])
    customer_notifications = []
    if user_id:
        try:
            notif_cursor = db.cursor(dictionary=True)
            tag = f"[customer:{user_id}]"
            notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
            customer_notifications = notif_cursor.fetchall() or []
            notif_cursor.close()
        except Exception:
            customer_notifications = []


    # Fetch products with average rating (include food-specific fields)
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT id, name, price, image, cuisine_type, preparation_time, servings, allergens, dietary_options, is_spicy, spice_level, is_bestseller FROM products WHERE status='approved' ORDER BY created_at DESC LIMIT 12")
    products = cursor.fetchall()
    product_ids = [p['id'] for p in products]
    ratings_map = {}
    if product_ids:
        placeholders = ','.join(['%s'] * len(product_ids))
        sql = f"SELECT product_id, AVG(rating) AS avg_rating, COUNT(*) AS cnt FROM product_ratings WHERE product_id IN ({placeholders}) GROUP BY product_id"
        params = tuple(product_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in product_ratings query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        rows = cursor.fetchall()
        for r in rows:
            ratings_map[r['product_id']] = {'avg': float(r.get('avg_rating') or 0), 'cnt': int(r.get('cnt') or 0)}
    for p in products:
        r = ratings_map.get(p['id'])
        p['avg_rating'] = round(r['avg'], 2) if r else 0.0
        p['rating_count'] = r['cnt'] if r else 0
    cursor.close()
    return render_template("homepages.html", user=user, products=products, notifications=customer_notifications)

@app.route("/loginreg", methods=["GET", "POST"])
def loginreg():
    active_role = request.args.get("role", "login")  # default to login
    form_data = None

    if request.method == "POST":
        # --- LOGIN FORM ---
        if "login_email" in request.form:
            email = request.form.get("login_email")
            password = request.form.get("login_password")

            cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cursor.fetchone()

            if not user or not verify_password(user.get("password"), password):
                flash("Invalid email or password!", "error")
                return render_template("loginreg.html", active_role="login")

            if not is_hashed_password(user.get("password")):
                cursor.execute(
                    "UPDATE users SET password=%s WHERE id=%s",
                    (hash_password(password), user.get("id"))
                )
                db.commit()

            # defensive access in case database row lacks expected keys
            user_role = user.get("role") if isinstance(user, dict) else None
            user_status = user.get("status") if isinstance(user, dict) else None

            if user_role == "admin":
                # prevent admins from logging in here
                flash("Please log in through the Admin Portal.", "error")
                return redirect(url_for("admin_login"))

            if user_status == "pending":
                flash("Your account is still pending admin approval.", "error")
                return render_template("loginreg.html", active_role="login")

            if user_status == "archived":
                flash("Your registration was rejected by the admin.", "error")
                return render_template("loginreg.html", active_role="login")

            # set session (use safe defaults)
            session["user_id"] = user.get("id")
            session["fullname"] = user.get("fullname")
            session["email"] = user.get("email")
            session["role"] = user_role or "customer"

            flash("Login successful!", "success")

            next_page = request.args.get("next")

            # redirect based on role
            if session["role"] == "seller":
                return redirect(url_for("seller"))
            elif session["role"] == "rider":
                return redirect(url_for("rider"))
            else:
                if next_page:
                    return redirect(next_page)
                return redirect(url_for("homepage"))

    return render_template("loginreg.html", active_role=active_role, form_data=form_data)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        cursor.execute("SELECT * FROM users WHERE email=%s AND role='admin'", (email,))
        admin = cursor.fetchone()

        if not admin or not verify_password(admin.get("password"), password):
            flash("Invalid admin credentials!", "error")
            return render_template("admin_login.html")

        if not is_hashed_password(admin.get("password")):
            cursor.execute(
                "UPDATE users SET password=%s WHERE id=%s",
                (hash_password(password), admin.get("id"))
            )
            db.commit()

        # optional: check if archived
        if admin["status"] == "archived":
            flash("This admin account has been deactivated.", "error")
            return render_template("admin_login.html")

        # session
        session["user_id"] = admin["id"]
        session["fullname"] = admin["fullname"]
        session["email"] = admin["email"]
        session["role"] = admin["role"]

        flash("Welcome back, Admin!", "success")
        return redirect(url_for("admin"))  # admin dashboard route

    return render_template("admin_login.html")

@app.route("/register_customer", methods=["POST"])
def register_customer():
    form_data = request.form.to_dict()
    active_role = "customer"

    fullname = form_data.get("fullname")
    email = form_data.get("email")
    password = form_data.get("password")
    confirm_password = form_data.get("confirm_password")
    date_of_birth = form_data.get("date_of_birth")
    phone = form_data.get("phone")
    id_picture = request.files.get("id_picture")

    region_code = form_data.get("region")
    region_name = form_data.get("region_name")
    province_code = form_data.get("province")
    province_name = form_data.get("province_name")
    city_code = form_data.get("city")
    city_name = form_data.get("city_name")
    barangay_code = form_data.get("barangay")
    barangay_name = form_data.get("barangay_name")
    street = form_data.get("street")

    if password != confirm_password:
        flash("Passwords do not match!", "error")
        return render_template("loginreg.html", form_data=form_data, active_role="customer")

    password_error = validate_strong_password(password)
    if password_error:
        flash(password_error, "error")
        return render_template("loginreg.html", form_data=form_data, active_role="customer")

    try:
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already registered!", "error")
            return render_template("loginreg.html", form_data=form_data, active_role="customer")

        id_path = save_file(id_picture, os.path.join(app.config["UPLOAD_FOLDER"], "id_pictures", "customers"))

        cursor.execute("""
            INSERT INTO users (fullname, email, password, role, id_picture, status)
            VALUES (%s, %s, %s, 'customer', %s, 'pending')
        """, (fullname, email, hash_password(password), id_path))
        db.commit()
        user_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO customers (user_id, date_of_birth, phone)
            VALUES (%s, %s, %s)
        """, (user_id, date_of_birth, phone))

        cursor.execute("""
            INSERT INTO addresses (
                user_id, region_code, region_name,
                province_code, province_name,
                city_code, city_name,
                barangay_code, barangay_name,
                street, is_default
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
        """, (user_id, region_code, region_name, province_code, province_name,
              city_code, city_name, barangay_code, barangay_name, street))

        db.commit()
        notify_admin_user_registration("customer", fullname, email, cursor=cursor)
        db.commit()
        flash("Customer registration submitted! Wait for admin approval.", "success")
        return redirect(url_for("loginreg"))
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        app.logger.exception("Customer registration failed")
        flash(f"Customer registration failed: {e}", "error")
        return render_template("loginreg.html", form_data=form_data, active_role="customer")

@app.route("/register_seller", methods=["POST"])
def register_seller():
    form_data = request.form.to_dict()
    active_role = "seller"

    fullname = form_data.get("fullname")
    email = form_data.get("email")
    password = form_data.get("password")
    confirm_password = form_data.get("confirm_password")
    date_of_birth = form_data.get("date_of_birth")
    phone = form_data.get("phone")
    business_name = form_data.get("business_name")

    region_code = form_data.get("region")
    region_name = form_data.get("region_name")
    province_code = form_data.get("province")
    province_name = form_data.get("province_name")
    city_code = form_data.get("city")
    city_name = form_data.get("city_name")
    barangay_code = form_data.get("barangay")
    barangay_name = form_data.get("barangay_name")
    street = form_data.get("street")
    
    business_permit = request.files.get("business_permit")
    id_picture = request.files.get("id_picture")
        


    if password != confirm_password:
        flash("Passwords do not match!", "error")
        return render_template("loginreg.html", form_data=form_data, active_role=active_role)

    password_error = validate_strong_password(password)
    if password_error:
        flash(password_error, "error")
        return render_template("loginreg.html", form_data=form_data, active_role=active_role)

    try:
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already exists!", "error")
            return render_template("loginreg.html", form_data=form_data, active_role=active_role)

        id_path = save_file(id_picture, os.path.join(app.config["UPLOAD_FOLDER"], "id_pictures", "sellers"))
        permit_path = save_file(business_permit, os.path.join(app.config["UPLOAD_FOLDER"], "business_permits"))

        cursor.execute("""
            INSERT INTO users (fullname, email, password, role, id_picture, status)
            VALUES (%s, %s, %s, 'seller', %s, 'pending')
        """, (fullname, email, hash_password(password), id_path))
        db.commit()
        user_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO sellers (user_id, business_name, business_permit, date_of_birth, phone)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, business_name, permit_path, date_of_birth, phone))

        cursor.execute("""
            INSERT INTO addresses (
                user_id, region_code, region_name,
                province_code, province_name,
                city_code, city_name,
                barangay_code, barangay_name,
                street, is_default
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
        """, (user_id, region_code, region_name, province_code, province_name,
              city_code, city_name, barangay_code, barangay_name, street))

        db.commit()
        notify_admin_user_registration("seller", fullname, email, cursor=cursor)
        db.commit()
        flash("Seller registration submitted! Wait for admin approval.", "success")
        return redirect(url_for("loginreg"))
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        app.logger.exception("Seller registration failed")
        flash(f"Seller registration failed: {e}", "error")
        return render_template("loginreg.html", form_data=form_data, active_role=active_role)

@app.route("/register_rider", methods=["POST"])
def register_rider():
    form_data = request.form.to_dict()
    active_role = "rider"

    fullname = form_data.get("fullname")
    email = form_data.get("email")
    password = form_data.get("password")
    confirm_password = form_data.get("confirm_password")
    date_of_birth = form_data.get("date_of_birth")
    phone = form_data.get("phone")
    vehicle_type = form_data.get("vehicle_type")
    plate_number = form_data.get("plate_number")

    region_code = form_data.get("region")
    region_name = form_data.get("region_name")
    province_code = form_data.get("province")
    province_name = form_data.get("province_name")
    city_code = form_data.get("city")
    city_name = form_data.get("city_name")
    barangay_code = form_data.get("barangay")
    barangay_name = form_data.get("barangay_name")
    street = form_data.get("street")
    
    drivers_license = request.files.get("drivers_license")
    id_picture = request.files.get("id_picture")

    if password != confirm_password:
        flash("Passwords do not match!", "error")
        return render_template("loginreg.html", form_data=form_data, active_role=active_role)

    password_error = validate_strong_password(password)
    if password_error:
        flash(password_error, "error")
        return render_template("loginreg.html", form_data=form_data, active_role=active_role)

    try:
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already registered!", "error")
            return render_template("loginreg.html", form_data=form_data, active_role=active_role)

        id_path = save_file(id_picture, os.path.join(app.config["UPLOAD_FOLDER"], "id_pictures", "riders"))
        license_path = save_file(drivers_license, os.path.join(app.config["UPLOAD_FOLDER"], "licenses"))

        cursor.execute("""
            INSERT INTO users (fullname, email, password, role, id_picture, status)
            VALUES (%s, %s, %s, 'rider', %s, 'pending')
        """, (fullname, email, hash_password(password), id_path))
        db.commit()
        user_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO riders (user_id, vehicle_type, plate_number, drivers_license, date_of_birth, phone)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, vehicle_type, plate_number, license_path, date_of_birth, phone))

        cursor.execute("""
            INSERT INTO addresses (
                user_id, region_code, region_name,
                province_code, province_name,
                city_code, city_name,
                barangay_code, barangay_name,
                street, is_default
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
        """, (user_id, region_code, region_name, province_code, province_name,
              city_code, city_name, barangay_code, barangay_name, street))

        notify_admin_user_registration("rider", fullname, email, cursor=cursor)
        db.commit()
        flash("Rider registration submitted! Wait for admin approval.", "success")
        return redirect(url_for("loginreg"))
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        app.logger.exception("Rider registration failed")
        flash(f"Rider registration failed: {e}", "error")
        return render_template("loginreg.html", form_data=form_data, active_role=active_role)

@app.route('/view_order_rating/<int:order_id>')
def view_order_rating(order_id):
    if 'user_id' not in session:
        flash('You must be logged in to view ratings.', 'error')
        return redirect(url_for('loginreg'))

    user_id = session['user_id']
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT pr.product_id, p.name AS product_name, p.image AS product_image, pr.rating, pr.comment, pr.created_at
            FROM product_ratings pr
            LEFT JOIN products p ON pr.product_id = p.id
            WHERE pr.order_id = %s AND pr.user_id = %s
        """, (order_id, user_id))
        ratings = cursor.fetchall()
    finally:
        cursor.close()

    return render_template('view_order_rating.html', ratings=ratings, order_id=order_id)


@app.route("/order/cancel_override", methods=["POST"])
def admin_cancel_override():
    user_id = session.get("user_id")
    role = session.get("role")
    if role != 'admin' or not user_id:
        return jsonify({"success": False, "error": "Not authorized"}), 403

    data = request.get_json() or request.form
    order_id = data.get('order_id')
    override_reason = data.get('reason') or 'Admin override: seller rejection overridden'
    override_notes = data.get('notes') or ''
    if not order_id:
        return jsonify({"success": False, "error": "Missing order_id"}), 400

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404

    # Allow override only when the order is in processing or cancel_request to avoid unintended overrides
    if (order.get('status') or '').lower() not in ('processing', 'cancel_request'):
        cursor.close()
        return jsonify({"success": False, "error": "Order cannot be overridden in its current status"}), 400

    # Apply override: mark as cancelled and clear seller rejection metadata
    # Update order and insert log atomically in same transaction
    cursor.execute("UPDATE orders SET status=%s, cancelled_at=NOW(), cancel_rejection_reason=NULL, cancel_rejection_notes=NULL, cancel_rejected_at=NULL, cancel_rejected_by=NULL WHERE id=%s", ("cancelled", order_id))
    cursor.execute(
        "INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)",
        (order_id, 'cancel_override', user_id, 'admin', override_reason, override_notes)
    )
    db.commit()

    # (Audit log handled as part of same transaction above)

    # Notify customer
    try:
        notif_cur = db.cursor()
        customer_msg = f"[customer:{order.get('user_id')}] Your order #{order_id} cancellation has been approved by admin override. Reason: {override_reason}"
        if override_notes:
            customer_msg += f" Notes: {override_notes}"
        notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
        seller_id = order.get('seller_id')
        if seller_id:
            seller_msg = f"[seller:{seller_id}] Order #{order_id} cancellation was approved by admin override. Reason: {override_reason}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
        admin_msg = f"Order #{order_id} status changed to cancelled by admin override. Reason: {override_reason}"
        notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
        db.commit()
        notif_cur.close()
    except Exception:
        try:
            notif_cur.close()
        except Exception:
            pass

    cursor.close()
    return jsonify({"success": True, "message": "Order override applied and customer notified."})


@app.route("/products/<category>")
def products_page(category):
    user = None
    user_id = session.get("user_id")

    cursor = db.cursor(dictionary=True)
    if user_id:
        cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

    # Category mapping
    category_map = {
        "Dog": "Dog%",
        "Cat": "Cat%",
        "Fish": "Aquarium%",
        "Bird": "Bird%",
        "Grooming": "Pet Grooming%",
        "Pet Health": "Pet Health%"
    }

    # If a search query is provided, ignore the category filter and search across
    # name, category and description. Otherwise, filter by category as before.
    q = request.args.get('q', '').strip()
    # Pagination params
    try:
        page = int(request.args.get('page', 1))
        if page < 1:
            page = 1
    except Exception:
        page = 1
    per_page = 12
    offset = (page - 1) * per_page

    # total count (for pagination)
    total_products = 0
    if q:
        like_q = f"%{q}%"
        count_query = "SELECT COUNT(*) AS cnt FROM products WHERE status='approved' AND (name LIKE %s OR category LIKE %s OR description LIKE %s)"
        cursor.execute(count_query, (like_q, like_q, like_q))
        total_products = int(cursor.fetchone().get('cnt') or 0)
        total_pages = math.ceil(total_products / per_page) if per_page > 0 else 1
        if total_pages < 1:
            total_pages = 1
        # clamp page
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        fetch_query = "SELECT * FROM products WHERE status='approved' AND (name LIKE %s OR category LIKE %s OR description LIKE %s) ORDER BY created_at DESC LIMIT %s OFFSET %s"
        cursor.execute(fetch_query, (like_q, like_q, like_q, per_page, offset))
    else:
        search_category = category_map.get(category, "%")
        # count
        cursor.execute("SELECT COUNT(*) AS cnt FROM products WHERE category LIKE %s AND status='approved'", (search_category,))
        total_products = int(cursor.fetchone().get('cnt') or 0)
        total_pages = math.ceil(total_products / per_page) if per_page > 0 else 1
        if total_pages < 1:
            total_pages = 1
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        # Fetch products by category (limit for pagination)
        cursor.execute("""
            SELECT * FROM products
            WHERE category LIKE %s AND status='approved'
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (search_category, per_page, offset))
    products = cursor.fetchall()

    # Set display image (fallback to default)
    # prepare ratings for all products (batch query) if reviews table exists
    product_ids = [p['id'] for p in products]
    has_reviews = False
    if product_ids:
        try:
            cursor.execute("SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'product_ratings'")
            tbl = cursor.fetchone()
            has_reviews = bool(tbl and tbl.get('cnt', 0) > 0)
        except Exception:
            has_reviews = False

    ratings_map = {}
    if product_ids:
        placeholders = ','.join(['%s'] * len(product_ids))
        sql = f"SELECT product_id, AVG(rating) AS avg_rating, COUNT(*) AS cnt FROM product_ratings WHERE product_id IN ({placeholders}) GROUP BY product_id"
        params = tuple(product_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in product_ratings query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        rows = cursor.fetchall()
        for r in rows:
            ratings_map[r['product_id']] = {'avg': float(r.get('avg_rating') or 0), 'cnt': int(r.get('cnt') or 0)}

    for p in products:
        # image
        if p.get('image'):
            p['display_image'] = media_url(p['image'], 'image/default.png')
        else:
            p['display_image'] = media_url(None, 'image/default.png')
        # attach rating info (defaults)
        r = ratings_map.get(p['id']) if ratings_map else None
        p['rating'] = round(r['avg'], 2) if r else 0.0
        p['rating_count'] = r['cnt'] if r else 0

    cursor.close()
    # Fetch customer-targeted notifications (if logged in)
    customer_notifications = []
    if user_id:
        try:
            notif_cursor = db.cursor(dictionary=True)
            tag = f"[customer:{user_id}]"
            notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
            customer_notifications = notif_cursor.fetchall() or []
            notif_cursor.close()
        except Exception:
            customer_notifications = []

    # Friendly display names for categories
    display_map = {
        'Dog': 'Dog Food & Treats',
        'Cat': 'Cat Litter & Accessories',
        'Fish': 'Aquariums & Fish Supplies',
        'Bird': 'Bird Feeders & Food',
        'Grooming': 'Pet Grooming Products',
        'Pet Health': 'Pet Health & Wellness',
        'search': 'Search Results'
    }
    display_category = display_map.get(category, category)
    # Total pages for pagination
    total_pages = math.ceil(total_products / per_page) if per_page > 0 else 1
    if total_pages < 1:
        total_pages = 1

    return render_template("products.html", category=category, display_category=display_category, products=products, user=user, notifications=customer_notifications, q=q, page=page, per_page=per_page, total_pages=total_pages, total_products=total_products)

@app.route("/product/<int:product_id>")
def product_detail(product_id):
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM products WHERE id=%s AND status='approved'", (product_id,))
    product = cursor.fetchone()

    cursor.execute("SELECT * FROM product_variants WHERE product_id=%s", (product_id,))
    variants = cursor.fetchall()
    if product and variants:
        product["stock"] = sum(int(v.get("stock") or 0) for v in variants)

    user = None
    if 'user_id' in session:
        cursor.execute("SELECT fullname, profile_pic FROM users WHERE id=%s", (session['user_id'],))
        user = cursor.fetchone()

    # compute average rating and count
    avg_rating = 0.0
    rating_count = 0
    cursor.execute("SELECT AVG(rating) AS avg_rating, COUNT(*) AS cnt FROM product_ratings WHERE product_id=%s", (product_id,))
    r = cursor.fetchone()
    if r and r.get('avg_rating') is not None:
        avg_rating = float(r.get('avg_rating'))
    rating_count = int(r.get('cnt') or 0)
    # Fetch recent reviews for display on product detail
    reviews = []
    try:
        cursor.execute(
            """
            SELECT pr.rating, pr.comment, pr.created_at, u.fullname
            FROM product_ratings pr
            LEFT JOIN users u ON pr.user_id = u.id
            WHERE pr.product_id = %s
            ORDER BY pr.created_at DESC
            LIMIT 50
            """,
            (product_id,)
        )
        reviews = cursor.fetchall() or []
    except Exception:
        reviews = []
    # Rating breakdown counts per star (1..5)
    rating_breakdown = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT rating, COUNT(*) AS cnt FROM product_ratings WHERE product_id=%s GROUP BY rating", (product_id,))
        rows = cursor.fetchall() or []
        for r in rows:
            rating = int(r.get('rating') or 0)
            cnt = int(r.get('cnt') or 0)
            if rating in rating_breakdown:
                rating_breakdown[rating] = cnt
        cursor.close()
    except Exception:
        # fallback to zeros
        rating_breakdown = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    cursor.close()
    # Fetch customer-targeted notifications (if logged in)
    customer_notifications = []
    if user and user.get('id') or session.get('user_id'):
        try:
            uid = session.get('user_id')
            notif_cursor = db.cursor(dictionary=True)
            tag = f"[customer:{uid}]"
            notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
            customer_notifications = notif_cursor.fetchall() or []
            notif_cursor.close()
        except Exception:
            customer_notifications = []

    # Fetch seller info for this product to show 'shop name' with message button
    seller_info = None
    try:
        if product and product.get('seller_id'):
            sc = db.cursor(dictionary=True)
            sc.execute("SELECT u.id, u.fullname, u.email, s.business_name FROM users u LEFT JOIN sellers s ON s.user_id = u.id WHERE u.id = %s", (product.get('seller_id'),))
            srow = sc.fetchone()
            sc.close()
            if srow:
                seller_info = {
                    'id': srow.get('id'),
                    'name': srow.get('business_name') or srow.get('fullname') or str(srow.get('id')),
                    'email': srow.get('email')
                }
    except Exception:
        # If anything fails, leave seller_info as None and proceed to render page
        seller_info = None

    return render_template("product_detail.html", product=product, variants=variants, user=user, avg_rating=round(avg_rating,2), rating_count=rating_count, reviews=reviews, rating_breakdown=rating_breakdown, notifications=customer_notifications, seller=seller_info)


@app.route('/api/product/<int:product_id>')
def api_product(product_id):
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM products WHERE id=%s AND status='approved'", (product_id,))
    product = cursor.fetchone()

    if not product:
        cursor.close()
        return jsonify({'success': False, 'error': 'Product not found'}), 404

    cursor.execute("SELECT id, color, size, price, stock, weight_kg FROM product_variants WHERE product_id=%s", (product_id,))
    variants = cursor.fetchall()
    if variants:
        product["stock"] = sum(int(v.get("stock") or 0) for v in variants)

    # attach image URL
    if product.get('image'):
        img = media_url(product['image'], 'image/default.png')
    else:
        img = media_url(None, 'image/default.png')

    # compute rating (best-effort)
    avg_rating = 0.0
    rating_count = 0
    try:
        cursor.execute("SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'product_ratings'")
        tbl = cursor.fetchone()
        if tbl and tbl.get('cnt', 0) > 0:
            cursor.execute("SELECT AVG(rating) AS avg_rating, COUNT(*) AS cnt FROM product_ratings WHERE product_id=%s", (product_id,))
            r = cursor.fetchone()
            if r and r.get('avg_rating') is not None:
                avg_rating = float(r.get('avg_rating'))
            rating_count = int(r.get('cnt') or 0)
    except Exception:
        avg_rating = 0.0
        rating_count = 0

    cursor.close()
    product['image_url'] = img
    product['rating'] = round(avg_rating, 2)
    product['rating_count'] = rating_count

    return jsonify({'success': True, 'product': product, 'variants': variants})

@app.route("/add_to_cart", methods=["POST"])
def add_to_cart():
    if "user_id" not in session:
        # Return JSON instead of redirect
        return {"status": "not_logged_in", "login_url": url_for("loginreg", next=request.referrer)}

    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    variant_id = data.get("variant_id")
    qty = data.get("quantity")

    try:
        product_id = int(product_id)
        qty = int(qty)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Invalid product or quantity"}, 400

    if qty <= 0:
        return {"status": "error", "message": "Invalid quantity"}, 400

    if variant_id in (None, "", 0, "0"):
        variant_id = None
    else:
        try:
            variant_id = int(variant_id)
        except (TypeError, ValueError):
            return {"status": "error", "message": "Invalid variant"}, 400

    cursor = db.cursor(dictionary=True)

    # Check stock availability
    if variant_id:
        cursor.execute("SELECT stock FROM product_variants WHERE id=%s AND product_id=%s", (variant_id, product_id))
        variant = cursor.fetchone()
        if not variant or variant["stock"] < qty:
            cursor.close()
            return {"status": "error", "message": "Insufficient stock for this variant"}, 400
    else:
        cursor.execute("SELECT COUNT(*) AS cnt FROM product_variants WHERE product_id=%s", (product_id,))
        variant_count = cursor.fetchone()
        if int((variant_count or {}).get("cnt") or 0) > 0:
            cursor.close()
            return {"status": "error", "message": "Please choose an available item option"}, 400
        cursor.execute("SELECT stock FROM products WHERE id=%s", (product_id,))
        product = cursor.fetchone()
        if not product or product["stock"] < qty:
            cursor.close()
            return {"status": "error", "message": "Insufficient stock for this product"}, 400

    if variant_id is None:
        cursor.execute("""
            SELECT id, quantity FROM cart
            WHERE user_id=%s AND product_id=%s AND variant_id IS NULL
        """, (session["user_id"], product_id))
    else:
        cursor.execute("""
            SELECT id, quantity FROM cart
            WHERE user_id=%s AND product_id=%s AND variant_id=%s
        """, (session["user_id"], product_id, variant_id))
    existing = cursor.fetchone()

    if existing:
        new_qty = existing["quantity"] + qty
        # Check if new quantity exceeds available stock
        if variant_id:
            cursor.execute("SELECT stock FROM product_variants WHERE id=%s AND product_id=%s", (variant_id, product_id))
            variant = cursor.fetchone()
            if not variant or variant["stock"] < new_qty:
                cursor.close()
                return {"status": "error", "message": f"Cannot add more. Only {(variant or {}).get('stock', 0)} in stock"}, 400
        else:
            cursor.execute("SELECT stock FROM products WHERE id=%s", (product_id,))
            product = cursor.fetchone()
            if product["stock"] < new_qty:
                cursor.close()
                return {"status": "error", "message": f"Cannot add more. Only {product['stock']} in stock"}, 400
        cursor.execute("UPDATE cart SET quantity=%s WHERE id=%s", (new_qty, existing["id"]))
    else:
        cursor.execute("""
            INSERT INTO cart (user_id, product_id, variant_id, quantity)
            VALUES (%s, %s, %s, %s)
        """, (session["user_id"], product_id, variant_id, qty))

    db.commit()
    cursor.close()

    return {"status": "success", "message": "Added to cart"}


@app.route('/message/<int:seller_id>')
def message_seller(seller_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to message sellers', 'error')
        return redirect(url_for('loginreg'))

    product_id = request.args.get('product_id', type=int)
    cursor = db.cursor(dictionary=True)
    convo = None
    # Use shared helper to find or create a conversation
    convo = get_or_create_conversation(seller_id, user_id, product_id)
    if not convo:
        # If the conversations table does not have product_id, attempt to insert without it
        try:
            cursor.execute("INSERT INTO conversations (seller_id, customer_id, product_id, created_at, updated_at) VALUES (%s, %s, %s, NOW(), NOW())", (seller_id, user_id, product_id))
        except Exception:
            cursor.execute("INSERT INTO conversations (seller_id, customer_id, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())", (seller_id, user_id))
        db.commit()
        # Fetch created conversation; prefer product-specific if possible
        try:
            if product_id is not None:
                cursor.execute("SELECT * FROM conversations WHERE seller_id=%s AND customer_id=%s AND product_id=%s ORDER BY updated_at DESC LIMIT 1", (seller_id, user_id, product_id))
            else:
                cursor.execute("SELECT * FROM conversations WHERE seller_id=%s AND customer_id=%s ORDER BY updated_at DESC LIMIT 1", (seller_id, user_id))
        except Exception:
            cursor.execute("SELECT * FROM conversations WHERE seller_id=%s AND customer_id=%s ORDER BY updated_at DESC LIMIT 1", (seller_id, user_id))
        convo = cursor.fetchone()

    cursor.execute("SELECT u.id, u.fullname, s.business_name, u.email FROM users u LEFT JOIN sellers s ON s.user_id = u.id WHERE u.id = %s", (seller_id,))
    srow = cursor.fetchone()
    seller = None
    if srow:
        seller = {'id': srow.get('id'), 'name': srow.get('business_name') or srow.get('fullname'), 'email': srow.get('email')}

    # load messages
    messages = []
    try:
        cursor.execute("SELECT m.*, u.fullname as sender_name FROM messages m LEFT JOIN users u ON u.id = m.sender_id WHERE m.conversation_id=%s ORDER BY m.created_at ASC", (convo['id'],))
        messages = cursor.fetchall() or []
    except Exception:
        messages = []

    cursor.close()
    # Redirect to inbox with this conversation selected for unified inbox UX
    if convo:
        return redirect(url_for('conversations') + f"?convo_id={convo.get('id')}")
    else:
        return redirect(url_for('conversations'))


@app.route('/message/send', methods=['POST'])
def message_send():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    # Handle both JSON and form-urlencoded data
    data = request.get_json() or request.form or {}
    convo_id = data.get('conversation_id')
    body = data.get('body', '').strip()
    if not convo_id or not body:
        return jsonify({'success': False, 'error': 'conversation_id and body required'}), 400

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM conversations WHERE id=%s", (convo_id,))
    convo = cursor.fetchone()
    if not convo:
        cursor.close()
        return jsonify({'success': False, 'error': 'Conversation not found'}), 404

    role = session.get('role') or 'customer'
    sender_role = 'customer'
    if role == 'seller' and convo.get('seller_id') == user_id:
        sender_role = 'seller'
    elif role == 'admin':
        sender_role = 'admin'
    elif role == 'rider':
        sender_role = 'rider'
    else:
        sender_role = 'customer'

    # Ensure user is participant
    if sender_role == 'customer' and convo.get('customer_id') != user_id:
        cursor.close()
        return jsonify({'success': False, 'error': 'Not allowed to post'}), 403
    if sender_role == 'seller' and convo.get('seller_id') != user_id:
        cursor.close()
        return jsonify({'success': False, 'error': 'Not allowed to post'}), 403

    try:
        print(f"[debug] message_send called: user={user_id} convo_id={convo_id} body_len={len(body)}")
        cur = db.cursor()
        cur.execute("INSERT INTO messages (conversation_id, sender_id, sender_role, body, created_at) VALUES (%s, %s, %s, %s, NOW())", (convo_id, user_id, sender_role, body))
        db.commit()
        msg_id = cur.lastrowid
        cur.execute("UPDATE conversations SET updated_at = NOW(), last_message = %s WHERE id=%s", (body[:255], convo_id))
        db.commit()
        cur.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Failed to send message'}), 500

    # create notification for other party
    other_id = convo.get('seller_id') if convo.get('customer_id') == user_id else convo.get('customer_id')
    if other_id:
        try:
            nt = db.cursor()
            target_pid = convo.get('product_id') if convo and convo.get('product_id') else ''
            target_url = f"/message/{convo.get('seller_id')}" + (f"?product_id={target_pid}" if target_pid else '')
            nt.execute("INSERT INTO notifications (`type`, `message`, `target_url`, `created_at`) VALUES (%s, %s, %s, NOW())", ("message", f"New message: {body[:120]}", target_url))
            db.commit()
            nt.close()
        except Exception:
            pass

    return jsonify({'success': True, 'message_id': msg_id})


@app.route('/api/conversations/<int:convo_id>/messages')
def api_conversation_messages(convo_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM conversations WHERE id=%s", (convo_id,))
    convo = cursor.fetchone()
    if not convo:
        cursor.close()
        return jsonify({'success': False, 'error': 'Not found'}), 404
    role = session.get('role')
    if role == 'admin' or convo.get('customer_id') == user_id or convo.get('seller_id') == user_id:
        messages = fetch_conversation_messages(convo_id)
        # Get conversation metadata
        convo_dict = conversation_to_dict(convo, user_id)
        cursor.close()
        try:
            cur = db.cursor()
            cur.execute("UPDATE messages SET is_read=1 WHERE conversation_id=%s AND sender_id!=%s", (convo_id, user_id))
            db.commit()
            cur.close()
        except Exception:
            pass
        # Add is_mine flag to messages for proper styling
        for msg in messages:
            msg['is_mine'] = msg.get('sender_id') == user_id
        return jsonify({'success': True, 'messages': messages, 'conversation': convo_dict})
    else:
        cursor.close()
        return jsonify({'success': False, 'error': 'Not allowed'}), 403


@app.route('/api/conversations')
def api_conversations():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    # use helper to fetch convos
    try:
        conversations = fetch_user_conversations(user_id)
        return jsonify({'success': True, 'conversations': conversations})
    except Exception:
        return jsonify({'success': False, 'conversations': []}), 500


@app.route('/api/conversations/create', methods=['POST'])
def api_create_conversation():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    data = request.get_json() or {}
    other_id = data.get('other_id')
    product_id = data.get('product_id')
    if not other_id:
        return jsonify({'success': False, 'error': 'other_id is required'}), 400
    cursor = db.cursor(dictionary=True)
    # Determine roles and create conversation accordingly
    # If current user is seller or customer, map other as opposite
    role = session.get('role') or 'customer'
    seller_id = None
    customer_id = None
    if role == 'seller':
        seller_id = session.get('user_id')
        customer_id = int(other_id)
    else:
        seller_id = int(other_id)
        customer_id = session.get('user_id')
    try:
        convo = get_or_create_conversation(seller_id, customer_id, product_id)
        if convo:
            print(f"[debug] api_create_conversation: created/returned convo id={convo.get('id')} seller={seller_id} customer={customer_id} product_id={product_id}")
    except Exception as e:
        return jsonify({'success': False, 'error': 'Database error'}), 500
    cursor.close()
    if convo:
        return jsonify({'success': True, 'conversation_id': convo.get('id')})
    else:
        return jsonify({'success': False, 'error': 'Failed to create conversation'}), 500


@app.route('/conversations', endpoint='conversations')
def conversations():
    # Show the inbox page
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in', 'error')
        return redirect(url_for('loginreg'))
    return render_template('conversations.html')

@app.route('/seller/chat/<int:customer_id>', methods=['GET', 'POST'])
def seller_chat_customer(customer_id):
    """Allow seller to initiate or continue conversation with a customer"""
    user_id = session.get('user_id')
    role = session.get('role')
    
    # Only sellers can access this route
    if not user_id or role != 'seller':
        flash('Unauthorized access', 'error')
        return redirect(url_for('loginreg'))
    
    product_id = request.args.get('product_id', type=int)
    
    # Get or create conversation
    convo = get_or_create_conversation(seller_id=user_id, customer_id=customer_id, product_id=product_id)
    
    if not convo:
        flash('Could not create conversation', 'error')
        return redirect(url_for('seller'))
    
    # Fetch messages
    messages = fetch_conversation_messages(convo.get('id'))
    
    # Fetch customer info
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT id, fullname, profile_pic FROM users WHERE id=%s", (customer_id,))
    customer = cursor.fetchone()
    if customer and customer.get("profile_pic"):
        customer["profile_pic"] = media_url(customer.get("profile_pic"), "image/default-avatar.png")
    
    # Fetch product info if specified
    product = None
    if product_id:
        cursor.execute("SELECT id, name FROM products WHERE id=%s AND seller_id=%s", (product_id, user_id))
        product = cursor.fetchone()
    
    cursor.close()
    
    return render_template(
        'seller_chat.html',
        conversation_id=convo.get('id'),
        customer=customer,
        product=product,
        messages=messages
    )

@app.route('/api/seller/customers', methods=['GET'])
def api_seller_customers():
    """Get list of customers seller can chat with (customers who bought from them)"""
    user_id = session.get('user_id')
    role = session.get('role')
    
    if not user_id or role != 'seller':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    cursor = db.cursor(dictionary=True)
    try:
        # Get customers who have purchased from this seller
        cursor.execute("""
            SELECT DISTINCT u.id, u.fullname, u.profile_pic
            FROM users u
            INNER JOIN orders o ON o.customer_id = u.id
            INNER JOIN products p ON p.id = o.product_id
            WHERE p.seller_id = %s
            ORDER BY u.fullname
        """, (user_id,))
        
        customers = cursor.fetchall() or []
        for customer in customers:
            customer["profile_pic"] = media_url(customer.get("profile_pic"), "image/default-avatar.png") if customer.get("profile_pic") else None
        return jsonify({'success': True, 'customers': customers})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()

@app.route("/cart_count")
def cart_count():
    if "user_id" not in session:
        return {"count": 0}

    cursor = db.cursor()
    cursor.execute("SELECT SUM(quantity) FROM cart WHERE user_id=%s", (session["user_id"],))
    result = cursor.fetchone()
    count = result[0] if result and result[0] else 0
    cursor.close()
    return {"count": count}

@app.route("/cart")
def cart():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in to view your cart.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor(dictionary=True)

    # Fetch user info
    cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
    user_info = cursor.fetchone()

    # Fetch customer's default address
    cursor.execute("""
        SELECT barangay_name, city_name, province_name, region_name
        FROM addresses
        WHERE user_id = %s AND is_default = 1
    """, (user_id,))
    customer_address = cursor.fetchone()
    if not customer_address:
        flash("Please set a default address to calculate shipping.", "error")
        return redirect(url_for("settings"))

    # Fetch cart items with seller addresses
    cursor.execute("""
        SELECT 
            IFNULL(s.business_name, 'Unknown Shop') AS business_name,
            p.id AS product_id,
            p.name AS product_name,
            p.image AS product_image,
            pv.id AS variant_id,
            pv.color,
            pv.size,
            IFNULL(pv.price, p.price) AS price,
            IFNULL(pv.weight_kg, 0) AS weight_kg,
            c.quantity,
            p.seller_id,
            a.barangay_name AS seller_barangay,
            a.city_name AS seller_city,
            a.province_name AS seller_province,
            a.region_name AS seller_region
        FROM cart c
        JOIN products p ON c.product_id = p.id
        LEFT JOIN product_variants pv ON c.variant_id = pv.id
        LEFT JOIN sellers s ON s.user_id = p.seller_id
        LEFT JOIN addresses a ON a.user_id = p.seller_id AND 
            (a.is_default = 1 OR a.id = (
                SELECT id FROM addresses 
                WHERE user_id = p.seller_id 
                ORDER BY is_default DESC, created_at ASC LIMIT 1
            ))
        WHERE c.user_id = %s
        ORDER BY s.business_name, p.name
    """, (user_id,))
    rows = cursor.fetchall()

    # Only calculate shipping for selected items (frontend will send selected_ids via session)
    selected_items = session.get("selected_cart_items", [])
    filtered_rows = []
    for row in rows:
        identifier = f"{row['product_id']}:{row['variant_id']}" if row['variant_id'] else str(row['product_id'])
        if identifier in selected_items:
            filtered_rows.append(row)

    # Group items by shop and calculate estimated shipping
    cart_by_shop = {}
    shipping_by_shop = {}
    vehicle_by_shop = {}
    for row in rows:
        shop_name = row["business_name"]
        if shop_name not in cart_by_shop:
            cart_by_shop[shop_name] = []

            seller_address = {
                "barangay_name": row["seller_barangay"] or "",
                "city_name": row["seller_city"] or "",
                "province_name": row["seller_province"] or "",
                "region_name": row.get("seller_region") or ""
            }

            # Calculate total weight for selected items only
            total_weight = sum(
                r["weight_kg"] * r["quantity"] 
                for r in filtered_rows if r["business_name"] == shop_name
            )

            # Estimated shipping based on area + weight + vehicle
            shipping_by_shop[shop_name] = calculate_shipping(customer_address, seller_address, total_weight)
            vehicle_by_shop[shop_name] = assign_vehicle(total_weight)

        row["shipping"] = shipping_by_shop[shop_name]
        row["vehicle"] = vehicle_by_shop[shop_name]
        cart_by_shop[shop_name].append(row)

    subtotal = sum(item["price"] * item["quantity"] for item in rows)
    total_shipping = sum(shipping_by_shop.values())
    total = subtotal + total_shipping

    # Fetch customer-targeted notifications (tagged with [customer:<id>])
    customer_notifications = []
    if user_id:
        try:
            notif_cursor = db.cursor(dictionary=True)
            tag = f"[customer:{user_id}]"
            notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
            customer_notifications = notif_cursor.fetchall() or []
            notif_cursor.close()
        except Exception:
            customer_notifications = []

    return render_template(
        "cart.html",
        cart_by_shop=cart_by_shop,
        subtotal=subtotal,
        total=total,
        total_shipping=total_shipping,
        shipping_by_shop=shipping_by_shop,
        vehicle_by_shop=vehicle_by_shop,
        user=user_info,
        notifications=customer_notifications
    )

@app.route('/update_cart_quantity', methods=['POST'])
def update_cart_quantity():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    product_id = data.get('product_id')
    variant_id = data.get('variant_id')
    quantity = data.get('quantity')

    cursor = db.cursor(dictionary=True)

    # Check stock availability before updating
    if variant_id:
        cursor.execute("SELECT stock FROM product_variants WHERE id=%s", (variant_id,))
        variant = cursor.fetchone()
        if not variant or variant["stock"] < quantity:
            cursor.close()
            return jsonify({'success': False, 'error': f'Cannot add more. Only {variant["stock"] if variant else 0} in stock'}), 400
        
        cursor = db.cursor()
        cursor.execute("""
            UPDATE cart 
            SET quantity = %s 
            WHERE user_id = %s AND product_id = %s AND variant_id = %s
        """, (quantity, user_id, product_id, variant_id))
    else:
        cursor.execute("SELECT stock FROM products WHERE id=%s", (product_id,))
        product = cursor.fetchone()
        if not product or product["stock"] < quantity:
            cursor.close()
            return jsonify({'success': False, 'error': f'Cannot add more. Only {product["stock"] if product else 0} in stock'}), 400
        
        cursor = db.cursor()
        cursor.execute("""
            UPDATE cart 
            SET quantity = %s 
            WHERE user_id = %s AND product_id = %s AND variant_id IS NULL
        """, (quantity, user_id, product_id))

    db.commit()
    cursor.close()
    return jsonify({'success': True})

@app.route('/remove_cart_item', methods=['POST'])
def remove_cart_item():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    product_id = data.get('product_id')
    variant_id = data.get('variant_id')

    # normalize variant_id: treat empty, '0', 0 as NULL (no variant)
    if variant_id in (None, '', 0, '0'):
        variant_id = None
    else:
        try:
            variant_id = int(variant_id)
        except Exception:
            variant_id = None

    cursor = db.cursor()
    if variant_id is not None:
        cursor.execute("""
            DELETE FROM cart 
            WHERE user_id = %s AND product_id = %s AND variant_id = %s
        """, (user_id, product_id, variant_id))
    else:
        cursor.execute("""
            DELETE FROM cart 
            WHERE user_id = %s AND product_id = %s AND variant_id IS NULL
        """, (user_id, product_id))

    db.commit()
    return jsonify({'success': True})

@app.route("/checkout", methods=["GET"])
def checkout_get():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in", "error")
        return redirect(url_for("loginreg"))

    selected_items = session.get("checkout_items")
    if not selected_items:
        flash("No items selected", "error")
        return redirect(url_for("cart"))

    cursor = db.cursor(dictionary=True)

    # Fetch user info
    cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
    user_info = cursor.fetchone()

    # Fetch customer's default address
    cursor.execute("""
        SELECT id AS address_id, street, barangay_name AS barangay, city_name AS city, 
               province_name AS province, region_name AS region
        FROM addresses
        WHERE user_id = %s AND is_default = 1
    """, (user_id,))
    customer_address = cursor.fetchone()
    if not customer_address:
        flash("No default address set", "error")
        return redirect(url_for("cart"))

    # Fetch cart items
    cursor.execute("""
        SELECT 
            IFNULL(s.business_name, 'Unknown Shop') AS business_name,
            p.id AS product_id,
            p.name AS product_name,
            pv.id AS variant_id,
            pv.color,
            pv.size,
            IFNULL(pv.price, p.price) AS price,
            IFNULL(pv.weight_kg, 0) AS weight_kg,
            c.quantity,
            p.image AS product_image,
            p.seller_id,
            a.barangay_name AS seller_barangay,
            a.city_name AS seller_city,
            a.province_name AS seller_province,
            a.region_name AS seller_region
        FROM cart c
        JOIN products p ON c.product_id = p.id
        LEFT JOIN product_variants pv ON c.variant_id = pv.id
        LEFT JOIN sellers s ON s.user_id = p.seller_id
        LEFT JOIN addresses a ON a.user_id = p.seller_id AND 
             (a.is_default = 1 OR a.id = (
                 SELECT id FROM addresses 
                 WHERE user_id = p.seller_id 
                 ORDER BY is_default DESC, created_at ASC LIMIT 1
             ))
        WHERE c.user_id = %s
    """, (user_id,))
    rows = cursor.fetchall()

    # Filter only selected items
    filtered_rows = []
    for row in rows:
        identifier = f"{row['product_id']}:{row['variant_id']}" if row['variant_id'] else str(row['product_id'])
        if identifier in selected_items:
            filtered_rows.append(row)

    # Group items by shop
    items_by_shop = {}
    shipping_by_shop = {}
    vehicle_by_shop = {}
    for row in filtered_rows:
        shop_name = row["business_name"]
        if shop_name not in items_by_shop:
            items_by_shop[shop_name] = []

            seller_address = {
                "barangay_name": row["seller_barangay"] or "",
                "city_name": row["seller_city"] or "",
                "province_name": row["seller_province"] or "",
                "region_name": row.get("seller_region") or ""
            }

            calc_customer_addr = {
                "barangay_name": customer_address.get("barangay", ""),
                "city_name": customer_address.get("city", ""),
                "province_name": customer_address.get("province", ""),
                "region_name": customer_address.get("region", "")
            }

            # Total weight per shop
            total_weight = sum(
                float(r["weight_kg"]) * int(r["quantity"])
                for r in filtered_rows if r["business_name"] == shop_name
            )

            # Calculate shipping per shop using your weight+vehicle logic
            shipping_by_shop[shop_name] = calculate_shipping(calc_customer_addr, seller_address, total_weight)
            vehicle_by_shop[shop_name] = assign_vehicle(total_weight)

        row["shipping"] = shipping_by_shop[shop_name]
        row["vehicle"] = vehicle_by_shop[shop_name]
        items_by_shop[shop_name].append(row)

    # Totals
    subtotal = sum(float(item["price"]) * int(item["quantity"]) for items in items_by_shop.values() for item in items)
    total_shipping = sum(shipping_by_shop.values())
    total = subtotal + total_shipping

    return render_template(
        "checkout.html",
        user=user_info,
        address=customer_address,
        items_by_shop=items_by_shop,
        subtotal=subtotal,
        shipping=total_shipping,
        total=total,
        shipping_by_shop=shipping_by_shop,
        vehicle_by_shop=vehicle_by_shop
    )

@app.route("/checkout", methods=["POST"])
def checkout_post():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in", "error")
        return redirect(url_for("loginreg"))

    import json
    data = request.get_json()
    if not data or "items" not in data:
        flash("No items selected", "error")
        return redirect(url_for("cart"))

    try:
        selected_items = json.loads(data["items"])
    except:
        flash("Invalid items selection", "error")
        return redirect(url_for("cart"))

    # Save to session
    session["checkout_items"] = selected_items
    return jsonify({"success": True, "redirect": url_for("checkout_get")})

@app.route("/place_order", methods=["POST"])
def place_order():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in"}), 401

    # Get messages from JSON body
    data = request.get_json()
    messages = data.get("messages", {}) if data else {}
    selected_items = session.get("checkout_items")
    if not selected_items:
        return jsonify({"error": "No items selected"}), 400

    cursor = db.cursor(dictionary=True)

    # Fetch cart items grouped by shop
    cursor.execute("""
        SELECT 
            IFNULL(s.business_name, 'Unknown Shop') AS business_name,
            p.id AS product_id,
            pv.id AS variant_id,
            IFNULL(pv.price, p.price) AS price,
            c.quantity,
            p.seller_id,
            IFNULL(pv.weight_kg, 0) AS weight_kg,
            a.barangay_name AS seller_barangay,
            a.city_name AS seller_city,
            a.province_name AS seller_province,
            a.region_name AS seller_region
        FROM cart c
        JOIN products p ON c.product_id = p.id
        LEFT JOIN product_variants pv ON c.variant_id = pv.id
        LEFT JOIN sellers s ON s.user_id = p.seller_id
        LEFT JOIN addresses a ON a.user_id = p.seller_id AND 
             (a.is_default = 1 OR a.id = (
                 SELECT id FROM addresses 
                 WHERE user_id = p.seller_id 
                 ORDER BY is_default DESC, created_at ASC LIMIT 1
             ))
        WHERE c.user_id = %s
    """, (user_id,))
    rows = cursor.fetchall()

    # Filter selected items
    filtered_rows = []
    for row in rows:
        identifier = f"{row['product_id']}:{row['variant_id']}" if row['variant_id'] else str(row['product_id'])
        if identifier in selected_items:
            filtered_rows.append(row)

    # Validate stock availability for all items before placing order
    for item in filtered_rows:
        if item["variant_id"]:
            cursor.execute("SELECT stock FROM product_variants WHERE id=%s", (item["variant_id"],))
            variant = cursor.fetchone()
            if not variant or variant["stock"] < item["quantity"]:
                cursor.close()
                return jsonify({"error": f"Insufficient stock. A variant is no longer available in the quantity you selected."}), 409
        else:
            cursor.execute("SELECT COUNT(*) AS cnt FROM product_variants WHERE product_id=%s", (item["product_id"],))
            variant_count = cursor.fetchone()
            if int((variant_count or {}).get("cnt") or 0) > 0:
                cursor.close()
                return jsonify({"error": "Please remove this item from your cart and choose an available item option again."}), 409
            cursor.execute("SELECT stock FROM products WHERE id=%s", (item["product_id"],))
            product = cursor.fetchone()
            if not product or product["stock"] < item["quantity"]:
                cursor.close()
                return jsonify({"error": f"Insufficient stock. A product is no longer available in the quantity you selected."}), 409

    # Group by shop
    items_by_shop = {}
    for row in filtered_rows:
        shop = row["business_name"]
        if shop not in items_by_shop:
            items_by_shop[shop] = []
        items_by_shop[shop].append(row)


    # Fetch customer address
    cursor.execute("SELECT * FROM addresses WHERE user_id = %s AND is_default = 1 LIMIT 1", (user_id,))
    customer_address = cursor.fetchone()
    if not customer_address:
        return jsonify({"error": "No default address set"}), 400

    # Insert orders per shop
    for shop, items in items_by_shop.items():
        seller_id = items[0]["seller_id"]
        total = sum(item["price"] * item["quantity"] for item in items)
        total_weight = sum(item["weight_kg"] * item["quantity"] for item in items)

        # Build seller address from first item
        seller_address = {
            "barangay_name": items[0].get("seller_barangay", ""),
            "city_name": items[0].get("seller_city", ""),
            "province_name": items[0].get("seller_province", ""),
            "region_name": items[0].get("seller_region") or ""
        }
        # Build customer address dict
        calc_customer_addr = {
            "barangay_name": customer_address.get("barangay", ""),
            "city_name": customer_address.get("city", ""),
            "province_name": customer_address.get("province", ""),
            "region_name": customer_address.get("region", "")
        }

        shipping_fee = calculate_shipping(calc_customer_addr, seller_address, total_weight)
        vehicle = assign_vehicle(total_weight)
        message = messages.get(shop, "")

        import json
        # Serialize full address snapshots from default address for customer and seller
        customer_snapshot = ', '.join(filter(None, [
            customer_address.get("province_name", ""),
            customer_address.get("city_name", ""),
            customer_address.get("barangay_name", ""),
            customer_address.get("street", "")
        ]))
        # Always fetch seller's default address
        seller_user_id = seller_id
        cursor.execute("SELECT * FROM addresses WHERE user_id = %s AND is_default = 1 LIMIT 1", (seller_user_id,))
        seller_address_row = cursor.fetchone()
        seller_snapshot = ', '.join(filter(None, [
            seller_address_row.get("province_name", "") if seller_address_row else "",
            seller_address_row.get("city_name", "") if seller_address_row else "",
            seller_address_row.get("barangay_name", "") if seller_address_row else "",
            seller_address_row.get("street", "") if seller_address_row else ""
        ]))
        cursor.execute("""
            INSERT INTO orders (user_id, customer_address_snapshot, seller_address_snapshot, seller_id, message, vehicle, shipping_fee, total, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (user_id, customer_snapshot, seller_snapshot, seller_id, message, vehicle, shipping_fee, total, "processing"))
        order_id = cursor.lastrowid

        # Create notifications for admin and seller (tag seller in message)
        try:
            # Admin notification
            cursor.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("order", f"New order #{order_id} placed by user {user_id} for shop {shop} (₱{total})", f"/admin?order_id={order_id}")
            )
            # Seller-specific notification includes a tag so seller can find it
            seller_msg = f"[seller:{seller_id}] New order #{order_id} for your shop '{shop}' — total ₱{total}"
            cursor.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("order_seller", seller_msg, f"/seller?order_id={order_id}")
            )
            # Customer notification so the buyer knows their order was placed
            customer_msg = f"[customer:{user_id}] Your order #{order_id} has been placed — total ₱{total}"
            cursor.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("order_customer", customer_msg, f"/orders?order_id={order_id}")
            )
        except Exception:
            # don't block order flow on notification failure
            pass

        # Insert order items with a snapshot of product.seller_id
        for item in items:
            # fetch current seller_id for this product
            pcur = db.cursor(dictionary=True)
            pcur.execute("SELECT seller_id FROM products WHERE id=%s", (item["product_id"],))
            prow = pcur.fetchone()
            pcur.close()
            seller_snapshot = prow.get('seller_id') if prow else None
            cursor.execute("""
                INSERT INTO order_items (order_id, product_id, variant_id, quantity, price, seller_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (order_id, item["product_id"], item["variant_id"], item["quantity"], item["price"], seller_snapshot))

        # Reduce stock and alert the seller if the product crosses its threshold.
        for item in items:
            try:
                decrement_ordered_stock(item, cursor)
            except ValueError as exc:
                db.rollback()
                cursor.close()
                return jsonify({"error": str(exc)}), 409

    # Clear only the items that were ordered from cart
    for item in filtered_rows:
        if item["variant_id"]:
            cursor.execute(
                "DELETE FROM cart WHERE user_id = %s AND product_id = %s AND variant_id = %s",
                (user_id, item["product_id"], item["variant_id"])
            )
        else:
            cursor.execute(
                "DELETE FROM cart WHERE user_id = %s AND product_id = %s AND variant_id IS NULL",
                (user_id, item["product_id"])
            )
    db.commit()

    session.pop("checkout_items", None)
    return jsonify({"success": True, "message": "Order placed successfully!"})

@app.route("/wishlist")
def wishlist():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in to view your wishlist.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor(dictionary=True)

    # Fetch user info
    cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
    user_info = cursor.fetchone()

    # Fetch wishlist items with product details
    cursor.execute("""
        SELECT 
            w.product_id,
            p.name,
            p.price,
            p.image,
            p.status
        FROM wishlist w
        JOIN products p ON w.product_id = p.id
        WHERE w.user_id = %s
          AND p.status != 'deleted'
          AND p.status != 'archived'
    """, (user_id,))
    wishlist_items = cursor.fetchall()

    # Attach ratings
    product_ids = [item['product_id'] for item in wishlist_items]
    ratings_map = {}
    if product_ids:
        placeholders = ','.join(['%s'] * len(product_ids))
        sql = f"SELECT product_id, AVG(rating) AS avg_rating, COUNT(*) AS cnt FROM product_ratings WHERE product_id IN ({placeholders}) GROUP BY product_id"
        params = tuple(product_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in product_ratings query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        rows = cursor.fetchall()
        for r in rows:
            ratings_map[r['product_id']] = {'avg': float(r.get('avg_rating') or 0), 'cnt': int(r.get('cnt') or 0)}
    for item in wishlist_items:
        r = ratings_map.get(item['product_id'])
        item['rating'] = round(r['avg'], 2) if r else 0.0
        item['rating_count'] = r['cnt'] if r else 0
    cursor.close()

    # Fetch customer-targeted notifications (tagged with [customer:<id>])
    customer_notifications = []
    try:
        notif_cursor = db.cursor(dictionary=True)
        tag = f"[customer:{user_id}]"
        notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
        customer_notifications = notif_cursor.fetchall() or []
        notif_cursor.close()
    except Exception:
        customer_notifications = []

    return render_template(
        "wishlist.html",
        user=user_info,
        wishlist_items=wishlist_items,
        notifications=customer_notifications
    )

@app.route("/wishlist/add/<int:product_id>", methods=["POST"])
def add_to_wishlist(product_id):
    user_id = session.get("user_id")

    if not user_id:
        return jsonify({"success": False, "message": "Login required"}), 401

    cursor = db.cursor()
    cursor.execute("""
        INSERT INTO wishlist (user_id, product_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id, product_id) DO NOTHING
    """, (user_id, product_id))
    db.commit()
    cursor.close()

    return jsonify({"success": True})

@app.route("/wishlist/remove/<int:product_id>", methods=["POST"])
def remove_from_wishlist(product_id):
    user_id = session.get("user_id")

    if not user_id:
        return jsonify({"success": False, "message": "Login required"}), 401

    cursor = db.cursor()
    cursor.execute("""
        DELETE FROM wishlist 
        WHERE user_id = %s AND product_id = %s
    """, (user_id, product_id))
    db.commit()
    cursor.close()

    return jsonify({"success": True})

@app.route("/wishlist/data")
def wishlist_data():
    user_id = session.get("user_id")

    if not user_id:
        return jsonify([])

    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT product_id
        FROM wishlist
        WHERE user_id = %s
    """, (user_id,))
    results = cursor.fetchall()
    cursor.close()

    return jsonify([row["product_id"] for row in results])

@app.route("/order/update_status", methods=["POST"])
def update_order_status():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    order_id = request.form.get("order_id")
    new_status = request.form.get("new_status")
    if not order_id or not new_status:
        return jsonify({"success": False, "error": "Missing data"}), 400

    cursor = db.cursor(dictionary=True)
    # Fetch order to check permissions
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404

    # Allow update if requester is the customer who placed the order,
    # or the seller assigned to the order, or an admin.
    allowed = False
    if order.get('user_id') == user_id:
        allowed = True
    elif order.get('seller_id') == user_id:
        allowed = True
    elif session.get('role') == 'admin':
        allowed = True

    if not allowed:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403

    # Perform status update
    try:
        cursor2 = db.cursor()
        cursor2.execute("UPDATE orders SET status=%s WHERE id=%s", (new_status, order_id))
        db.commit()
        cursor2.close()

        # Create notifications for admin and seller about this status change
        try:
            notif_cur = db.cursor()
            # Admin notification
            admin_msg = f"Order #{order_id} status changed to {new_status} by {session.get('role') or 'user'}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            # Seller notification (tagged)
            seller_id = order.get('seller_id')
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Order #{order_id} status changed to {new_status}"
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            # Customer notification
            customer_id = order.get('user_id')
            if customer_id:
                customer_msg = f"[customer:{customer_id}] Your order #{order_id} status is now {new_status}"
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
            # Rider notifications: when order becomes available for pickup, notify riders matching vehicle type
            if new_status == 'ready_for_pickup':
                try:
                    # If this is a return flow, set pickup requested in return_requests table
                    try:
                        pcur = db.cursor()
                        pcur.execute("UPDATE return_requests SET status=%s, pickup_requested_at=NOW() WHERE order_id=%s", ('pickup_requested', order_id))
                        db.commit()
                        pcur.close()
                    except Exception:
                        try:
                            pcur.close()
                        except Exception:
                            pass
                    vehicle_type = order.get('vehicle') or order.get('vehicle_type')
                    if vehicle_type:
                        # find all riders with this vehicle_type
                        rcur = db.cursor(dictionary=True)
                        rcur.execute("SELECT user_id FROM riders WHERE vehicle_type = %s", (vehicle_type,))
                        riders = rcur.fetchall() or []
                        rcur.close()
                        for r in riders:
                            rid = r.get('user_id')
                            if not rid:
                                continue
                            # If this order is part of a return flow, notify riders for return pickup
                            if order and order.get('refund_requested_at'):
                                rider_msg = f"[rider:{rid}] New return pickup available: Order #{order_id}."
                            else:
                                rider_msg = f"[rider:{rid}] New delivery available: Order #{order_id}."
                            # Prevent duplicate notifications for same order & rider
                            chk = db.cursor()
                            chk.execute("SELECT COUNT(*) FROM notifications WHERE `type` = %s AND message = %s", ("order_rider", rider_msg))
                            exists = chk.fetchone()[0]
                            chk.close()
                            if exists == 0:
                                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_rider", rider_msg, f"/rider?order_id={order_id}"))
                except Exception:
                    # ignore rider notification failures to not block the main update
                    pass
            # If order becomes 'completed', record income as well
            # If order becomes 'delivered', record rider income on delivery (rider pocket), admin/seller when completed
            if new_status == 'completed':
                notify_rider_order_completed(order_id, cursor=notif_cur)

            if new_status == 'delivered':
                try:
                    inc_cur = db.cursor(dictionary=True)
                    inc_cur.execute("SELECT shipping_fee FROM orders WHERE id = %s", (order_id,))
                    orow = inc_cur.fetchone()
                    inc_cur.close()
                    if orow:
                        shipping_fee = float(orow.get('shipping_fee', 0) or 0)
                        # find rider assigned
                        rcur = db.cursor(dictionary=True)
                        rcur.execute('SELECT rider_id FROM order_riders WHERE order_id=%s LIMIT 1', (order_id,))
                        rr = rcur.fetchone()
                        rcur.close()
                        if rr and rr.get('rider_id'):
                            rider_id_val = rr.get('rider_id')
                            # Insert rider income if not already present
                            rins = db.cursor()
                            rins.execute("SELECT COUNT(*) FROM income WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, rider_id_val, 'rider'))
                            if rins.fetchone()[0] == 0:
                                rins.execute("INSERT INTO income (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, rider_id_val, 'rider', shipping_fee, f"Order #{order_id} delivery fee (on delivered)"))
                                # Also record in the new earnings table for reporting
                                rins.execute("SELECT COUNT(*) FROM earnings WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, rider_id_val, 'rider'))
                                if rins.fetchone()[0] == 0:
                                    rins.execute("INSERT INTO earnings (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, rider_id_val, 'rider', shipping_fee, f"Order #{order_id} delivery fee (on delivered)"))
                                db.commit()
                            rins.close()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass

            if new_status == 'completed':
                try:
                    inc_cur = db.cursor(dictionary=True)
                    inc_cur.execute("SELECT total, seller_id, shipping_fee FROM orders WHERE id = %s", (order_id,))
                    orow = inc_cur.fetchone()
                    inc_cur.close()
                    if orow:
                        total_amount = float(orow.get('total', 0))
                        seller_id = orow.get('seller_id')
                        shipping_fee = float(orow.get('shipping_fee', 0) or 0)
                        admin_id = 1
                        seller_income = total_amount * 0.95
                        admin_income = total_amount * 0.05
                        rider_income = shipping_fee
                        cursor3 = db.cursor()
                        if seller_id:
                            cursor3.execute("SELECT COUNT(*) FROM income WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, seller_id, 'seller'))
                            if cursor3.fetchone()[0] == 0:
                                cursor3.execute("INSERT INTO income (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, seller_id, 'seller', seller_income, f"Order #{order_id} income (95%)"))
                                # mirror into earnings table
                                cursor3.execute("SELECT COUNT(*) FROM earnings WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, seller_id, 'seller'))
                                if cursor3.fetchone()[0] == 0:
                                    cursor3.execute("INSERT INTO earnings (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, seller_id, 'seller', seller_income, f"Order #{order_id} income (95%)"))
                        cursor3.execute("SELECT COUNT(*) FROM income WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, admin_id, 'admin'))
                        if cursor3.fetchone()[0] == 0:
                            cursor3.execute("INSERT INTO income (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, admin_id, 'admin', admin_income, f"Order #{order_id} system fee (5%)"))
                            # mirror into earnings table
                            cursor3.execute("SELECT COUNT(*) FROM earnings WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, admin_id, 'admin'))
                            if cursor3.fetchone()[0] == 0:
                                cursor3.execute("INSERT INTO earnings (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, admin_id, 'admin', admin_income, f"Order #{order_id} system fee (5%)"))
                        # if rider exists, record rider income
                        rider_row = db.cursor(dictionary=True)
                        rider_row.execute('SELECT rider_id FROM order_riders WHERE order_id=%s LIMIT 1', (order_id,))
                        rr = rider_row.fetchone()
                        rider_row.close()
                        if rr and rr.get('rider_id'):
                            rider_id_val = rr.get('rider_id')
                            cursor3.execute("SELECT COUNT(*) FROM income WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, rider_id_val, 'rider'))
                            if cursor3.fetchone()[0] == 0:
                                cursor3.execute("INSERT INTO income (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, rider_id_val, 'rider', rider_income, f"Order #{order_id} delivery fee"))
                                # mirror into earnings table
                                cursor3.execute("SELECT COUNT(*) FROM earnings WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, rider_id_val, 'rider'))
                                if cursor3.fetchone()[0] == 0:
                                    cursor3.execute("INSERT INTO earnings (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, rider_id_val, 'rider', rider_income, f"Order #{order_id} delivery fee"))
                        db.commit()
                        cursor3.close()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
            db.commit()
            notif_cur.close()
        except Exception:
            # don't block order flow on notification failure
            pass

        return jsonify({"success": True})
    finally:
        cursor.close()


@app.route("/order/mark_shipped", methods=["POST"])
def mark_order_shipped():
    user_role = session.get("role")
    user_id = session.get("user_id")
    order_id = request.form.get("order_id")
    if not user_id or not order_id:
        return jsonify({"success": False, "error": "Missing user or order."}), 400

    cursor = db.cursor(dictionary=True)
    try:
        allowed = False
        # If rider, ensure assignment to this rider
        if user_role == 'rider':
            cursor.execute("SELECT * FROM order_riders WHERE order_id = %s AND rider_id = %s", (order_id, user_id))
            if cursor.fetchone():
                allowed = True

        # If seller, ensure this seller owns at least one product in the order
        elif user_role == 'seller':
            cursor.execute("SELECT 1 FROM order_items oi JOIN products p ON oi.product_id = p.id WHERE oi.order_id = %s AND p.seller_id = %s LIMIT 1", (order_id, user_id))
            if cursor.fetchone():
                allowed = True

        # Admins can also mark shipped
        elif user_role == 'admin':
            allowed = True

        if not allowed:
            return jsonify({"success": False, "error": "Permission denied."}), 403

        cursor.execute("UPDATE orders SET status = 'shipped' WHERE id = %s", (order_id,))
        db.commit()

        # Notify admin and seller about shipped status
        try:
            # fetch order to know seller
            oc = db.cursor(dictionary=True)
            oc.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            ord_row = oc.fetchone()
            oc.close()
            notif_cur = db.cursor()
            admin_msg = f"Order #{order_id} has been marked as shipped by {user_role}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            seller_id = ord_row.get('seller_id') if ord_row else None
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Order #{order_id} has been marked as shipped"
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            # Customer notification
            customer_id = ord_row.get('user_id') if ord_row else None
            if customer_id:
                customer_msg = f"[customer:{customer_id}] Your order #{order_id} has been shipped"
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
            db.commit()
            notif_cur.close()
        except Exception:
            try:
                notif_cur.close()
            except Exception:
                pass

        return jsonify({"success": True})
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cursor.close()


@app.route('/order/upload_proof', methods=['POST'])
def upload_delivery_proof():
    # Riders upload a proof image which creates a delivery_proofs record
    if session.get('role') != 'rider':
        return jsonify({'success': False, 'error': 'Access denied.'}), 403

    user_id = session.get('user_id')
    order_id = request.form.get('order_id')
    proof_file = request.files.get('proof_file')

    if not order_id or not proof_file:
        return jsonify({'success': False, 'error': 'Missing order or file.'}), 400

    # Allow any file type for proof uploads (no extension restriction)

    cursor = db.cursor(dictionary=True)
    shipping_fee = 0
    try:
        # Verify rider is assigned to this order
        execute_safe(cursor, 'SELECT * FROM order_riders WHERE order_id = %s AND rider_id = %s', (order_id, user_id))
        assigned = cursor.fetchone()
        if not assigned:
            return jsonify({'success': False, 'error': 'Not assigned to this order.'}), 403

        execute_safe(cursor, 'SELECT status FROM orders WHERE id = %s', (order_id,))
        order_status_row = cursor.fetchone()
        order_status = (order_status_row.get('status') if order_status_row else '') or ''
        if order_status.lower() != 'shipped':
            return jsonify({'success': False, 'error': 'This delivery can only be marked delivered after the seller marks the order as shipped.'}), 400

        # Save file under static/uploads/delivery_proofs
        saved = save_file(proof_file, os.path.join(app.config.get('UPLOAD_FOLDER', 'static/uploads'), 'delivery_proofs'))
        if not saved:
            return jsonify({'success': False, 'error': 'Failed to save file.'}), 500

        # Insert record into delivery_proofs
        cursor2 = db.cursor()
        execute_safe(cursor2, 'INSERT INTO delivery_proofs (order_id, file_path) VALUES (%s, %s)', (order_id, saved))

        # Update statuses: order_riders.status and orders.status
        execute_safe(cursor2, 'UPDATE order_riders SET status=%s WHERE order_id=%s', ('delivered', order_id))
        execute_safe(cursor2, 'UPDATE orders SET status=%s WHERE id=%s', ('delivered', order_id))
        # If this was a return flow, mark the return_request as delivered
        try:
            return_cur = db.cursor()
            execute_safe(return_cur, "UPDATE return_requests SET delivered_at=NOW(), status=%s WHERE order_id=%s AND status IN ('assigned','in_transit','pickup_requested')", ('delivered', order_id))
            db.commit()
            return_cur.close()
        except Exception:
            try:
                return_cur.close()
            except Exception:
                pass
        
        # When rider uploads proof and marks delivered, record rider earning (shipping fee)
        try:
            inc_cur = db.cursor(dictionary=True)
            execute_safe(inc_cur, "SELECT shipping_fee FROM orders WHERE id = %s", (order_id,))
            orow = inc_cur.fetchone()
            inc_cur.close()
            if orow:
                shipping_fee = float(orow.get('shipping_fee', 0) or 0)
                # Rider assigned (should be current user)
                rider_id_val = assigned.get('rider_id') if assigned and assigned.get('rider_id') else user_id
                if rider_id_val:
                    rins = db.cursor()
                    execute_safe(rins, "SELECT COUNT(*) FROM income WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, rider_id_val, 'rider'))
                    if rins.fetchone()[0] == 0:
                        execute_safe(rins, "INSERT INTO income (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, rider_id_val, 'rider', shipping_fee, f"Order #{order_id} delivery fee (on delivered)"))
                        # Also mirror into earnings table
                        execute_safe(rins, "SELECT COUNT(*) FROM earnings WHERE order_id=%s AND user_id=%s AND role=%s", (order_id, rider_id_val, 'rider'))
                        if rins.fetchone()[0] == 0:
                            execute_safe(rins, "INSERT INTO earnings (order_id, user_id, role, amount, description, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, rider_id_val, 'rider', shipping_fee, f"Order #{order_id} delivery fee (on delivered)"))
                        db.commit()
                    rins.close()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        
        db.commit()
        # Create notifications for admin, seller and customer about delivery
        try:
            oc = db.cursor(dictionary=True)
            oc.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            ord_row = oc.fetchone()
            oc.close()
            notif_cur = db.cursor()
            admin_msg = f"Order #{order_id} has been delivered"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            seller_id = ord_row.get('seller_id') if ord_row else None
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Order #{order_id} has been delivered to the customer"
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            customer_id = ord_row.get('user_id') if ord_row else None
            if customer_id:
                customer_msg = f"[customer:{customer_id}] Your order #{order_id} has been delivered"
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
            db.commit()
            notif_cur.close()
        except Exception:
            try:
                notif_cur.close()
            except Exception:
                pass

        cursor2.close()
        return jsonify({'success': True, 'rider_earning': shipping_fee})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()

@app.route('/search_products')
def search_products():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    cursor = db.cursor(dictionary=True)
    # Match query against name, category and description (partial matches)
    like_q = f"%{q}%"
    cursor.execute(
        "SELECT id, name, image FROM products WHERE status='approved' AND (name LIKE %s OR category LIKE %s OR description LIKE %s) ORDER BY name LIMIT 20",
        (like_q, like_q, like_q)
    )
    prods = cursor.fetchall()
    for p in prods:
        p['image_url'] = media_url(p['image'], 'image/dog.webp')
    cursor.close()
    return jsonify(prods)


@app.route("/seller", methods=["GET", "POST"])
def seller():
    cleanup_deleted_products()

    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
    user_info = cursor.fetchone()

    # Handle profile update
    if request.method == "POST":
        fullname = request.form.get("fullname")
        profile_picture = request.files.get("profile_picture")
        picture_path = user_info["profile_pic"] if user_info.get("profile_pic") else None

        if has_uploaded_file(profile_picture):
            if allowed_file(profile_picture.filename):
                picture_path = save_file(profile_picture, "uploads/profile_pics")
            else:
                flash("File type not allowed! Only png, jpg, jpeg, gif.", "error")
                cursor.close()
                return redirect(url_for("seller") + "#settings/profileTab")

        cursor.execute(
            "UPDATE users SET fullname=%s, profile_pic=%s WHERE id=%s",
            (fullname, picture_path, user_id)
        )
        db.commit()
        cursor.close()
        session["fullname"] = fullname
        session["profile_pic"] = picture_path
        flash("✅ Profile updated successfully!", "success")
        return redirect(url_for("seller") + "#settings/profileTab")

    # Fetch addresses
    cursor.execute("SELECT * FROM addresses WHERE user_id=%s ORDER BY id DESC", (user_id,))
    addresses = cursor.fetchall()

    # Fetch products with real stock from variants when variants exist.
    cursor.execute("""
        SELECT p.*, COALESCE(variant_stock.real_stock, p.stock, 0) AS real_stock
        FROM products p
        LEFT JOIN (
            SELECT product_id, SUM(stock) AS real_stock
            FROM product_variants
            GROUP BY product_id
        ) AS variant_stock ON variant_stock.product_id = p.id
        WHERE p.seller_id=%s
        ORDER BY p.created_at DESC
    """, (user_id,))
    products = cursor.fetchall()

    # Fetch product variants
    product_ids = [p['id'] for p in products]
    variants = []
    if product_ids:
        format_strings = ','.join(['%s'] * len(product_ids))
        sql = f"SELECT * FROM product_variants WHERE product_id IN ({format_strings})"
        params = tuple(product_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in product_variants query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        variants = cursor.fetchall()

    # Group variants by product_id
    variants_dict = {}
    for v in variants:
        variants_dict.setdefault(v['product_id'], []).append(v)
    for p in products:
        product_variants = variants_dict.get(p['id'], [])
        if product_variants:
            p['real_stock'] = sum(int(v.get('stock') or 0) for v in product_variants)
        if p.get('real_stock') is None:
            p['real_stock'] = p.get('stock') or 0

    # Fetch orders for this seller's products, including vehicle assigned by system
    orders = []
    order_items_by_order = {}
    # Fetch orders that contain at least one product belonging to this seller. Use product.seller_id to ensure we include
    # all historic orders that reference products now associated with this seller, even if the product ID list is outdated.
    try:
        sql = f"""
            SELECT DISTINCT o.*, o.vehicle,
               COALESCE((SELECT SUM(amount) FROM earnings e WHERE e.order_id = o.id AND e.user_id = %s AND e.role='seller'), (SELECT SUM(amount) FROM income i WHERE i.order_id = o.id AND i.user_id = %s AND i.role='seller'), 0) AS seller_earning
            FROM orders o
            WHERE o.id IN (
                SELECT oi.order_id FROM order_items oi LEFT JOIN products p ON oi.product_id = p.id WHERE COALESCE(oi.seller_id, p.seller_id) = %s
            ) OR o.seller_id = %s
            ORDER BY o.created_at DESC
        """
        params = (user_id, user_id, user_id, user_id)
        # Debug: check placeholders vs params
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in seller orders query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        orders = cursor.fetchall() or []
    except Exception:
        orders = []

    # Get all order items for these orders, but only for this seller's products
    order_ids = [o['id'] for o in orders]
    if order_ids:
        format_order_ids = ','.join(['%s'] * len(order_ids))
        sql = f"""
                SELECT oi.*, p.name AS product_name, p.image AS product_image, pv.color, pv.size
                FROM order_items oi
                LEFT JOIN products p ON oi.product_id = p.id
                LEFT JOIN product_variants pv ON oi.variant_id = pv.id
                WHERE oi.order_id IN ({format_order_ids}) AND COALESCE(oi.seller_id, p.seller_id) = %s
        """
        params = tuple(order_ids) + (user_id,)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in seller order_items query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        items = cursor.fetchall()
        for item in items:
            order_items_by_order.setdefault(item['order_id'], []).append(item)

            # Fetch assigned riders for these orders (if any)
            sql = f"SELECT ordr.order_id, u.fullname AS rider_name, u.id AS rider_id FROM order_riders ordr JOIN users u ON ordr.rider_id = u.id WHERE ordr.order_id IN ({format_order_ids})"
            params = tuple(order_ids)
            if sql.count('%s') != len(params):
                app.logger.error("SQL placeholder mismatch in seller assigned riders query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
            execute_safe(cursor, sql, params)
            assigned_riders = cursor.fetchall()
            riders_map = {r['order_id']: r for r in assigned_riders} if assigned_riders else {}
            # Fetch delivery proofs for these orders (if any)
            sql = f"SELECT * FROM delivery_proofs WHERE order_id IN ({format_order_ids})"
            params = tuple(order_ids)
            if sql.count('%s') != len(params):
                app.logger.error("SQL placeholder mismatch in seller delivery_proofs query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
            execute_safe(cursor, sql, params)
            proof_rows = cursor.fetchall()
            proofs_map = {}
            for p in (proof_rows or []):
                proofs_map.setdefault(p['order_id'], []).append(p['file_path'])
            # Fetch any return_requests for these seller orders
            try:
                sql = f"SELECT * FROM return_requests WHERE order_id IN ({format_order_ids})"
                params = tuple(order_ids)
                if sql.count('%s') != len(params):
                    app.logger.error("SQL placeholder mismatch in seller return_requests query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
                execute_safe(cursor, sql, params)
                seller_rr_rows = cursor.fetchall() or []
                seller_return_requests_map = {r['order_id']: r for r in seller_rr_rows}
            except Exception:
                seller_return_requests_map = {}
            # fill requested_by_name for seller return_requests
            # Build sellers_by_order mapping (all sellers involved in orders)
            sellers_by_order = {}
            try:
                sql = f"SELECT DISTINCT oi.order_id, COALESCE(oi.seller_id, p.seller_id) AS seller_id FROM order_items oi LEFT JOIN products p ON oi.product_id = p.id WHERE oi.order_id IN ({format_order_ids})"
                params = tuple(order_ids)
                if sql.count('%s') != len(params):
                    app.logger.error("SQL placeholder mismatch in seller seller_rows query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
                execute_safe(cursor, sql, params)
                seller_rows = cursor.fetchall() or []
                for r in seller_rows:
                    oid = r.get('order_id')
                    sid = r.get('seller_id')
                    if oid and sid:
                        sellers_by_order.setdefault(oid, set()).add(sid)
            except Exception:
                sellers_by_order = {}
            # Resolve seller id -> fullname map
            sellers_name_map = {}
            all_seller_ids = set()
            for sset in sellers_by_order.values():
                all_seller_ids.update(sset)
            if all_seller_ids:
                fmt = ','.join(['%s'] * len(all_seller_ids))
                sql = f"SELECT id, fullname FROM users WHERE id IN ({fmt})"
                params = tuple(all_seller_ids)
                if sql.count('%s') != len(params):
                    app.logger.error("SQL placeholder mismatch in seller sellers_name_map query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
                execute_safe(cursor, sql, params)
                rows = cursor.fetchall() or []
                sellers_name_map = {r['id']: r.get('fullname') or str(r['id']) for r in rows}
            # Attach sellers_display to each order
            for o in orders:
                sset = sellers_by_order.get(o['id'], set())
                names = [sellers_name_map.get(i, str(i)) for i in sorted(sset)]
                o['sellers_display'] = ', '.join(names) if names else '-' 
            try:
                req_ids = {r.get('requested_by') for r in (seller_rr_rows or []) if r.get('requested_by')}
                if req_ids:
                    fmt = ','.join(['%s'] * len(req_ids))
                    sql = f"SELECT id, fullname FROM users WHERE id IN ({fmt})"
                    params = tuple(req_ids)
                    if sql.count('%s') != len(params):
                        app.logger.error("SQL placeholder mismatch in seller requested_by query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
                    execute_safe(cursor, sql, params)
                    req_rows = cursor.fetchall() or []
                    req_map = {r['id']: r.get('fullname') for r in req_rows}
                    for r in (seller_rr_rows or []):
                        r['requested_by_name'] = req_map.get(r.get('requested_by'))
            except Exception:
                pass

    cursor.close()
    if not user_info:
        user_info = {"fullname": "", "profile_pic": None}


    import json
    for order in orders:
        # Attach customer name
        try:
            cursor2 = db.cursor(dictionary=True)
            cursor2.execute("SELECT fullname FROM users WHERE id = %s", (order.get('user_id'),))
            user_row = cursor2.fetchone()
            order['customer_name'] = user_row['fullname'] if user_row and user_row.get('fullname') else str(order.get('user_id'))
            cursor2.close()
        except Exception:
            order['customer_name'] = str(order.get('user_id'))
        items = order_items_by_order.get(order['id'], [])
        order['total_amount'] = sum((item.get('price', 0) or 0) * (item.get('quantity', 1) or 1) for item in items)
        # attach rider info if present
        rid = riders_map.get(order['id']) if 'riders_map' in locals() else None
        if rid:
            order['rider_name'] = rid.get('rider_name')
            order['rider_id'] = rid.get('rider_id')
            order['rider_assigned'] = True
        else:
            order['rider_name'] = None
            order['rider_id'] = None
            order['rider_assigned'] = False
        # attach any delivery proofs
        order['proofs'] = proofs_map.get(order['id'], []) if 'proofs_map' in locals() else []
        order['return_request'] = seller_return_requests_map.get(order['id']) if 'seller_return_requests_map' in locals() else None
        # attach total weight (kg) for the order by summing variant weights * quantity
        try:
            wcur = db.cursor(dictionary=True)
            wcur.execute("SELECT SUM(pv.weight_kg * oi.quantity) AS total_weight FROM order_items oi LEFT JOIN product_variants pv ON oi.variant_id = pv.id WHERE oi.order_id = %s", (order['id'],))
            w = wcur.fetchone()
            order['total_weight'] = round((w and w.get('total_weight')) or 0, 2)
        finally:
            try:
                wcur.close()
            except Exception:
                pass
        # Use the customer_address_snapshot and seller_address_snapshot fields (JSON) from orders table for display
        import json
        def extract_addr(snap):
            addr = '-'
            if snap:
                try:
                    data = json.loads(snap) if isinstance(snap, str) else snap
                    addr = ', '.join(filter(None, [
                        data.get('province_name'),
                        data.get('city_name'),
                        data.get('barangay_name'),
                        data.get('street')
                    ]))
                    if not addr:
                        addr = ', '.join(str(v) for v in data.values() if isinstance(v, str) and v.strip())
                except Exception:
                    addr = str(snap)
            return addr or '-'

        order['customer_full_address'] = extract_addr(order.get('customer_address_snapshot'))
        order['seller_full_address'] = extract_addr(order.get('seller_address_snapshot'))

    # Assign a per-shop sequential order number (1 = first order for this shop)
    # Use created_at when available, otherwise fall back to id for ordering
    try:
        sorted_orders = sorted(orders, key=lambda o: (o.get('created_at') is None, o.get('created_at') or o.get('id')))
    except Exception:
        sorted_orders = list(orders)
    for idx, ord_obj in enumerate(sorted_orders, start=1):
        # attach number to the original order dict
        ord_obj['shop_order_number'] = idx

    # Fetch seller-targeted notifications (tagged with [seller:<id>])
    try:
        notif_cursor = db.cursor(dictionary=True)
        tag = f"[seller:{user_id}]"
        notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
        seller_notifications = notif_cursor.fetchall() or []
        notif_cursor.close()
    except Exception:
        seller_notifications = []



    # Fetch cancellation requests for this seller
    cancel_cursor = db.cursor(dictionary=True)
    cancel_cursor = db.cursor(dictionary=True)
    execute_safe(cancel_cursor, '''
        SELECT o.id AS order_id, o.user_id, o.created_at, o.status, o.cancelled_reason AS reason, u.fullname AS customer_name
        FROM orders o
        JOIN users u ON o.user_id = u.id
        WHERE o.seller_id = %s AND o.status = 'cancel_request'
        ORDER BY o.created_at DESC
    ''', (user_id,))
    cancellation_requests = cancel_cursor.fetchall()
    cancel_cursor.close()
    
    # Fetch refund requests for this seller
    refund_cursor = db.cursor(dictionary=True)
    refund_cursor.execute('''
        SELECT o.id AS order_id, o.user_id, o.created_at, o.status, o.refund_reason AS reason, u.fullname AS customer_name
        FROM orders o
        JOIN users u ON o.user_id = u.id
        WHERE o.seller_id = %s AND o.status = 'refund_request'
        ORDER BY o.created_at DESC
    ''', (user_id,))
    refund_requests = refund_cursor.fetchall()
    refund_cursor.close()

    # Fetch seller income data
    income_cursor = db.cursor(dictionary=True)
    income_cursor.execute('''
        SELECT COALESCE(
            (SELECT SUM(amount) FROM earnings WHERE user_id=%s AND role='seller'),
            (SELECT SUM(amount) FROM income WHERE user_id=%s AND role='seller'),
            0
        ) AS total_income,
        COALESCE(
            (SELECT COUNT(DISTINCT order_id) FROM earnings WHERE user_id=%s AND role='seller'),
            (SELECT COUNT(DISTINCT order_id) FROM income WHERE user_id=%s AND role='seller'),
            0
        ) AS completed_orders
        ''', (user_id, user_id, user_id, user_id))
    income_data = income_cursor.fetchone() or {}
    seller_total_income = float(income_data.get('total_income', 0) or 0)
    seller_completed_orders = int(income_data.get('completed_orders', 0) or 0)
    
    # Fetch monthly income breakdown
    income_cursor.execute('''
        SELECT month, SUM(amount) AS monthly_amount FROM (
            SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, amount FROM earnings WHERE user_id = %s AND role = 'seller' AND created_at IS NOT NULL
            UNION ALL
            SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, amount FROM income WHERE user_id = %s AND role = 'seller' AND created_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM earnings e WHERE e.user_id = %s AND e.role = 'seller' AND DATE_FORMAT(e.created_at, '%Y-%m') = DATE_FORMAT(income.created_at, '%Y-%m'))
        ) a
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    ''', (user_id, user_id, user_id))
    monthly_income = income_cursor.fetchall() or []
    income_cursor.close()

    # Compute pending deliveries count and product count
    pending_deliveries = len([o for o in orders if (o.get('status') or '').lower() in ('ready_for_pickup','assigned','processing','shipped')]) if orders else 0
    products_count = len(products) if products else 0
    # Seller orders today
    try:
        tod_cur = db.cursor()
        tod_cur.execute("SELECT COUNT(*) FROM orders WHERE seller_id=%s AND DATE(created_at)=CURDATE()", (user_id,))
        seller_orders_today = tod_cur.fetchone()[0] if tod_cur.rowcount != 0 else 0
        tod_cur.close()
    except Exception:
        seller_orders_today = 0
    # Seller income today
    try:
        inc_today_cur = db.cursor()
        inc_today_cur.execute('''SELECT COALESCE((SELECT SUM(amount) FROM earnings WHERE user_id=%s AND role='seller' AND DATE(created_at)=CURDATE()), (SELECT SUM(amount) FROM income WHERE user_id=%s AND role='seller' AND DATE(created_at)=CURDATE()), 0)''', (user_id, user_id))
        seller_sales_today = inc_today_cur.fetchone()[0] or 0
        inc_today_cur.close()
    except Exception:
        seller_sales_today = 0
    
    return render_template(
        "seller.html",
        user_info=user_info,
        addresses=addresses,
        products=products,
        variants=variants_dict,
        seller_orders=orders,
        seller_order_items=order_items_by_order,
        notifications=seller_notifications,
        cancellation_requests=cancellation_requests,
        refund_requests=refund_requests,
        seller_return_requests=seller_return_requests_map if 'seller_return_requests_map' in locals() else [],
        seller_total_income=seller_total_income,
        seller_completed_orders=seller_completed_orders,
        monthly_income=monthly_income,
        pending_deliveries=pending_deliveries,
        products_count=products_count,
        seller_orders_today=seller_orders_today,
        seller_sales_today=seller_sales_today
    )


@app.route("/orders")
def orders():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in to view your orders.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor(dictionary=True)
    # Fetch user info
    cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
    user_info = cursor.fetchone()

    # Fetch orders for the user
    cursor.execute("""
        SELECT * FROM orders WHERE user_id = %s ORDER BY id DESC
    """, (user_id,))
    orders = cursor.fetchall()

    # Fetch order items for each order
    order_ids = [o['id'] for o in orders]
    items_by_order = {}
    ratings_by_order = {}
    if order_ids:
        format_strings = ','.join(['%s'] * len(order_ids))
        sql = f"""
            SELECT oi.order_id, oi.product_id, oi.price, oi.quantity, p.name AS product_name, p.image AS product_image, pv.color, pv.size
            FROM order_items oi
            LEFT JOIN products p ON oi.product_id = p.id
            LEFT JOIN product_variants pv ON oi.variant_id = pv.id
            WHERE oi.order_id IN ({format_strings})
        """
        params = tuple(order_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in order_items query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        items = cursor.fetchall()
        for item in items:
            # Ensure price is present for template
            if 'price' not in item or item['price'] is None:
                item['price'] = 0.0
            items_by_order.setdefault(item['order_id'], []).append(item)
        # Fetch product ratings for these orders
        sql = f"SELECT order_id, product_id FROM product_ratings WHERE user_id = %s AND order_id IN ({format_strings})"
        params = tuple([user_id] + order_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in product_ratings query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        rated_rows = cursor.fetchall()
        rated_map = {}
        for r in rated_rows:
            rated_map.setdefault(r['order_id'], set()).add(r['product_id'])
        for oid in order_ids:
            # Get all unique product_ids in this order
            prod_ids = set(i['product_id'] for i in items_by_order.get(oid, []))
            rated_prod_ids = rated_map.get(oid, set())
            ratings_by_order[oid] = prod_ids.issubset(rated_prod_ids) and len(prod_ids) > 0

    cursor.close()
    # Attach items and rating flag to each order
    for order in orders:
        order['items'] = items_by_order.get(order['id'], [])
        order['is_rated'] = ratings_by_order.get(order['id'], False)
    # Fetch return_requests for these customer orders and attach
    try:
        if orders:
            order_ids = [o['id'] for o in orders]
            format_strings = ','.join(['%s'] * len(order_ids))
            rr_cur = db.cursor(dictionary=True)
            sql = f"SELECT * FROM return_requests WHERE order_id IN ({format_strings})"
            params = tuple(order_ids)
            if sql.count('%s') != len(params):
                app.logger.error("SQL placeholder mismatch in orders return_requests query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
            execute_safe(rr_cur, sql, params)
            rr_rows = rr_cur.fetchall() or []
            rr_map = {r['order_id']: r for r in rr_rows}
            rr_cur.close()
        else:
            rr_map = {}
    except Exception:
        rr_map = {}
    for order in orders:
        order['return_request'] = rr_map.get(order['id'])

    # Fetch customer-targeted notifications (tagged with [customer:<id>])
    customer_notifications = []
    try:
        notif_cursor = db.cursor(dictionary=True)
        tag = f"[customer:{user_id}]"
        notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
        customer_notifications = notif_cursor.fetchall() or []
        notif_cursor.close()
    except Exception:
        customer_notifications = []

    customer_return_requests = list(rr_map.values()) if 'rr_map' in locals() else []
    return render_template("order.html", user=user_info, orders=orders, notifications=customer_notifications, customer_return_requests=customer_return_requests)

# Customer order cancellation endpoint
@app.route("/order/cancel", methods=["POST"])
def cancel_order():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json() or request.form
    order_id = data.get("order_id")
    reason = data.get("reason")
    if not order_id or not reason:
        return jsonify({"success": False, "error": "Missing order_id or reason"}), 400

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404

    # Only the customer who placed the order can cancel
    if order.get('user_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403

    current_status = (order.get('status') or '').lower()
    # Customers may only cancel while the order is still processing.
    if current_status == 'processing':
        cursor.execute("UPDATE orders SET status=%s, cancelled_reason=%s, cancelled_at=NOW() WHERE id=%s", ("cancelled", reason, order_id))
        db.commit()
        # Notify customer and seller of the cancellation
        try:
            notif_cur = db.cursor()
            customer_msg = f"[customer:{user_id}] Your order #{order_id} has been cancelled. Reason: {reason}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
            seller_id = order.get('seller_id')
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Order #{order_id} was cancelled by the customer. Reason: {reason}"
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            admin_msg = f"Order #{order_id} status changed to cancelled by customer. Reason: {reason}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            db.commit()
            notif_cur.close()
        except Exception:
            try:
                notif_cur.close()
            except Exception:
                pass
        cursor.close()
        return jsonify({"success": True, "message": "Order cancelled successfully."})
    else:
        cursor.close()
        return jsonify({"success": False, "error": "You can only cancel orders while they are still processing."}), 400
    
# Seller/Admin: Confirm or reject cancellation request
@app.route("/order/cancel_decision", methods=["POST"])
def seller_cancel_decision():
    user_id = session.get("user_id")
    role = session.get("role")
    if not user_id or role not in ("seller", "admin"):
        return jsonify({"success": False, "error": "Not authorized"}), 403

    data = request.get_json() or request.form
    order_id = data.get("order_id")
    decision = data.get("decision")  # 'confirm' or 'reject'
    if not order_id or decision not in ("confirm", "reject"):
        return jsonify({"success": False, "error": "Missing or invalid data"}), 400

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404

    # Only the seller assigned to this order can act (or admin)
    if role == "seller" and order.get('seller_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403

    if (order.get('status') or '').lower() != 'cancel_request':
        cursor.close()
        return jsonify({"success": False, "error": "Order is not awaiting cancellation confirmation"}), 400

    customer_id = order.get('user_id')
    reason = order.get('cancelled_reason', '')
    seller_id = order.get('seller_id')
    
    if decision == 'confirm':
        # Seller or admin approves cancellation
        # Clear any previous rejection metadata when approving
        # Perform update and log in same transaction for consistency
        cursor.execute("UPDATE orders SET status=%s, cancelled_at=NOW(), cancel_rejection_reason=NULL, cancel_rejection_notes=NULL, cancel_rejected_at=NULL, cancel_rejected_by=NULL WHERE id=%s", ("cancelled", order_id))
        cursor.execute(
            "INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)",
            (order_id, 'cancel_approve', user_id, role, reason or '', '')
        )
        db.commit()
        # Notify customer, seller, and admin
        approver = "seller" if role == "seller" else "admin"
        try:
            notif_cur = db.cursor()
            customer_msg = f"[customer:{customer_id}] Your order #{order_id} cancellation has been approved by {approver}. Reason: {reason}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Order #{order_id} cancellation has been approved by {approver}."
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            admin_msg = f"Order #{order_id} status changed to cancelled by {approver}."
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            db.commit()
            notif_cur.close()
        except Exception:
            try:
                notif_cur.close()
            except Exception:
                pass
        # (Action log created in the same transaction above)
        cursor.close()
        return jsonify({"success": True, "message": f"Order cancelled and customer notified."})
    else:
        # Seller or admin rejects cancellation
        # capture rejection reason/notes if present
        rejection_reason = (data.get('rejection_reason') or data.get('reason') or data.get('reject_reason') or '')
        rejection_notes = (data.get('rejection_notes') or data.get('notes') or '')
        # Perform update and log in same transaction for consistency
        cursor.execute(
            "UPDATE orders SET status=%s, cancel_rejection_reason=%s, cancel_rejection_notes=%s, cancel_rejected_at=NOW(), cancel_rejected_by=%s WHERE id=%s",
            ("processing", rejection_reason, rejection_notes, user_id, order_id)
        )
        cursor.execute(
            "INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)",
            (order_id, 'cancel_reject', user_id, role, rejection_reason, rejection_notes)
        )
        db.commit()
        # (Action log created in the same transaction above)
        # Notify customer, seller, and admin
        approver = "seller" if role == "seller" else "admin"
        try:
            notif_cur = db.cursor()
            customer_msg = f"[customer:{customer_id}] Your cancellation request for order #{order_id} was rejected by {approver}."
            if rejection_reason:
                customer_msg += f" Reason: {rejection_reason}."
            if rejection_notes:
                customer_msg += f" Notes: {rejection_notes}."
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Order #{order_id} cancellation request was rejected by {approver}."
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            admin_msg = f"Order #{order_id} status changed back to processing after cancellation rejection by {approver}."
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            db.commit()
            notif_cur.close()
        except Exception:
            try:
                notif_cur.close()
            except Exception:
                pass
        cursor.close()
        return jsonify({"success": True, "message": "Cancellation rejected and customer notified. Order reverted to processing."})


@app.route("/order/seller_cancel", methods=["POST"])
def seller_cancel_order():
    """Seller or admin cancels an order without a customer request.
    Requires: order_id, reason (one of allowed reasons), optional notes.
    Performs: update order status, set cancelled_at and cancelled_reason, insert action log, notify customer.
    """
    user_id = session.get('user_id')
    role = session.get('role')
    if not user_id or role not in ("seller", "admin"):
        return jsonify({"success": False, "error": "Not authorized"}), 403

    data = request.get_json() or request.form
    order_id = data.get('order_id')
    reason = data.get('reason')
    notes = data.get('notes') or ''

    # Validate inputs
    allowed_reasons = [
        'Out of stock',
        'Product defect found during inspection',
        'Wrong price listing (pricing error)',
        'Seller unable to fulfill due to logistics issues',
        'Verification or fraud concerns',
        'Other'
    ]
    if not order_id or not reason:
        return jsonify({"success": False, "error": "Missing order_id or reason"}), 400
    if reason not in allowed_reasons:
        return jsonify({"success": False, "error": "Invalid reason"}), 400
    if reason == 'Other' and not notes:
        return jsonify({"success": False, "error": "Please provide additional notes for reason 'Other'"}), 400

    # Fetch order and permission checks
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404
    if role == 'seller' and order.get('seller_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403

    # Prevent cancelling orders that are already completed/delivered/refunded/cancelled
    cur_status = (order.get('status') or '').lower()
    if cur_status in ('cancelled', 'refunded', 'delivered', 'completed'):
        cursor.close()
        return jsonify({"success": False, "error": f"Order cannot be cancelled in its current status: {cur_status}"}), 400

    # Perform update and log in same transaction
    try:
        # Use a non-dictionary cursor for operations
        cur = db.cursor()
        full_reason = reason
        if notes:
            full_reason = f"{reason} | Notes: {notes}"
        cur.execute("UPDATE orders SET status=%s, cancelled_reason=%s, cancelled_at=NOW() WHERE id=%s", ("cancelled", full_reason, order_id))
        cur.execute(
            "INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)",
            (order_id, 'seller_cancel', user_id, role, reason, notes)
        )
        db.commit()
        cur.close()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        cursor.close()
        return jsonify({"success": False, "error": str(e)}), 500

    # Notify customer
    try:
        notif_cur = db.cursor()
        customer_id = order.get('user_id')
        if customer_id:
            customer_msg = f"[customer:{customer_id}] Your order #{order_id} has been cancelled by the {role}. Reason: {reason}"
            if notes:
                customer_msg += f". Notes: {notes}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, f"/orders?order_id={order_id}"))
        seller_id = order.get('seller_id')
        if seller_id:
            seller_msg = f"[seller:{seller_id}] Order #{order_id} status changed to cancelled by {role}. Reason: {reason}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
        admin_msg = f"Order #{order_id} status changed to cancelled by {role}. Reason: {reason}"
        notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
        db.commit()
        notif_cur.close()
    except Exception:
        try:
            notif_cur.close()
        except Exception:
            pass

    cursor.close()
    return jsonify({"success": True, "message": "Order cancelled and customer notified."})

@app.route("/order/refund_decision", methods=["POST"])
def order_refund_decision():
    """Admin-only refund decision endpoint.
    Sellers can accept refunds directly via `/order/comply_refund`; admin review is needed for decline decisions.
    """
    user_id = session.get("user_id")
    role = session.get("role")
    if not user_id or role != 'admin':
        return jsonify({"success": False, "error": "Not authorized"}), 403

    data = request.get_json() or request.form
    order_id = data.get("order_id")
    decision = data.get("decision")  # 'confirm' or 'reject'
    if not order_id or decision not in ("confirm", "reject"):
        return jsonify({"success": False, "error": "Missing or invalid data"}), 400

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404

    # Only proceed for orders in refund_request status
    if (order.get('status') or '').lower() != 'refund_request':
        cursor.close()
        return jsonify({"success": False, "error": "Order is not awaiting refund confirmation"}), 400

    customer_id = order.get('user_id')
    reason = order.get('refund_reason', '')
    
    return_required = data.get('return_required') or data.get('return') or False
    # normalize truthy values for return_required
    if isinstance(return_required, str):
        return_required = return_required.lower() in ('1', 'true', 'yes', 'on')
    if decision == 'confirm':
        # Admin approves refund
        try:
            cur = db.cursor()
            if return_required:
                # Request a return pickup before refunding
                cur.execute("UPDATE orders SET status=%s, refund_requested_at=COALESCE(refund_requested_at, NOW()) WHERE id=%s", ("return_request", order_id))
                # Ensure a return_requests row exists for this order
                try:
                    cur2 = db.cursor(dictionary=True)
                    cur2.execute("SELECT id FROM return_requests WHERE order_id=%s", (order_id,))
                    rr = cur2.fetchone()
                    if not rr:
                        cur.execute("INSERT INTO return_requests (order_id, requested_by, requested_by_role, reason, status, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, user_id, role, reason, 'requested'))
                    cur2.close()
                except Exception:
                    try:
                        cur2.close()
                    except Exception:
                        pass
                # Notify admin/seller to coordinate a return (simplified)
                notify_cur = db.cursor()
                seller_id = order.get('seller_id')
                if seller_id:
                    seller_msg = f"[seller:{seller_id}] Refund for order #{order_id} was approved and a return pickup is requested. Reason: {reason}"
                    notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
                admin_msg = f"[admin:system] Refund for order #{order_id} was approved and a return pickup is requested. Reason: {reason}"
                notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
                notify_cur.close()
            else:
                cur.execute("UPDATE orders SET status=%s, refunded_at=NOW() WHERE id=%s", ("refunded", order_id))
                # If a return_request row exists for this order, mark it as cancelled since we're refunding immediately
                try:
                    r_cancel = db.cursor()
                    r_cancel.execute("UPDATE return_requests SET status=%s WHERE order_id=%s", ('cancelled', order_id))
                    db.commit()
                    r_cancel.close()
                except Exception:
                    try:
                        r_cancel.close()
                    except Exception:
                        pass
                # Notify customer
                notify_cur = db.cursor()
                seller_id = order.get('seller_id')
                if seller_id:
                    seller_msg = f"[seller:{seller_id}] Refund for order #{order_id} has been approved and processed. Reason: {reason}"
                    notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
                admin_msg = f"[admin:system] Refund for order #{order_id} has been approved and processed. Reason: {reason}"
                notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
                notify_cur.close()
            # Insert audit log
            cur.execute(
                "INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, 'refund_approve', user_id, role, reason or '', '')
            )
            db.commit()
            cur.close()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            cursor.close()
            return jsonify({"success": False, "error": str(e)}), 500
        # Notify customer
        approver = "seller" if role == "seller" else "admin"
        try:
            notif_cur = db.cursor()
            customer_msg = f"[customer:{customer_id}] Your refund for order #{order_id} has been approved by {approver}. Reason: {reason}"
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, "/orders"))
            db.commit()
            notif_cur.close()
        except Exception:
            try:
                notif_cur.close()
            except Exception:
                pass
        cursor.close()
        return jsonify({"success": True, "message": "Refund approved and customer notified."})
    else:
        # Admin rejects refund
        rejection_reason = (data.get('rejection_reason') or data.get('reason') or data.get('reject_reason') or '')
        rejection_notes = (data.get('rejection_notes') or data.get('notes') or '')
        try:
            cur = db.cursor()
            cur.execute("UPDATE orders SET status=%s, refund_rejection_reason=%s, refund_rejection_notes=%s, refund_rejected_at=NOW(), refund_rejected_by=%s WHERE id=%s", ("completed", rejection_reason, rejection_notes, user_id, order_id))
            # Insert audit log
            cur.execute(
                "INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, 'refund_reject', user_id, role, rejection_reason, rejection_notes)
            )
            db.commit()
            cur.close()
            # If a return_requests row exists for this order, mark it as cancelled
            try:
                r_cancel2 = db.cursor()
                r_cancel2.execute("UPDATE return_requests SET status=%s WHERE order_id=%s", ('cancelled', order_id))
                db.commit()
                r_cancel2.close()
            except Exception:
                try:
                    r_cancel2.close()
                except Exception:
                    pass
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            cursor.close()
            return jsonify({"success": False, "error": str(e)}), 500
        # Notify customer
        approver = "admin"
        try:
            notif_cur = db.cursor()
            customer_msg = f"[customer:{customer_id}] Your refund request for order #{order_id} was rejected by {approver}."
            if rejection_reason:
                customer_msg += f" Reason: {rejection_reason}."
            if rejection_notes:
                customer_msg += f" Notes: {rejection_notes}."
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", customer_msg, "/orders"))
            seller_id = order.get('seller_id')
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Refund request for order #{order_id} was rejected by admin."
                notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            admin_msg = f"Order #{order_id} status changed to completed after refund rejection by admin."
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            db.commit()
            notif_cur.close()
        except Exception:
            try:
                notif_cur.close()
            except Exception:
                pass
        cursor.close()
        return jsonify({"success": True, "message": "Refund rejected and customer notified. Order reverted to completed."})


@app.route('/order/refund', methods=['POST'])
def request_refund():
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({"success": False, "error": "Not logged in"}), 401

        data = request.get_json() or request.form
        order_id = data.get('order_id')
        reason = (data.get('reason') or data.get('refund_reason') or '').strip()
        notes = data.get('notes') or ''
        if not order_id:
            return jsonify({"success": False, "error": "Missing order_id"}), 400
        # allow reason optional from client, but encourage providing it
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
        order = cursor.fetchone()
        if not order:
            cursor.close()
            return jsonify({"success": False, "error": "Order not found"}), 404
        if order.get('user_id') != user_id:
            cursor.close()
            return jsonify({"success": False, "error": "Permission denied"}), 403
        # Only allow refund requests for delivered orders (customers cannot request after 'completed')
        if (order.get('status') or '').lower() != 'delivered':
            cursor.close()
            return jsonify({"success": False, "error": "Refund can only be requested for delivered orders"}), 400

        try:
            cur = db.cursor()
            cur.execute("UPDATE orders SET status=%s, refund_reason=%s, refund_requested_at=NOW() WHERE id=%s", ("refund_request", reason, order_id))
            cur.execute(
                "INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, 'refund_request', user_id, 'customer', reason, notes)
            )
            # Notify seller and admin
            seller_id = order.get('seller_id')
            notify_cur = db.cursor()
            if seller_id:
                seller_msg = f"[seller:{seller_id}] Customer {user_id} requested a refund for order #{order_id}. Reason: {reason}"
                notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_seller", seller_msg, f"/seller?order_id={order_id}"))
            admin_msg = f"[admin:system] Customer {user_id} requested a refund for order #{order_id}. Reason: {reason}"
            notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
            db.commit()
            cur.close(); notify_cur.close()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            cursor.close()
            return jsonify({"success": False, "error": str(e)}), 500

        cursor.close()
        return jsonify({"success": True, "message": "Refund request submitted and seller notified."})


@app.route('/order/comply_refund', methods=['POST'])
def order_comply_refund():
    """Seller-only endpoint to record acceptance or a decline request for a refund request.
    Seller acceptance approves the refund immediately. Decline requests are sent to admin for final decision.
    """
    user_id = session.get('user_id')
    if not user_id or session.get('role') != 'seller':
        return jsonify({"success": False, "error": "Not authorized"}), 403

    data = request.get_json() or request.form
    order_id = data.get('order_id')
    comply = data.get('comply')
    notes = data.get('notes', '').strip() if data.get('notes') else None
    return_required = data.get('return_required') or data.get('return') or False
    if isinstance(return_required, str):
        return_required = return_required.lower() in ('1', 'true', 'yes', 'on')
    if not order_id or comply is None:
        return jsonify({"success": False, "error": "order_id and comply are required"}), 400

    comply_bool = True if str(comply).lower() in ('1', 'true', 'yes') else False
    if notes == 'Return required':
        return_required = True

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order.get('seller_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403

    if (order.get('status') or '').lower() != 'refund_request':
        cursor.close()
        return jsonify({"success": False, "error": "Order is not awaiting refund confirmation"}), 400

    try:
        cur = db.cursor()
        cur.execute('UPDATE orders SET refund_complied=%s, refund_comply_notes=%s, refund_complied_at=NOW(), refund_complied_by=%s WHERE id=%s', (1 if comply_bool else 0, notes, user_id, order_id))
        if comply_bool:
            reason = order.get('refund_reason', '') or ''
            if return_required:
                cur.execute("UPDATE orders SET status=%s, refund_requested_at=COALESCE(refund_requested_at, NOW()) WHERE id=%s", ("return_request", order_id))
                try:
                    cur.execute("SELECT id FROM return_requests WHERE order_id=%s", (order_id,))
                    rr = cur.fetchone()
                    if not rr:
                        cur.execute(
                            "INSERT INTO return_requests (order_id, requested_by, requested_by_role, reason, status, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                            (order_id, user_id, 'seller', reason, 'requested')
                        )
                except Exception:
                    app.logger.exception("Failed to create return request for seller-approved refund order %s", order_id)
            else:
                cur.execute("UPDATE orders SET status=%s, refunded_at=NOW() WHERE id=%s", ("refunded", order_id))
                try:
                    cur.execute("UPDATE return_requests SET status=%s WHERE order_id=%s", ('cancelled', order_id))
                except Exception:
                    app.logger.exception("Failed to cancel return request for seller-approved refund order %s", order_id)
        # Log action
        cur.execute("INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)", (order_id, 'refund_approve' if comply_bool else 'refund_decline_request', user_id, 'seller', order.get('refund_reason') if comply_bool else None, notes or ''))
        db.commit()
        cur.close()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        cursor.close()
        return jsonify({"success": False, "error": str(e)}), 500

    # Notify admin and customer
    try:
        notify_cur = db.cursor()
        seller_id = user_id
        if comply_bool and return_required:
            admin_msg = f"[admin:system] Seller {seller_id} accepted refund for order #{order_id} and requested return pickup."
        elif comply_bool:
            admin_msg = f"[admin:system] Seller {seller_id} accepted and processed refund for order #{order_id}."
        else:
            admin_msg = f"[admin:system] Seller {seller_id} requested to decline the refund for order #{order_id}"
        notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
        customer_id = order.get('user_id')
        if customer_id:
            if comply_bool and return_required:
                cust_msg = f"[customer:{customer_id}] Seller accepted your refund request for order #{order_id}. Return pickup is required before the refund is completed."
            elif comply_bool:
                cust_msg = f"[customer:{customer_id}] Seller accepted and processed your refund request for order #{order_id}."
            else:
                cust_msg = f"[customer:{customer_id}] Seller requested admin review to decline your refund request for order #{order_id}"
            notify_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", cust_msg, f"/orders?order_id={order_id}"))
        db.commit()
        notify_cur.close()
    except Exception:
        pass

    cursor.close()
    if comply_bool and return_required:
        return jsonify({"success": True, "message": "Refund accepted. Return pickup requested."})
    if comply_bool:
        return jsonify({"success": True, "message": "Refund accepted and processed."})
    return jsonify({"success": True, "message": "Decline request sent to admin."})


@app.route('/order/seller_finalize_refund', methods=['POST'])
def order_seller_finalize_refund():
    """Seller-only endpoint to finalize and process a refund after the returned item has been delivered to the seller.
    Requires that:
    - Session is a seller and owns the order
    - The order has refund_requested_at (admin approved and return flow)
    - The order status is 'delivered' (a return delivery has been completed)
    """
    user_id = session.get('user_id')
    role = session.get('role')
    if not user_id or role != 'seller':
        return jsonify({"success": False, "error": "Not authorized"}), 403

    data = request.get_json() or request.form
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({"success": False, "error": "Missing order_id"}), 400

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order.get('seller_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403
    if not order.get('refund_requested_at'):
        cursor.close()
        return jsonify({"success": False, "error": "This order is not a return flow or admin did not require a return."}), 400
    # ensure last status is delivered so seller indeed received the item
    if (order.get('status') or '').lower() != 'delivered':
        cursor.close()
        return jsonify({"success": False, "error": "Order must be delivered (returned) to seller to finalize refund."}), 400

    try:
        cur = db.cursor()
        # Mark refunded
        cur.execute("UPDATE orders SET status=%s, refunded_at=NOW() WHERE id=%s", ("refunded", order_id))
        cur.execute("INSERT INTO order_action_logs (order_id, action_type, performed_by, performed_role, reason, notes) VALUES (%s, %s, %s, %s, %s, %s)", (order_id, 'seller_refund', user_id, 'seller', None, 'Seller processed refund after receiving returned item'))
        db.commit()
        cur.close()
        # Mark the return_requests entry as refunded and record the finalization
        try:
            rc = db.cursor()
            rc.execute("UPDATE return_requests SET status=%s, seller_finalized_at=NOW(), refund_processed_at=NOW() WHERE order_id=%s", ('refunded', order_id))
            db.commit()
            rc.close()
        except Exception:
            try:
                rc.close()
            except Exception:
                pass
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        cursor.close()
        return jsonify({"success": False, "error": str(e)}), 500

    # Notify customer and admin
    try:
        notif_cur = db.cursor()
        customer_id = order.get('user_id')
        if customer_id:
            cust_msg = f"[customer:{customer_id}] Seller {user_id} processed refund for order #{order_id}."
            notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_customer", cust_msg, f"/orders?order_id={order_id}"))
        admin_msg = f"[admin:system] Seller {user_id} processed refund for order #{order_id}."
        notif_cur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order", admin_msg, f"/admin?order_id={order_id}"))
        db.commit()
        notif_cur.close()
    except Exception:
        try:
            notif_cur.close()
        except Exception:
            pass

    cursor.close()
    return jsonify({"success": True, "message": "Refund processed and customer notified."})


@app.route('/order/mark_return_pickup_requested', methods=['POST'])
def mark_return_pickup_requested():
    user_id = session.get('user_id')
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Not authorized'}), 403
    data = request.get_json() or request.form
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({'success': False, 'error': 'Missing order_id'}), 400
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return jsonify({'success': False, 'error': 'Order not found'}), 404
    # Ensure we have a return_requests row
    cursor.execute("SELECT * FROM return_requests WHERE order_id=%s", (order_id,))
    rr = cursor.fetchone()
    if not rr:
        cursor.close()
        return jsonify({'success': False, 'error': 'No return request exists for this order'}), 400
    cursor.execute("UPDATE return_requests SET status=%s, pickup_requested_at=NOW() WHERE order_id=%s", ('pickup_requested', order_id))
    db.commit()
    cursor.close()
    # Notify riders (simple broadcast)
    try:
        ncur = db.cursor()
        rider_msg = f"[riders:all] Return pickup requested for order #{order_id}."
        ncur.execute("INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)", ("order_rider", rider_msg, f"/rider?order_id={order_id}"))
        db.commit()
        ncur.close()
    except Exception:
        try:
            ncur.close()
        except Exception:
            pass
    return jsonify({'success': True})

@app.route("/get_products")
def get_products():
    user_id = session.get("user_id")
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT p.*, COALESCE(variant_stock.real_stock, p.stock, 0) AS real_stock
        FROM products p
        LEFT JOIN (
            SELECT product_id, SUM(stock) AS real_stock
            FROM product_variants
            GROUP BY product_id
        ) AS variant_stock ON variant_stock.product_id = p.id
        WHERE p.seller_id=%s
        ORDER BY p.created_at DESC
    """, (user_id,))
    products = cursor.fetchall()

    # Fetch variants
    product_ids = [p['id'] for p in products]
    variants_dict = {}
    if product_ids:
        format_strings = ','.join(['%s'] * len(product_ids))
        sql = f"SELECT * FROM product_variants WHERE product_id IN ({format_strings})"
        params = tuple(product_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in product_variants query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        variants = cursor.fetchall()
        for v in variants:
            variants_dict.setdefault(v['product_id'], []).append(v)

    # Merge variants with products
    for p in products:
        p['variants'] = variants_dict.get(p['id'], [])
        if p['variants']:
            p['real_stock'] = sum(int(v.get('stock') or 0) for v in p['variants'])
        if p.get('real_stock') is None:
            p['real_stock'] = p.get('stock') or 0

    cursor.close()
    return jsonify(products)


@app.route('/random_products')
def random_products():
    """Return a small set of random approved products for recommendations. Filter by category if provided."""
    cursor = db.cursor(dictionary=True)
    try:
        # Get category from query parameter (optional)
        category = request.args.get('category', '').strip()
        
        # Check if a reviews table exists in the database
        cursor.execute("SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'product_ratings'")
        tbl = cursor.fetchone()
        has_reviews_table = bool(tbl and tbl.get('cnt', 0) > 0)
        
        # Build query based on category
        if category:
            # Select up to 8 random approved products from the same category (include food fields)
            cursor.execute("SELECT id, name, price, image, seller_id, cuisine_type, preparation_time, servings, allergens, dietary_options, is_spicy, spice_level, is_bestseller FROM products WHERE status='approved' AND category=%s ORDER BY RAND() LIMIT 8", (category,))
        else:
            # Fallback: select random approved products from all categories (include food fields)
            cursor.execute("SELECT id, name, price, image, seller_id, cuisine_type, preparation_time, servings, allergens, dietary_options, is_spicy, spice_level, is_bestseller FROM products WHERE status='approved' ORDER BY RAND() LIMIT 8")
        
        prods = cursor.fetchall()
        results = []
        
        for p in prods:
            image = p.get('image') or 'image/dog.webp'
            # Build absolute static URL for image
            image_url = media_url(image, 'image/dog.webp')
            prod_obj = {
                'id': p.get('id'),
                'name': p.get('name'),
                'price': float(p.get('price') or 0),
                'image_url': image_url,
                'seller_id': p.get('seller_id'),
                'cuisine_type': p.get('cuisine_type'),
                'preparation_time': p.get('preparation_time'),
                'servings': p.get('servings'),
                'allergens': p.get('allergens'),
                'dietary_options': p.get('dietary_options'),
                'is_spicy': bool(p.get('is_spicy')),
                'spice_level': p.get('spice_level'),
                'is_bestseller': bool(p.get('is_bestseller'))
            }
            # attach average rating and count if reviews table exists
            if has_reviews_table:
                cursor.execute("SELECT AVG(rating) AS avg_rating, COUNT(*) AS cnt FROM product_ratings WHERE product_id=%s", (p.get('id'),))
                r = cursor.fetchone()
                avg = float(r.get('avg_rating')) if r and r.get('avg_rating') is not None else 0.0
                prod_obj['rating'] = round(avg, 2)
                prod_obj['rating_count'] = int(r.get('cnt') or 0) if r else 0
            else:
                prod_obj['rating'] = 0.0
                prod_obj['rating_count'] = 0

            results.append(prod_obj)
        return jsonify(results)


    
    finally:
        cursor.close()

@app.route("/admin")
def admin():
    if session.get("role") != "admin":
        flash("Access denied.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor(dictionary=True)

    # 🧹 Auto-delete archived users older than 30 days
    try:
        cursor.execute("""
            DELETE FROM users
            WHERE status = 'archived'
            AND archived_at < NOW() - INTERVAL 30 DAY
        """)
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f"Error during user cleanup: {e}", "error")


    # 🧹 Auto-delete archived products older than 30 days
    try:
        cursor.execute("""
            DELETE FROM products
            WHERE status = 'archived'
            AND archived_at < NOW() - INTERVAL 30 DAY
        """)
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f"Error during product cleanup: {e}", "error")

    # 🧾 Fetch product data
    cursor.execute("SELECT * FROM products WHERE status = 'pending'")
    pending_products = cursor.fetchall()
    cursor.execute("SELECT * FROM products WHERE status = 'approved'")
    approved_products = cursor.fetchall()

    cursor.execute("SELECT * FROM products WHERE status = 'archived'")
    archived_products = cursor.fetchall()

    # 🧾 Fetch user data — separate pending, approved, and archived
    cursor.execute("""
        SELECT u.*, 
               COALESCE(c.phone, s.phone, r.phone) as phone,
               COALESCE(c.date_of_birth, s.date_of_birth, r.date_of_birth) as date_of_birth,
               s.business_name,
               r.vehicle_type,
               r.plate_number
        FROM users u
        LEFT JOIN customers c ON u.id = c.user_id
        LEFT JOIN sellers s ON u.id = s.user_id
        LEFT JOIN riders r ON u.id = r.user_id
        WHERE u.status = 'pending'
    """)
    pending_users = cursor.fetchall()

    cursor.execute("""
        SELECT u.*, 
               COALESCE(c.phone, s.phone, r.phone) as phone,
               COALESCE(c.date_of_birth, s.date_of_birth, r.date_of_birth) as date_of_birth,
               s.business_name,
               r.vehicle_type,
               r.plate_number
        FROM users u
        LEFT JOIN customers c ON u.id = c.user_id
        LEFT JOIN sellers s ON u.id = s.user_id
        LEFT JOIN riders r ON u.id = r.user_id
        WHERE u.status = 'approved'
    """)
    approved_users = cursor.fetchall()

    cursor.execute("""
        SELECT u.*, 
               COALESCE(c.phone, s.phone, r.phone) as phone,
               COALESCE(c.date_of_birth, s.date_of_birth, r.date_of_birth) as date_of_birth,
               s.business_name,
               r.vehicle_type,
               r.plate_number
        FROM users u
        LEFT JOIN customers c ON u.id = c.user_id
        LEFT JOIN sellers s ON u.id = s.user_id
        LEFT JOIN riders r ON u.id = r.user_id
        WHERE u.status = 'archived'
    """)
    archived_users = cursor.fetchall()

    # Fetch notifications (latest 20) but exclude seller-, customer-, and rider-targeted notifications
    cursor.execute("SELECT * FROM notifications WHERE `type` NOT IN (%s,%s,%s) ORDER BY created_at DESC LIMIT 20", ("order_seller","order_customer","order_rider",))
    notifications = cursor.fetchall()
    # Fetch all orders for admin view
    cursor.execute("SELECT * FROM orders ORDER BY created_at DESC")
    orders = cursor.fetchall()

    order_items_by_order = {}
    riders_map = {}
    proofs_map = {}
    if orders:
        order_ids = [o['id'] for o in orders]
        format_order_ids = ','.join(['%s'] * len(order_ids))
        sql = f"""
            SELECT oi.*, p.name AS product_name, p.image AS product_image, p.seller_id, pv.color, pv.size
            FROM order_items oi
            LEFT JOIN products p ON oi.product_id = p.id
            LEFT JOIN product_variants pv ON oi.variant_id = pv.id
            WHERE oi.order_id IN ({format_order_ids})
        """
        params = tuple(order_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in admin order_items query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        items = cursor.fetchall()
        for item in items:
            order_items_by_order.setdefault(item['order_id'], []).append(item)

        # Build mapping of sellers per order (collect seller_ids from items)
        sellers_by_order = {}
        for it in items:
            sid = it.get('seller_id')
            if sid:
                sellers_by_order.setdefault(it['order_id'], set()).add(sid)

        # resolve seller ids to names and addresses in one query
        all_seller_ids = set()
        for sset in sellers_by_order.values():
            all_seller_ids.update(sset)
        sellers_name_map = {}
        sellers_address_map = {}
        if all_seller_ids:
            format_sellers = ','.join(['%s'] * len(all_seller_ids))
            sql = f"SELECT id, fullname FROM users WHERE id IN ({format_sellers})"
            params = tuple(all_seller_ids)
            if sql.count('%s') != len(params):
                app.logger.error("SQL placeholder mismatch in admin sellers id query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
            execute_safe(cursor, sql, params)
            rows = cursor.fetchall()
            for r in (rows or []):
                sellers_name_map[r['id']] = r.get('fullname') or str(r['id'])

            # fetch default address (is_default = 1) or latest address for each seller
            sql = f"SELECT user_id, province_name, city_name, barangay_name, street, is_default FROM addresses WHERE user_id IN ({format_sellers}) ORDER BY is_default DESC"
            params = tuple(all_seller_ids)
            if sql.count('%s') != len(params):
                app.logger.error("SQL placeholder mismatch in admin sellers addresses query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
            execute_safe(cursor, sql, params)
            addr_rows = cursor.fetchall()
            # pick the first address encountered per user (ORDER BY is_default DESC places default first)
            seen = set()
            for a in (addr_rows or []):
                uid = a.get('user_id')
                if uid in seen:
                    continue
                seen.add(uid)
                parts = [a.get('province_name') or '', a.get('city_name') or '', a.get('barangay_name') or '', a.get('street') or '']
                addr = ', '.join([p for p in parts if p])
                sellers_address_map[uid] = addr or '-'

        # attach sellers info list to orders (list of names, and addresses)
        sellers_info_by_order = {}
        for oid, sset in sellers_by_order.items():
            names = [sellers_name_map.get(sid, str(sid)) for sid in sorted(sset)]
            infos = []
            for sid in sorted(sset):
                infos.append({
                    'id': sid,
                    'name': sellers_name_map.get(sid, str(sid)),
                    'address': sellers_address_map.get(sid, '-')
                })
            sellers_info_by_order[oid] = infos
            sellers_by_order[oid] = names

        # Fetch assigned riders for these orders
        sql = f"SELECT ordr.order_id, u.fullname AS rider_name, u.id AS rider_id FROM order_riders ordr JOIN users u ON ordr.rider_id = u.id WHERE ordr.order_id IN ({format_order_ids})"
        params = tuple(order_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in admin assigned riders query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        assigned_riders = cursor.fetchall()
        riders_map = {r['order_id']: r for r in (assigned_riders or [])}

        # Fetch delivery proofs
        sql = f"SELECT * FROM delivery_proofs WHERE order_id IN ({format_order_ids})"
        params = tuple(order_ids)
        if sql.count('%s') != len(params):
            app.logger.error("SQL placeholder mismatch in admin delivery_proofs query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
        execute_safe(cursor, sql, params)
        proof_rows = cursor.fetchall()
        for p in (proof_rows or []):
            proofs_map.setdefault(p['order_id'], []).append(p['file_path'])
        # Fetch return_requests for these orders and map them
        try:
            sql = f"SELECT * FROM return_requests WHERE order_id IN ({format_order_ids})"
            params = tuple(order_ids)
            if sql.count('%s') != len(params):
                app.logger.error("SQL placeholder mismatch in admin return_requests query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
            execute_safe(cursor, sql, params)
            rr_rows = cursor.fetchall() or []
            return_requests_map = {}
            requested_by_ids = set()
            for r in rr_rows:
                return_requests_map[r['order_id']] = r
                if r.get('requested_by'):
                    requested_by_ids.add(r.get('requested_by'))
            # map requester ids to fullname
            if requested_by_ids:
                fmt_req = ','.join(['%s'] * len(requested_by_ids))
                sql = f"SELECT id, fullname FROM users WHERE id IN ({fmt_req})"
                params = tuple(requested_by_ids)
                if sql.count('%s') != len(params):
                    app.logger.error("SQL placeholder mismatch in admin requested_by users query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
                execute_safe(cursor, sql, params)
                names_rows = cursor.fetchall() or []
                name_map = {n['id']: n.get('fullname') for n in names_rows}
                # add a `requested_by_name` key to each rr
                for r in rr_rows:
                    r['requested_by_name'] = name_map.get(r.get('requested_by'))
        except Exception:
            return_requests_map = {}

    # Enrich orders with computed fields used in the template
    for order in (orders or []):
        try:
            cursor2 = db.cursor(dictionary=True)
            cursor2.execute("SELECT fullname FROM users WHERE id = %s", (order.get('user_id'),))
            user_row = cursor2.fetchone()
            order['customer_name'] = user_row['fullname'] if user_row and user_row.get('fullname') else str(order.get('user_id'))
            cursor2.close()
        except Exception:
            order['customer_name'] = str(order.get('user_id'))

        items = order_items_by_order.get(order['id'], [])
        order['total_amount'] = sum((item.get('price', 0) or 0) * (item.get('quantity', 1) or 1) for item in items)

        # attach sellers info collected earlier (if any)
        try:
            sellers_list = sellers_by_order.get(order['id'], []) if 'sellers_by_order' in locals() else []
        except Exception:
            sellers_list = []
        order['sellers'] = sellers_list
        order['sellers_display'] = ', '.join(sellers_list) if sellers_list else '-'

        # attach sellers info with addresses if available
        try:
            sellers_info = sellers_info_by_order.get(order['id'], []) if 'sellers_info_by_order' in locals() else []
        except Exception:
            sellers_info = []
        order['sellers_info'] = sellers_info
        # also a compact addresses display for quick view
        if sellers_info:
            order['sellers_addresses_display'] = ', '.join([s.get('address', '-') for s in sellers_info])
        else:
            order['sellers_addresses_display'] = '-'

        rid = riders_map.get(order['id'])
        if rid:
            order['rider_name'] = rid.get('rider_name')
            order['rider_id'] = rid.get('rider_id')
            order['rider_assigned'] = True
        else:
            order['rider_name'] = None
            order['rider_id'] = None
            order['rider_assigned'] = False

        order['proofs'] = proofs_map.get(order['id'], [])
        order['return_request'] = return_requests_map.get(order['id']) if 'return_requests_map' in locals() else None

        # total weight (kg)
        try:
            wcur = db.cursor(dictionary=True)
            wcur.execute("SELECT SUM(pv.weight_kg * oi.quantity) AS total_weight FROM order_items oi LEFT JOIN product_variants pv ON oi.variant_id = pv.id WHERE oi.order_id = %s", (order['id'],))
            w = wcur.fetchone()
            order['total_weight'] = round((w and w.get('total_weight')) or 0, 2)
        finally:
            try:
                wcur.close()
            except Exception:
                pass

        # customer/seller address snapshots
        import json
        def extract_addr(snap):
            addr = '-'
            if snap:
                try:
                    data = json.loads(snap) if isinstance(snap, str) else snap
                    addr = ', '.join(filter(None, [
                        data.get('province_name'),
                        data.get('city_name'),
                        data.get('barangay_name'),
                        data.get('street')
                    ]))
                    if not addr:
                        addr = ', '.join(str(v) for v in data.values() if isinstance(v, str) and v.strip())
                except Exception:
                    addr = str(snap)
            return addr or '-'

        order['customer_full_address'] = extract_addr(order.get('customer_address_snapshot'))
        order['seller_full_address'] = extract_addr(order.get('seller_address_snapshot'))
        # compute admin fee and sellers earnings per order (prefer earnings first, fallback to income)
        try:
            fee_cur = db.cursor()
            fee_cur.execute("SELECT COALESCE((SELECT SUM(amount) FROM earnings WHERE order_id=%s AND role='admin'), (SELECT SUM(amount) FROM income WHERE order_id=%s AND role='admin'), 0)", (order['id'], order['id']))
            order['admin_earning'] = fee_cur.fetchone()[0] or 0
            fee_cur.execute("SELECT COALESCE((SELECT SUM(amount) FROM earnings WHERE order_id=%s AND role='seller'), (SELECT SUM(amount) FROM income WHERE order_id=%s AND role='seller'), 0)", (order['id'], order['id']))
            order['sellers_total_income'] = fee_cur.fetchone()[0] or 0
            fee_cur.close()
        except Exception:
            order['admin_earning'] = 0
            order['sellers_total_income'] = 0

    # Attach a simple sequential number for admin view
    try:
        sorted_orders = sorted(orders, key=lambda o: (o.get('created_at') is None, o.get('created_at') or o.get('id')))
    except Exception:
        sorted_orders = list(orders or [])
    for idx, ord_obj in enumerate(sorted_orders, start=1):
        ord_obj['shop_order_number'] = idx

    # Map refund_complied_by user ids to names to show in admin UI
    try:
        refund_complied_by_ids = sorted({o.get('refund_complied_by') for o in (orders or []) if o.get('refund_complied_by')})
        refund_complied_by_name_map = {}
        if refund_complied_by_ids:
            fmt = ','.join(['%s'] * len(refund_complied_by_ids))
            sql = f"SELECT id, fullname FROM users WHERE id IN ({fmt})"
            params = tuple(refund_complied_by_ids)
            if sql.count('%s') != len(params):
                app.logger.error("SQL placeholder mismatch in admin refund_complied_by_ids query: placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
            execute_safe(cursor, sql, params)
            rows = cursor.fetchall() or []
            refund_complied_by_name_map = {r['id']: r.get('fullname') or str(r['id']) for r in rows}
        for o in (orders or []):
            rid = o.get('refund_complied_by')
            o['refund_complied_by_name'] = refund_complied_by_name_map.get(rid) if rid else None
    except Exception:
        # Don't hard-fail admin view if this mapping calculation fails
        for o in (orders or []):
            if 'refund_complied_by' in o and o.get('refund_complied_by'):
                o['refund_complied_by_name'] = None

    # Fetch admin/system income data
    income_cursor = db.cursor(dictionary=True)
    income_cursor.execute('''
        SELECT COALESCE((SELECT SUM(amount) FROM earnings WHERE role = 'admin'), (SELECT SUM(amount) FROM income WHERE role = 'admin'), 0) AS total_income,
               COALESCE((SELECT COUNT(DISTINCT order_id) FROM earnings WHERE role = 'admin'), (SELECT COUNT(DISTINCT order_id) FROM income WHERE role = 'admin'), 0) AS completed_orders
        ''')
    admin_income_data = income_cursor.fetchone() or {}
    admin_total_income = float(admin_income_data.get('total_income', 0) or 0)
    admin_completed_orders = int(admin_income_data.get('completed_orders', 0) or 0)
    
    # Fetch monthly income breakdown
    income_cursor.execute('''
        SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, SUM(amount) AS monthly_amount
        FROM income
        WHERE role = 'admin'
        GROUP BY DATE_FORMAT(created_at, '%Y-%m')
        ORDER BY month DESC
        LIMIT 12
    ''')
    admin_monthly_income = income_cursor.fetchall() or []
    
    # Fetch all sellers' income data
    income_cursor.execute('''
        SELECT u.id, u.fullname, COALESCE(e.total_income, i.total_income, 0) AS total_income, COALESCE(e.completed_orders, i.completed_orders, 0) AS completed_orders
        FROM users u
        LEFT JOIN (
            SELECT user_id, SUM(amount) AS total_income, COUNT(DISTINCT order_id) AS completed_orders
            FROM earnings
            WHERE role = 'seller'
            GROUP BY user_id
        ) e ON e.user_id = u.id
        LEFT JOIN (
            SELECT user_id, SUM(amount) AS total_income, COUNT(DISTINCT order_id) AS completed_orders
            FROM income
            WHERE role = 'seller'
            GROUP BY user_id
        ) i ON i.user_id = u.id
        WHERE u.role = 'seller'
        ORDER BY total_income DESC
    ''')
    sellers_income = income_cursor.fetchall() or []
    income_cursor.close()
    # Admin earnings today
    try:
        today_cur = db.cursor()
        today_cur.execute("SELECT COALESCE((SELECT SUM(amount) FROM earnings WHERE role='admin' AND DATE(created_at)=CURDATE()), (SELECT SUM(amount) FROM income WHERE role='admin' AND DATE(created_at)=CURDATE()), 0)")
        admin_earnings_today = today_cur.fetchone()[0] or 0
        today_cur.close()
    except Exception:
        admin_earnings_today = 0
    # Admin stats: today's order count and active users
    stats_cursor = db.cursor()
    stats_cursor.execute("SELECT COUNT(*) FROM orders WHERE DATE(created_at) = CURDATE()")
    orders_today = stats_cursor.fetchone()[0] if stats_cursor.rowcount != 0 else 0
    stats_cursor.execute("SELECT COUNT(*) FROM users WHERE status = %s", ('approved',))
    active_users = stats_cursor.fetchone()[0] if stats_cursor.rowcount != 0 else 0
    stats_cursor.close()

    cursor.close()
    # Admin's own user info for settings/profile
    try:
        ucur = db.cursor(dictionary=True)
        ucur.execute("SELECT id, fullname, email, profile_pic FROM users WHERE id=%s", (session.get('user_id'),))
        user = ucur.fetchone() or {}
        ucur.close()
    except Exception:
        user = {}

    return render_template(
        "admin.html",
        pending_users=pending_users,
        approved_users=approved_users,
        archived_users=archived_users,
        pending_products=pending_products,
        approved_products=approved_products,
        archived_products=archived_products,
        notifications=notifications,
        orders=orders,
        order_items=order_items_by_order,
        admin_total_income=admin_total_income,
        admin_completed_orders=admin_completed_orders,
        admin_monthly_income=admin_monthly_income,
        sellers_income=sellers_income,
        orders_today=orders_today,
        active_users=active_users
        ,admin_earnings_today=admin_earnings_today
        ,admin_return_requests=(list(return_requests_map.values()) if 'return_requests_map' in locals() else []),
        user=user
    )

@app.route('/admin/user-info/<int:user_id>')
def user_info(user_id):
    # Basic user
    cursor.execute("SELECT * FROM users WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404

    # Addresses (could be multiple)
    cursor.execute("SELECT * FROM addresses WHERE user_id=%s", (user_id,))
    addresses = cursor.fetchall()

    # Role-specific tables
    extra = {}
    if user['role'] == 'customer':
        cursor.execute("SELECT * FROM customers WHERE user_id=%s", (user_id,))
        cust = cursor.fetchone()
        if cust:
            extra.update({
                'phone': cust.get('phone'),
                'date_of_birth': cust.get('date_of_birth').strftime('%Y-%m-%d') if cust.get('date_of_birth') else None
            })
    elif user['role'] == 'seller':
        cursor.execute("SELECT * FROM sellers WHERE user_id=%s", (user_id,))
        seller = cursor.fetchone()
        if seller:
            extra.update({
                'business_name': seller.get('business_name'),
                'business_permit': seller.get('business_permit'),
                'phone': seller.get('phone'),
                'date_of_birth': seller.get('date_of_birth').strftime('%Y-%m-%d') if seller.get('date_of_birth') else None
            })
    elif user['role'] == 'rider':
        cursor.execute("SELECT * FROM riders WHERE user_id=%s", (user_id,))
        rider = cursor.fetchone()
        if rider:
            extra.update({
                'vehicle_type': rider.get('vehicle_type'),
                'plate_number': rider.get('plate_number'),
                'drivers_license': rider.get('drivers_license'),
                'phone': rider.get('phone'),
                'date_of_birth': rider.get('date_of_birth').strftime('%Y-%m-%d') if rider.get('date_of_birth') else None
            })

    # Build response object with safe values
    def normalize_path(p):
        if not p:
            return None
        return media_url(p)

    user_data = {
        'id': user['id'],
        'fullname': user.get('fullname'),
        'email': user.get('email'),
        'role': user.get('role'),
        'status': user.get('status'),
        'profile_pic': normalize_path(user.get('profile_pic')),
        'id_picture': normalize_path(user.get('id_picture')),
        'addresses': addresses,
    }
    # Normalize any file fields from extra
    if 'business_permit' in extra:
        extra['business_permit'] = normalize_path(extra.get('business_permit'))
    if 'drivers_license' in extra:
        extra['drivers_license'] = normalize_path(extra.get('drivers_license'))
    if 'profile_pic' in extra:
        extra['profile_pic'] = normalize_path(extra.get('profile_pic'))

    user_data.update(extra)

    return jsonify(user_data)

@app.route('/buy_again/<int:order_id>', methods=['POST'])
def buy_again(order_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in first.', 'error')
        return redirect(url_for('loginreg'))

    cursor = db.cursor(dictionary=True)
    # Fetch all items from the order
    cursor.execute("""
        SELECT product_id, variant_id, quantity
        FROM order_items
        WHERE order_id = %s
    """, (order_id,))
    items = cursor.fetchall()

    # Add each item to the cart (do not increment if already exists)
    for item in items:
        product_id = item['product_id']
        variant_id = item['variant_id']
        quantity = item['quantity']
        # Check if already in cart
        if variant_id:
            cursor.execute("SELECT id FROM cart WHERE user_id=%s AND product_id=%s AND variant_id=%s", (user_id, product_id, variant_id))
        else:
            cursor.execute("SELECT id FROM cart WHERE user_id=%s AND product_id=%s AND variant_id IS NULL", (user_id, product_id))
        existing = cursor.fetchone()
        if not existing:
            cursor.execute("INSERT INTO cart (user_id, product_id, variant_id, quantity) VALUES (%s, %s, %s, %s)", (user_id, product_id, variant_id, quantity))

    db.commit()
    cursor.close()
    flash('Items added to cart!', 'success')
    return redirect(url_for('cart'))

@app.route('/admin/product-info/<int:product_id>')
def product_info(product_id):
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT p.*, s.business_name, s.user_id AS seller_user_id FROM products p LEFT JOIN sellers s ON p.seller_id = s.user_id WHERE p.id=%s", (product_id,))
    product = cursor.fetchone()
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    # Variants
    cursor.execute("SELECT * FROM product_variants WHERE product_id=%s", (product_id,))
    variants = cursor.fetchall()

    # Seller address (first/default)
    seller_addr = None
    if product.get('seller_id'):
        cursor.execute("SELECT * FROM addresses WHERE user_id=%s ORDER BY is_default DESC, created_at ASC LIMIT 1", (product.get('seller_id'),))
        seller_addr = cursor.fetchone()

    def normalize_path(p):
        if not p:
            return None
        return media_url(p)

    # Fetch seller full name from users table
    seller_fullname = None
    if product.get('seller_id'):
        cursor.execute("SELECT fullname FROM users WHERE id=%s", (product.get('seller_id'),))
        seller_user = cursor.fetchone()
        seller_fullname = seller_user['fullname'] if seller_user and seller_user.get('fullname') else None

    prod = {
        'id': product['id'],
        'name': product.get('name'),
        'category': product.get('category'),
        'description': product.get('description'),
        'price': product.get('price'),
        'stock': product.get('stock'),
        'image': normalize_path(product.get('image')),
        'status': product.get('status'),
        'seller': {
            'business_name': product.get('business_name'),
            'seller_id': product.get('seller_id'),
            'fullname': seller_fullname
        },
        'variants': [],
        'seller_address': seller_addr
    }

    for v in variants:
        prod['variants'].append({
            'id': v.get('id'),
            'color': v.get('color'),
            'size': v.get('size'),
            'price': v.get('price'),
            'stock': v.get('stock'),
            'image': normalize_path(v.get('image')),
            'weight_kg': v.get('weight_kg')
        })

    return jsonify(prod)

@app.route("/rider")
def rider():
    if session.get("role") != "rider":
        flash("Access denied.", "error")
        return redirect(url_for("loginreg"))

    user_id = session.get("user_id")
    cursor = db.cursor(dictionary=True)
    # Get rider's vehicle type
    execute_safe(cursor, "SELECT vehicle_type FROM riders WHERE user_id=%s", (user_id,))
    rider_info = cursor.fetchone() or {}
    vehicle_type = rider_info.get("vehicle_type", "motorcycle")

    # Show assigned orders for this rider
    execute_safe(cursor, """
        SELECT o.*, sa.street AS pickup_address, CONCAT_WS(', ', sa.street, sa.barangay_name, sa.city_name, sa.province_name, sa.region_name) AS seller_full_address, ca.street AS delivery_address, CONCAT_WS(', ', ca.street, ca.barangay_name, ca.city_name, ca.province_name, ca.region_name) AS customer_full_address,
               r.vehicle_type, orr.status AS rider_status,
               su.fullname AS seller_name, cu.fullname AS customer_name
        FROM order_riders orr
        JOIN orders o ON orr.order_id = o.id
        LEFT JOIN addresses sa ON sa.user_id = o.seller_id AND sa.is_default = 1
        LEFT JOIN addresses ca ON ca.user_id = o.user_id AND ca.is_default = 1
        LEFT JOIN riders r ON orr.rider_id = r.user_id
        LEFT JOIN users su ON o.seller_id = su.id
        LEFT JOIN users cu ON o.user_id = cu.id
        WHERE orr.rider_id = %s
          AND (
              o.status IN ('ready_for_pickup', 'assigned', 'processing')
              OR orr.status IN ('assigned','accepted')
          )
        ORDER BY o.created_at DESC
    """, (user_id,))
    rider_orders = cursor.fetchall()

    # Show available ready_for_pickup orders matching rider's vehicle type (not yet assigned)
    execute_safe(cursor, """
        SELECT o.*, sa.street AS pickup_address, CONCAT_WS(', ', sa.street, sa.barangay_name, sa.city_name, sa.province_name, sa.region_name) AS seller_full_address, ca.street AS delivery_address, CONCAT_WS(', ', ca.street, ca.barangay_name, ca.city_name, ca.province_name, ca.region_name) AS customer_full_address,
               su.fullname AS seller_name, cu.fullname AS customer_name
        FROM orders o
        LEFT JOIN addresses sa ON sa.user_id = o.seller_id AND sa.is_default = 1
        LEFT JOIN addresses ca ON ca.user_id = o.user_id AND ca.is_default = 1
        LEFT JOIN users su ON o.seller_id = su.id
        LEFT JOIN users cu ON o.user_id = cu.id
        WHERE o.status = 'ready_for_pickup' AND o.vehicle = %s
        AND NOT EXISTS (SELECT 1 FROM order_riders orr WHERE orr.order_id = o.id)
        ORDER BY o.created_at DESC
    """, (vehicle_type,))
    available_orders = cursor.fetchall()

    import json
    def extract_addr(snap):
        addr = '-'
        if snap:
            try:
                data = json.loads(snap) if isinstance(snap, str) else snap
                addr = ', '.join(filter(None, [
                    data.get('province_name'),
                    data.get('city_name'),
                    data.get('barangay_name'),
                    data.get('street')
                ]))
                if not addr:
                    addr = ', '.join(str(v) for v in data.values() if isinstance(v, str) and v.strip())
            except Exception:
                addr = str(snap)
        return addr or '-'

    # Attach total_weight, total_amount, customer_full_address, and seller_full_address for each available order
    for order in available_orders:
        execute_safe(cursor, "SELECT SUM(pv.weight_kg * oi.quantity) AS total_weight, SUM(oi.price * oi.quantity) AS total_amount FROM order_items oi LEFT JOIN product_variants pv ON oi.variant_id = pv.id WHERE oi.order_id = %s", (order["id"],))
        w = cursor.fetchone()
        order["total_weight"] = round(w["total_weight"] or 0, 2)
        order["total_amount"] = round(w["total_amount"] or 0, 2)
        order["customer_full_address"] = extract_addr(order.get('customer_address_snapshot'))
        order["seller_full_address"] = extract_addr(order.get('seller_address_snapshot'))

    # Attach total_weight, total_amount, customer_full_address, and seller_full_address for each rider order
    for order in rider_orders:
        execute_safe(cursor, "SELECT SUM(pv.weight_kg * oi.quantity) AS total_weight, SUM(oi.price * oi.quantity) AS total_amount FROM order_items oi LEFT JOIN product_variants pv ON oi.variant_id = pv.id WHERE oi.order_id = %s", (order["id"],))
        w = cursor.fetchone()
        order["total_weight"] = round(w["total_weight"] or 0, 2)
        order["total_amount"] = round(w["total_amount"] or 0, 2)
        order["customer_full_address"] = extract_addr(order.get('customer_address_snapshot'))
        order["seller_full_address"] = extract_addr(order.get('seller_address_snapshot'))

    # Also fetch all accepted deliveries for this rider (order_riders.status = assigned/accepted)
    cursor = db.cursor(dictionary=True)
    execute_safe(cursor, """
        SELECT o.*, sa.street AS pickup_address, ca.street AS delivery_address,
               r.vehicle_type, orr.status AS rider_status,
               su.fullname AS seller_name, cu.fullname AS customer_name,
               CONCAT_WS(', ', sa.street, sa.barangay_name, sa.city_name, sa.province_name, sa.region_name) AS seller_full_address,
               CONCAT_WS(', ', ca.street, ca.barangay_name, ca.city_name, ca.province_name, ca.region_name) AS customer_full_address,
               COALESCE(
                   (SELECT SUM(amount) FROM earnings e WHERE e.order_id = o.id AND e.user_id = %s AND e.role='rider'),
                   (SELECT SUM(amount) FROM income i WHERE i.order_id = o.id AND i.user_id = %s AND i.role='rider'),
                   0
               ) AS rider_earning
        FROM order_riders orr
        JOIN orders o ON orr.order_id = o.id
        LEFT JOIN addresses sa ON sa.user_id = o.seller_id AND sa.is_default = 1
        LEFT JOIN addresses ca ON ca.user_id = o.user_id AND ca.is_default = 1
        LEFT JOIN riders r ON orr.rider_id = r.user_id
        LEFT JOIN users su ON o.seller_id = su.id
        LEFT JOIN users cu ON o.user_id = cu.id
        WHERE orr.rider_id = %s AND orr.status IN ('assigned', 'accepted')
        ORDER BY o.created_at DESC
    """, (user_id, user_id, user_id))
    accepted_orders = cursor.fetchall()
    # Attach totals for accepted orders
    for order in accepted_orders:
        cursor.execute("SELECT SUM(pv.weight_kg * oi.quantity) AS total_weight, SUM(oi.price * oi.quantity) AS total_amount FROM order_items oi LEFT JOIN product_variants pv ON oi.variant_id = pv.id WHERE oi.order_id = %s", (order["id"],))
        w = cursor.fetchone()
        order["total_weight"] = round(w["total_weight"] or 0, 2)
        order["total_amount"] = round(w["total_amount"] or 0, 2)

    # Rider income statistics
    income_cur = db.cursor(dictionary=True)
    sql = '''
        SELECT COALESCE((SELECT SUM(amount) FROM earnings WHERE user_id=%s AND role='rider'), (SELECT SUM(amount) FROM income WHERE user_id=%s AND role='rider'), 0) AS total_income,
               COALESCE((SELECT COUNT(DISTINCT order_id) FROM earnings WHERE user_id=%s AND role='rider'), (SELECT COUNT(DISTINCT order_id) FROM income WHERE user_id=%s AND role='rider'), 0) AS completed_orders
    '''
    params = (user_id, user_id, user_id, user_id)
    if sql.count('%s') != len(params):
        app.logger.error("RIDER income_cur SQL placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
    execute_safe(income_cur, sql, params)
    rider_income_data = income_cur.fetchone() or {}
    rider_total_income = float(rider_income_data.get('total_income', 0) or 0)
    rider_completed_orders = int(rider_income_data.get('completed_orders', 0) or 0)
    
    # Get monthly income with delivery counts for Reports section
    sql = '''
        SELECT 
            DATE_FORMAT(o.created_at, '%Y-%m') AS month,
            DATE_FORMAT(o.created_at, '%b %Y') AS month_name,
            COUNT(DISTINCT o.id) AS deliveries,
            COALESCE(SUM(CASE WHEN o.status IN ('delivered','completed') THEN 1 ELSE 0 END), 0) AS successful_deliveries,
            COALESCE(SUM(COALESCE(ri.amount, 0)), 0) AS monthly_amount
        FROM order_riders orr
        JOIN orders o ON orr.order_id = o.id
        LEFT JOIN (
            SELECT order_id, SUM(amount) AS amount
            FROM income
            WHERE user_id = %s AND role = 'rider'
            GROUP BY order_id
        ) ri ON ri.order_id = o.id
        WHERE orr.rider_id = %s AND o.created_at IS NOT NULL
        GROUP BY DATE_FORMAT(o.created_at, '%Y-%m'), DATE_FORMAT(o.created_at, '%b %Y')
        ORDER BY DATE_FORMAT(o.created_at, '%Y-%m') DESC
        LIMIT 12
    '''
    params = (user_id, user_id)
    if sql.count('%s') != len(params):
        app.logger.error("RIDER monthly income SQL placeholders=%s params=%s SQL=%s", sql.count('%s'), len(params), sql)
    execute_safe(income_cur, sql, params)
    rider_monthly_income = income_cur.fetchall() or []
    if DEBUG_SQL_MISMATCH:
        try:
            app.logger.info('rider_monthly_income rows=%s', rider_monthly_income)
        except Exception:
            pass
    
    # Calculate Reports metrics - initialize variables first
    success_rate = 0
    avg_rating = 0
    avg_earning_per_delivery = 0
    rider_total_deliveries = 0
    # Compute on-route count and today's assigned count for rider
    on_route = len([o for o in rider_orders if (o.get('status') or '').lower() in ('assigned','accepted','processing','shipped')]) if rider_orders else 0
    today_assigned_count = 0
    try:
        tcur = db.cursor()
        execute_safe(tcur, "SELECT COUNT(*) FROM order_riders orr JOIN orders o ON orr.order_id=o.id WHERE orr.rider_id=%s AND DATE(o.created_at)=CURDATE()", (user_id,))
        today_assigned_count = tcur.fetchone()[0] if tcur.rowcount != 0 else 0
        tcur.close()
    except Exception:
        today_assigned_count = 0

    # Rider earnings today
    try:
        inc_cur = db.cursor()
        execute_safe(inc_cur, "SELECT SUM(amount) FROM income WHERE user_id=%s AND role='rider' AND DATE(created_at)=CURDATE()", (user_id,))
        earnings_today = inc_cur.fetchone()[0] or 0
        inc_cur.close()
    except Exception:
        earnings_today = 0

    # Fetch delivery history (delivered/completed) for this rider
    history = []
    try:
        hcur = db.cursor(dictionary=True)
        execute_safe(hcur, '''
                 SELECT o.id AS order_id, o.created_at, o.shipping_fee, CONCAT_WS(', ', sa.street, sa.barangay_name, sa.city_name, sa.province_name, sa.region_name) AS seller_full_address, CONCAT_WS(', ', ca.street, ca.barangay_name, ca.city_name, ca.province_name, ca.region_name) AS customer_full_address,
                     COALESCE(
                      (SELECT SUM(amount) FROM earnings e WHERE e.order_id = o.id AND e.user_id=%s AND e.role='rider'),
                      (SELECT SUM(amount) FROM income i WHERE i.order_id = o.id AND i.user_id=%s AND i.role='rider'),
                      0
                     ) AS rider_earning,
                     COALESCE((SELECT rating FROM ratings WHERE order_id = o.id AND rated_user_id = %s AND role = 'rider' LIMIT 1), NULL) AS rider_rating
            FROM order_riders orr
            JOIN orders o ON orr.order_id = o.id
            LEFT JOIN addresses sa ON sa.user_id = o.seller_id AND sa.is_default = 1
            LEFT JOIN addresses ca ON ca.user_id = o.user_id AND ca.is_default = 1
            WHERE orr.rider_id = %s AND o.status IN ('delivered', 'completed')
            ORDER BY o.created_at DESC LIMIT 20
        ''', (user_id, user_id, user_id, user_id))
        history = hcur.fetchall() or []
        hcur.close()
    except Exception:
        history = []

    cursor.close()
    # Earnings this month
    try:
        month_cur = db.cursor()
        execute_safe(month_cur, "SELECT SUM(amount) FROM income WHERE user_id=%s AND role='rider' AND DATE_FORMAT(created_at, '%Y-%m') = DATE_FORMAT(CURDATE(), '%Y-%m')", (user_id,))
        rider_month_total = month_cur.fetchone()[0] or 0
        month_cur.close()
    except Exception:
        rider_month_total = 0

    # Count completed deliveries today
    rider_completed_today = 0
    try:
        ccur = db.cursor()
        execute_safe(ccur, "SELECT COUNT(DISTINCT o.id) FROM order_riders orr JOIN orders o ON orr.order_id=o.id WHERE orr.rider_id=%s AND o.status IN ('delivered','completed') AND DATE(o.created_at)=CURDATE()", (user_id,))
        rider_completed_today = ccur.fetchone()[0] or 0
        ccur.close()
    except Exception:
        rider_completed_today = 0

    pending_payouts = 0
    # Total delivered/completed deliveries for this rider (lifetime)
    try:
        tcur = db.cursor()
        execute_safe(tcur, "SELECT COUNT(DISTINCT o.id) FROM order_riders orr JOIN orders o ON orr.order_id=o.id WHERE orr.rider_id=%s AND o.status IN ('delivered','completed')", (user_id,))
        rider_total_deliveries = tcur.fetchone()[0] or 0
        tcur.close()
    except Exception:
        rider_total_deliveries = 0
    
    # Calculate Reports metrics now that rider_total_deliveries is set
    if rider_total_deliveries > 0:
        avg_earning_per_delivery = rider_total_income / rider_total_deliveries
    
    # Get success rate (completed/delivered vs total)
    try:
        sr_cur = db.cursor()
        execute_safe(sr_cur, 
            "SELECT COUNT(DISTINCT o.id) FROM order_riders orr JOIN orders o ON orr.order_id=o.id WHERE orr.rider_id=%s AND o.status IN ('delivered','completed')",
            (user_id,))
        successful_deliveries = sr_cur.fetchone()[0] or 0
        sr_cur.close()
        if rider_total_deliveries > 0:
            success_rate = round((successful_deliveries / rider_total_deliveries) * 100, 1)
    except Exception:
        success_rate = 0
    
    # Get average rating from order ratings
    try:
        ar_cur = db.cursor()
        execute_safe(ar_cur,
            '''SELECT AVG(r.rating) FROM ratings r 
               JOIN order_riders orr ON r.order_id = orr.order_id 
               WHERE orr.rider_id = %s AND r.role = 'rider' ''',
            (user_id,))
        avg_rating = ar_cur.fetchone()[0] or 0
        if avg_rating:
            avg_rating = round(avg_rating, 1)
        ar_cur.close()
    except Exception:
        avg_rating = 0
    try:
        addr_cur = db.cursor(dictionary=True)
        execute_safe(addr_cur, "SELECT * FROM addresses WHERE user_id=%s", (user_id,))
        addresses = addr_cur.fetchall() or []
        addr_cur.close()
    except Exception:
        addresses = []

    return render_template("rider.html", rider_orders=rider_orders, available_orders=available_orders, accepted_orders=accepted_orders, rider_total_income=rider_total_income, rider_completed_orders=rider_completed_orders, rider_monthly_income=rider_monthly_income, rider_on_route=on_route, rider_today_assigned=today_assigned_count, rider_completed_today=rider_completed_today, rider_earnings_today=earnings_today, delivery_history=history, rider_month_total=rider_month_total, pending_payouts=pending_payouts, rider_total_deliveries=rider_total_deliveries, rider_info=rider_info, addresses=addresses, total_deliveries=rider_total_deliveries, success_rate=success_rate, avg_rating=avg_rating, avg_earning_per_delivery=avg_earning_per_delivery)

@app.route("/update_user_status", methods=["POST"])
def update_user_status():
    if session.get("role") != "admin":
        flash("Access denied.", "error")
        return redirect(url_for("loginreg"))

    user_id = request.form.get("user_id")
    action = request.form.get("action")

    target_section = "#users"

    if action == "approve":
        new_status = "approved"
    elif action == "archive":
        new_status = "archived"
    elif action == "restore":
        new_status = "approved"
    else:
        flash("Invalid action.", "error")
        return redirect(url_for("admin"))

    try:
        # Get user info for email
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT id, fullname, email, role FROM users WHERE id=%s", (user_id,))
        user = cursor.fetchone()
        cursor.close()
        
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("admin") + "#users")

        role_key_map = {
            "customer": "customers",
            "seller": "sellers",
            "rider": "riders",
        }
        role_key = role_key_map.get(user.get("role"), "customers")
        if action == "approve":
            target_section = f"#users-{role_key}-approved"
        elif action == "archive":
            target_section = "#archived-users"
        else:
            target_section = f"#users-{role_key}-approved"
        
        # Update user status in database
        cursor = db.cursor()
        if new_status == "archived":
            cursor.execute(
                "UPDATE users SET status=%s, archived_at=NOW() WHERE id=%s",
                (new_status, user_id)
            )
        else:
            # If restoring or approving, clear archived_at
            cursor.execute(
                "UPDATE users SET status=%s, archived_at=NULL WHERE id=%s",
                (new_status, user_id)
            )
        db.commit()
        cursor.close()
        
        # Send appropriate email based on action
        if action == "approve" and user.get('email'):
            # Send approval email when user is approved
            send_approval_email(
                email=user.get('email'),
                fullname=user.get('fullname'),
                role=user.get('role'),
                status='approved'
            )
        elif action == "archive" and user.get('email'):
            # Send suspension email when user is archived/suspended
            # User account suspended for 30 days before automatic deletion
            send_suspension_email(
                email=user.get('email'),
                fullname=user.get('fullname'),
                role=user.get('role')
            )
        elif action == "restore" and user.get('email'):
            # Send restore email when user is restored
            send_restore_email(
                email=user.get('email'),
                fullname=user.get('fullname'),
                role=user.get('role')
            )
        
        flash(f"User status updated to {new_status}.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error updating user status: {e}", "error")

    # ✅ Redirect back to the correct section (keeps user in context)
    return redirect(url_for("admin") + target_section)

@app.route("/reject_user", methods=["POST"])
def reject_user():
    """Reject a pending user and send rejection email."""
    if session.get("role") != "admin":
        flash("Access denied.", "error")
        return redirect(url_for("loginreg"))

    user_id = request.form.get("user_id")
    
    try:
        # Get user info for email
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT id, fullname, email, role FROM users WHERE id=%s", (user_id,))
        user = cursor.fetchone()
        cursor.close()
        
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("admin") + "#users")
        
        # Archive the rejected user
        cursor = db.cursor()
        cursor.execute(
            "UPDATE users SET status=%s, archived_at=NOW() WHERE id=%s",
            ("archived", user_id)
        )
        db.commit()
        cursor.close()
        
        # Send rejection email
        if user.get('email'):
            send_approval_email(
                email=user.get('email'),
                fullname=user.get('fullname'),
                role=user.get('role'),
                status='rejected'
            )
        
        flash(f"User {user.get('fullname')} has been rejected and notified.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error rejecting user: {e}", "error")

    return redirect(url_for("admin") + "#archived-users")

@app.route("/save_address", methods=["POST"])
def save_address():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    region_code = request.form.get("region")
    province_code = request.form.get("province")
    city_code = request.form.get("city")
    barangay_code = request.form.get("barangay")

    region_name = request.form.get("region_name")
    province_name = request.form.get("province_name")
    city_name = request.form.get("city_name")
    barangay_name = request.form.get("barangay_name")
    street = request.form.get("street")
    address_id = request.form.get("address_id")

    cursor = db.cursor()
    try:
        if address_id:  # Update existing
            cursor.execute("""
                UPDATE addresses
                SET region_code=%s, province_code=%s, city_code=%s, barangay_code=%s,
                    region_name=%s, province_name=%s, city_name=%s, barangay_name=%s, street=%s
                WHERE id=%s AND user_id=%s
            """, (region_code, province_code, city_code, barangay_code,
                  region_name, province_name, city_name, barangay_name, street, address_id, user_id))
            flash("Address updated successfully!", "success")
        else:  # Insert new
            cursor.execute("""
                INSERT INTO addresses (user_id, region_code, region_name, province_code, province_name,
                                       city_code, city_name, barangay_code, barangay_name, street, is_default)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, region_code, region_name, province_code, province_name,
                  city_code, city_name, barangay_code, barangay_name, street, 0))
            flash("Address added successfully!", "success")

        db.commit()
    except Exception as e:
        db.rollback()
        flash(f"Error saving address: {e}", "error")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'message': 'Address saved successfully'})
    return redirect(url_for("settings", section="address"))


@app.route("/order/rider_accept", methods=["POST"])
def rider_accept_order():
    # Assign current rider to order but keep order.status as 'ready_for_pickup'
    if session.get("role") != "rider":
        return jsonify({"success": False, "error": "Access denied."}), 403

    user_id = session.get("user_id")
    order_id = request.form.get("order_id")
    if not user_id or not order_id:
        return jsonify({"success": False, "error": "Missing rider or order."}), 400

    cursor = db.cursor(dictionary=True)
    try:
        # Ensure order not already assigned
        execute_safe(cursor, "SELECT * FROM order_riders WHERE order_id = %s", (order_id,))
        if cursor.fetchone():
            return jsonify({"success": False, "error": "Order already assigned."}), 400

        execute_safe(cursor, "INSERT INTO order_riders (order_id, rider_id, status) VALUES (%s, %s, %s)", (order_id, user_id, 'assigned'))
        db.commit()
        # If the order is a return request (refund_requested_at set), mark the return_requests row
        try:
            rcur = db.cursor()
            execute_safe(rcur, "UPDATE return_requests SET pickup_rider_id=%s, assigned_at=NOW(), status=%s WHERE order_id=%s AND status IN ('pickup_requested','requested')", (user_id, 'assigned', order_id))
            db.commit()
            rcur.close()
        except Exception:
            try:
                rcur.close()
            except Exception:
                pass
        return jsonify({"success": True})
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cursor.close()

@app.route("/seller/settings", methods=["GET", "POST"])
def seller_settings():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
    user_info = cursor.fetchone()

    # Handle form submission (profile only)
    if request.method == "POST":
        fullname = request.form.get("fullname")
        profile_picture = request.files.get("profile_pic") or request.files.get("profile_picture")
        picture_path = user_info["profile_pic"] if user_info and user_info["profile_pic"] else None

        if has_uploaded_file(profile_picture):
            if allowed_file(profile_picture.filename):
                picture_path = save_file(profile_picture, "uploads/profile_pics")
            else:
                flash("File type not allowed! Only png, jpg, jpeg, gif.", "error")
                return redirect(url_for("seller_settings"))

        cursor.execute(
            "UPDATE users SET fullname=%s, profile_pic=%s WHERE id=%s",
            (fullname, picture_path, user_id)
        )
        db.commit()
        session["fullname"] = fullname
        session["profile_pic"] = picture_path
        flash("Profile updated successfully!", "success")
        return redirect(url_for("seller_settings") + "#settings/profileTab")

    cursor.close()

    if not user_info:
        user_info = {"fullname": "", "profile_pic": None}

    return render_template("seller.html", user_info=user_info)

@app.route("/add_product", methods=["POST"])
def add_product():
    if "user_id" not in session:
        flash("Please log in first.", "error")
        return redirect(url_for("login"))

    seller_id = session["user_id"]
    cursor = db.cursor(dictionary=True)

    # Check if user exists
    cursor.execute("SELECT fullname FROM users WHERE id = %s", (seller_id,))
    user = cursor.fetchone()
    if not user:
        flash("⚠️ Please complete your profile first.", "error")
        return redirect(url_for("seller"))

    # Product info
    name = request.form.get("name")
    category = request.form.get("category")
    description = request.form.get("description")
    price = float(request.form.get("price") or 0)
    stock = int(request.form.get("stock") or 0)
    # Food-specific fields (optional)
    cuisine_type = request.form.get("cuisine_type")
    try:
        preparation_time = int(request.form.get("preparation_time")) if request.form.get("preparation_time") else None
    except ValueError:
        preparation_time = None
    try:
        servings = int(request.form.get("servings")) if request.form.get("servings") else None
    except ValueError:
        servings = None
    ingredients = request.form.get("ingredients")
    allergens = request.form.get("allergens")
    is_spicy = True if (request.form.get("is_spicy") in ("on", "true", "1")) else False
    try:
        spice_level = int(request.form.get("spice_level")) if request.form.get("spice_level") else None
    except ValueError:
        spice_level = None
    # dietary_options may be submitted as a list of checkboxes
    dietary_list = request.form.getlist("dietary_options[]") or request.form.getlist("dietary_options") or []
    dietary_options = ",".join(dietary_list) if dietary_list else request.form.get("dietary_options")
    storage_instructions = request.form.get("storage_instructions")
    reheating_instructions = request.form.get("reheating_instructions")
    is_bestseller = True if (request.form.get("is_bestseller") in ("on", "true", "1")) else False
    origin_location = request.form.get("origin_location")
    nutritional_info = request.form.get("nutritional_info")
    is_available_today = True if (request.form.get("is_available_today") in ("on", "true", "1")) else True
    expiration_date = request.form.get("expiration_date")
    # (removed base product weight handling)

    # Main product image
    image = request.files.get("image")
    uploaded_product_filename = getattr(image, "filename", None)
    image_filename = None
    if has_uploaded_file(image):
        image_filename = save_file(image, "uploads/products")

    # Insert main product with temporary stock
    cursor.execute("""
        INSERT INTO products (
            seller_id, name, category, description, price, stock, image, status,
            cuisine_type, preparation_time, servings, ingredients, allergens, is_spicy, spice_level,
            dietary_options, storage_instructions, reheating_instructions, is_bestseller, origin_location,
            nutritional_info, is_available_today, expiration_date
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        seller_id, name, category, description, price, stock, image_filename,
        cuisine_type, preparation_time, servings, ingredients, allergens, is_spicy, spice_level,
        dietary_options, storage_instructions, reheating_instructions, is_bestseller, origin_location,
        nutritional_info, is_available_today, expiration_date
    ))
    product_id = cursor.lastrowid

    total_variant_stock = 0  # sum of all variant stocks

    # Variant handling
    variant_mode = request.form.get("variant_mode")

    if variant_mode == "double":
        color_values = request.form.getlist("variant_color[]")
        size_values = request.form.getlist("variant_size[]")
        prices = request.form.getlist("variant_price[]")
        stocks = request.form.getlist("variant_stock[]")
        weights = request.form.getlist("variant_weight[]")
        images = request.files.getlist("variant_image[]")

        max_len = max(len(color_values), len(size_values), len(prices), len(stocks), len(weights), len(images))
        for i in range(max_len):
            color = color_values[i] if i < len(color_values) else None
            size = size_values[i] if i < len(size_values) else None
            price = float(prices[i]) if i < len(prices) and prices[i] else 0.0
            stock = int(stocks[i]) if i < len(stocks) and stocks[i] else 0
            total_variant_stock += stock
            variant_weight = float(weights[i]) if i < len(weights) and weights[i] else 0.0
            if variant_weight < 0:
                variant_weight = 0.0
            v_image = images[i] if i < len(images) else None
            v_image_filename = None

            if v_image and v_image.filename:
                v_image_filename = save_file(v_image, "uploads/products/variants")

            cursor.execute("""
                INSERT INTO product_variants (product_id, color, size, price, stock, image, weight_kg)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (product_id, color, size, price, stock, v_image_filename, variant_weight))

    elif variant_mode == "single":
        variant_type = request.form.get("variant_type")  # 'color' or 'size'
        names = request.form.getlist("variant_name[]")
        prices = request.form.getlist("variant_price[]")
        stocks = request.form.getlist("variant_stock[]")
        weights = request.form.getlist("variant_weight[]")
        images = request.files.getlist("variant_image[]")

        max_len = max(len(names), len(prices), len(stocks), len(weights), len(images))
        for i in range(max_len):
            color = names[i] if variant_type == "color" and i < len(names) else None
            size = names[i] if variant_type == "size" and i < len(names) else None
            price = float(prices[i]) if i < len(prices) and prices[i] else 0.0
            stock = int(stocks[i]) if i < len(stocks) and stocks[i] else 0
            total_variant_stock += stock
            variant_weight = float(weights[i]) if i < len(weights) and weights[i] else 0.0
            if variant_weight < 0:
                variant_weight = 0.0
            v_image = images[i] if i < len(images) else None
            v_image_filename = None

            if v_image and v_image.filename:
                v_image_filename = save_file(v_image, "uploads/products/variants")

            cursor.execute("""
                INSERT INTO product_variants (product_id, color, size, price, stock, image, weight_kg)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (product_id, color, size, price, stock, v_image_filename, variant_weight))

    # Keep product-level stock in sync with variants, including when all variants are 0.
    if variant_mode in ("single", "double"):
        cursor.execute("UPDATE products SET stock = %s WHERE id = %s", (total_variant_stock, product_id))

    db.commit()
    # Notify admin of new product request
    cursor2 = db.cursor()
    cursor2.execute(
        """
        INSERT INTO notifications (type, message, target_url) VALUES (%s, %s, %s)
        """,
        ("product_request", f"New product submitted for approval: {name} by seller ID {seller_id}", f"/admin/product-info/{product_id}")
    )
    db.commit()
    cursor2.close()

    cursor.close()
    flash("✅ Product with variants submitted for admin approval!", "success")
    return redirect(url_for("seller") + "#add")

@app.route("/reject_product", methods=["POST"])
def reject_product():
    """Reject a pending product and send rejection email."""
    if session.get("role") != "admin":
        flash("Access denied.", "error")
        return redirect(url_for("loginreg"))

    product_id = request.form.get("product_id")
    
    try:
        # Get product and seller info for email
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT p.id, p.name, p.seller_id, u.email, u.fullname 
            FROM products p 
            JOIN sellers s ON p.seller_id = s.user_id 
            JOIN users u ON s.user_id = u.id 
            WHERE p.id = %s
        """, (product_id,))
        product_info = cursor.fetchone()
        cursor.close()
        
        if not product_info:
            flash("Product not found.", "error")
            return redirect(url_for("admin") + "#products-pending")
        
        # Delete the rejected product
        cursor = db.cursor()
        cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
        db.commit()
        cursor.close()
        
        # Send rejection email
        seller_email = product_info.get('email')
        seller_name = product_info.get('fullname')
        product_name = product_info.get('name')
        seller_id = product_info.get('seller_id')
        
        if seller_email:
            send_product_rejection_email(seller_email, seller_name, product_name)
        notify_seller_product_status(seller_id, None, product_name, "rejected by admin")
        
        flash(f"Product '{product_name}' has been rejected and seller notified.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error rejecting product: {e}", "error")

    return redirect(url_for("admin") + "#products-pending")

@app.route("/update_product_status", methods=["POST"])
def update_product_status():
    if session.get("role") != "admin":
        flash("Unauthorized access.", "error")
        return redirect(url_for("loginreg"))

    product_id = request.form.get("product_id")
    action = request.form.get("action")
    cursor = db.cursor(dictionary=True)
    target_section = "#products"

    try:
        # Get product and seller info for email
        cursor.execute("SELECT p.id, p.name, p.seller_id, u.email, u.fullname FROM products p JOIN sellers s ON p.seller_id = s.user_id JOIN users u ON s.user_id = u.id WHERE p.id=%s", (product_id,))
        product_info = cursor.fetchone()
        
        if not product_info:
            flash("Product not found.", "error")
            cursor.close()
            return redirect(url_for("admin") + "#products")
        
        seller_email = product_info.get('email')
        seller_name = product_info.get('fullname')
        product_name = product_info.get('name')
        
        if action == "approve":
            cursor.execute("UPDATE products SET status = 'approved', archived_at = NULL WHERE id = %s", (product_id,))
            notify_seller_product_status(product_info.get('seller_id'), product_id, product_name, "approved and is now live", cursor=cursor)
            flash("✅ Product approved successfully!", "success")
            # Send approval email
            if seller_email:
                send_product_approval_email(seller_email, seller_name, product_name)

        elif action == "archive":
            cursor.execute("UPDATE products SET status = 'archived', archived_at = NOW() WHERE id = %s", (product_id,))
            notify_seller_product_status(product_info.get('seller_id'), product_id, product_name, "archived by admin", cursor=cursor)
            flash("🗃 Product archived successfully!", "info")
            # Send suspension email
            if seller_email:
                send_product_suspension_email(seller_email, seller_name, product_name)

        elif action == "restore":
            cursor.execute("UPDATE products SET status = 'approved', archived_at = NULL WHERE id = %s", (product_id,))
            notify_seller_product_status(product_info.get('seller_id'), product_id, product_name, "restored and approved", cursor=cursor)
            flash("♻️ Product restored successfully!", "success")
            # Send restore email
            if seller_email:
                send_product_restore_email(seller_email, seller_name, product_name)

        db.commit()

    except Exception as e:
        db.rollback()
        flash(f"⚠️ Error updating product: {e}", "error")
    finally:
        cursor.close()

    # ✅ Stay on the products section after action
    return redirect(url_for("admin") + "#products")

@app.route("/seller/save_address", methods=["POST"])
def seller_save_address():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    region_code = request.form.get("region")
    region_name = request.form.get("region_name")
    province_code = request.form.get("province")
    province_name = request.form.get("province_name")
    city_code = request.form.get("city")
    city_name = request.form.get("city_name")
    barangay_code = request.form.get("barangay")
    barangay_name = request.form.get("barangay_name")
    street = request.form.get("street")

    cursor = db.cursor()
    query = """
        INSERT INTO addresses (
            user_id, region_code, region_name, province_code, province_name,
            city_code, city_name, barangay_code, barangay_name, street, is_default
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    values = (
        user_id, region_code, region_name,
        province_code, province_name,
        city_code, city_name,
        barangay_code, barangay_name,
        street, 0
    )

    try:
        cursor.execute(query, values)
        db.commit()
        flash("✅ Address added successfully!", "success")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'message': 'Address saved successfully'})
    except Exception as e:
        db.rollback()
        flash(f"Error saving address: {str(e)}", "error")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()

    return redirect(url_for("seller") + "#settings/addressTab")


@app.route("/setdefault_address/<int:address_id>", methods=["POST"])
def setdefault_address(address_id):
    # compatibility alias for templates that call `setdefault_address`
    return set_default_address(address_id)

@app.route("/seller/set_default_address/<int:address_id>", methods=["POST"])
def set_default_address(address_id):
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor()

    try:
        # Remove default from all user addresses
        cursor.execute("UPDATE addresses SET is_default = 0 WHERE user_id = %s", (user_id,))
        # Set the chosen one as default
        cursor.execute("UPDATE addresses SET is_default = 1 WHERE id = %s AND user_id = %s", (address_id, user_id))
        db.commit()
        flash("✅ Default address updated!", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error setting default: {str(e)}", "error")
    finally:
        cursor.close()

    return redirect(url_for("seller") + "#settings/addressTab")

@app.route("/seller/delete_address/<int:address_id>", methods=["POST"])
def seller_delete_address(address_id):
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor()
    try:
        cursor.execute("DELETE FROM addresses WHERE id = %s AND user_id = %s", (address_id, user_id))
        db.commit()
        flash("🗑️ Address deleted successfully.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error deleting address: {str(e)}", "error")
    finally:
        cursor.close()

    return redirect(url_for("seller") + "#settings/addressTab")


@app.route("/delete_address/<int:address_id>", methods=["POST"])
def delete_address(address_id):
    """Generic delete address endpoint used by templates (`url_for('delete_address')`).
    Deletes the address only if it belongs to the current user, then redirects
    to the appropriate settings page depending on role.
    """
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor()
    try:
        cursor.execute("DELETE FROM addresses WHERE id = %s AND user_id = %s", (address_id, user_id))
        db.commit()
        flash("🗑️ Address deleted successfully.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error deleting address: {str(e)}", "error")
    finally:
        cursor.close()

    if session.get("role") == "seller":
        return redirect(url_for("seller") + "#settings/addressTab")
    return redirect(url_for("settings") + "#settings/addressTab")

@app.route('/submit_order_rating', methods=['POST'])
def submit_order_rating():
    if 'user_id' not in session:
        flash('You must be logged in to rate an order.', 'error')
        return redirect(url_for('loginreg'))

    user_id = session['user_id']
    order_id = request.form.get('order_id')
    rating = request.form.get('rating')
    comment = request.form.get('comment', '')

    if not order_id or not rating:
        flash('Rating and order ID are required.', 'error')
        return redirect(url_for('orders'))

    cursor = db.cursor(dictionary=True)
    try:
        # Get all products in the order
        cursor.execute("SELECT product_id FROM order_items WHERE order_id = %s", (order_id,))
        products = cursor.fetchall()
        rated_products = set()
        for prod in products:
            product_id = prod['product_id']
            if product_id in rated_products:
                continue
            # Check if already rated for this product/order/user
            cursor.execute("SELECT id FROM order_ratings WHERE order_id = %s AND user_id = %s AND product_id = %s", (order_id, user_id, product_id))
            if cursor.fetchone():
                continue
            cursor.execute("""
                INSERT INTO order_ratings (order_id, user_id, product_id, rating, comment, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (order_id, user_id, product_id, rating, comment))
            rated_products.add(product_id)
        db.commit()
        flash('Thank you for rating your order!', 'success')
    except Exception as e:
        db.rollback()
        flash('Error submitting rating: {}'.format(e), 'error')
    finally:
        cursor.close()
    return redirect(url_for('orders'))
def is_major_product_change(product, name, category, price, image_path):
    """
    Returns True if any major field has changed (name, category, price, image).
    """
    if name != product.get("name"):
        return True
    if category != product.get("category"):
        return True
    # Compare price as float
    try:
        old_price = float(product.get("price", 0))
        new_price = float(price)
    except Exception:
        old_price = product.get("price")
        new_price = price
    if old_price != new_price:
        return True
    if image_path != product.get("image"):
        return True
    return False

@app.route("/edit_product", methods=["POST"])
def edit_product():
    user_id = session.get("user_id")
    if not user_id:
        flash("You must be logged in.", "error")
        return redirect(url_for("loginreg"))

    product_id = request.form.get("product_id")
    name = request.form.get("name")
    category = request.form.get("category")
    description = request.form.get("description")
    price = request.form.get("price")
    stock = request.form.get("stock")
    # removed main product weight from edit flow
    image = request.files.get("image")
    # Food-specific fields
    cuisine_type = request.form.get("cuisine_type")
    try:
        preparation_time = int(request.form.get("preparation_time")) if request.form.get("preparation_time") else None
    except ValueError:
        preparation_time = None
    try:
        servings = int(request.form.get("servings")) if request.form.get("servings") else None
    except ValueError:
        servings = None
    ingredients = request.form.get("ingredients")
    allergens = request.form.get("allergens")
    is_spicy = True if (request.form.get("is_spicy") in ("on", "true", "1")) else False
    try:
        spice_level = int(request.form.get("spice_level")) if request.form.get("spice_level") else None
    except ValueError:
        spice_level = None
    dietary_list = request.form.getlist("dietary_options[]") or request.form.getlist("dietary_options") or []
    dietary_options = ",".join(dietary_list) if dietary_list else request.form.get("dietary_options")
    storage_instructions = request.form.get("storage_instructions")
    reheating_instructions = request.form.get("reheating_instructions")
    is_bestseller = True if (request.form.get("is_bestseller") in ("on", "true", "1")) else False
    origin_location = request.form.get("origin_location")
    nutritional_info = request.form.get("nutritional_info")
    is_available_today = True if (request.form.get("is_available_today") in ("on", "true", "1")) else True
    expiration_date = request.form.get("expiration_date")

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM products WHERE id=%s AND seller_id=%s", (product_id, user_id))
    product = cursor.fetchone()
    if not product:
        flash("Product not found or unauthorized.", "error")
        cursor.close()
        return redirect(url_for("seller") + "#products")

    # --- Handle main product image ---
    image_path = product["image"]
    if has_uploaded_file(image):
        if allowed_file(image.filename):
            image_path = save_file(image, "uploads/products")
        else:
            flash("Invalid image format.", "error")
            cursor.close()
            return redirect(url_for("seller") + "#products")

    # --- Determine if major change (status pending) ---
    def is_major_change(old, new):
        try:
            return float(old) != float(new)
        except:
            return old != new

    new_status = "pending" if (
        name != product["name"] or
        category != product["category"] or
        cuisine_type != (product.get("cuisine_type") or None) or
        is_major_change(product["price"], price) or
        image_path != product["image"]
    ) else product["status"]

    # --- Process variants ---
    variant_ids = request.form.getlist('variant_id[]')
    variant_names = request.form.getlist('variant_name[]')
    variant_colors = request.form.getlist('variant_color[]')
    variant_sizes = request.form.getlist('variant_size[]')
    variant_prices = request.form.getlist('variant_price[]')
    variant_stocks = request.form.getlist('variant_stock[]')
    variant_weights = request.form.getlist('variant_weight[]')
    variant_images = request.files.getlist('variant_image[]')

    cursor.execute("SELECT id FROM product_variants WHERE product_id=%s", (product_id,))
    existing_ids = set(str(v['id']) for v in cursor.fetchall())
    submitted_ids = set([v for v in variant_ids if v.strip()])

    # Delete removed variants
    for vid in existing_ids - submitted_ids:
        cursor.execute("DELETE FROM product_variants WHERE id=%s AND product_id=%s", (vid, product_id))

    total_variant_stock = 0
    max_len = max(len(variant_ids), len(variant_names), len(variant_colors), len(variant_sizes),
                  len(variant_prices), len(variant_stocks), len(variant_weights), len(variant_images))

    for i in range(max_len):
        vid = variant_ids[i] if i < len(variant_ids) else ''
        name_val = variant_names[i] if i < len(variant_names) else None
        color = variant_colors[i] if i < len(variant_colors) else None
        size = variant_sizes[i] if i < len(variant_sizes) else None
        price_val = float(variant_prices[i]) if i < len(variant_prices) and variant_prices[i] else 0
        stock_val = int(variant_stocks[i]) if i < len(variant_stocks) and variant_stocks[i] else 0
        weight_val = float(variant_weights[i]) if i < len(variant_weights) and variant_weights[i] else 0
        img_file = variant_images[i] if i < len(variant_images) else None

        if name_val and not color and not size:
            color = name_val  # single variant: store in color

        img_path = None
        if has_uploaded_file(img_file):
            img_path = save_file(img_file, "uploads/products/variants")

        if vid.strip():  # Update existing
            if img_path:
                cursor.execute("""
                    UPDATE product_variants SET color=%s, size=%s, price=%s, stock=%s, weight_kg=%s, image=%s
                    WHERE id=%s AND product_id=%s
                """, (color, size, price_val, stock_val, weight_val, img_path, vid, product_id))
            else:
                cursor.execute("""
                    UPDATE product_variants SET color=%s, size=%s, price=%s, stock=%s, weight_kg=%s
                    WHERE id=%s AND product_id=%s
                """, (color, size, price_val, stock_val, weight_val, vid, product_id))
        else:  # Insert new
            cursor.execute("""
                INSERT INTO product_variants (product_id, color, size, price, stock, weight_kg, image)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (product_id, color, size, price_val, stock_val, weight_val, img_path))

        total_variant_stock += stock_val

    final_stock = total_variant_stock if max_len > 0 else int(stock or product.get("stock", 0))

    # --- Update main product ---
    cursor.execute("""
        UPDATE products
        SET name=%s, category=%s, description=%s, price=%s, stock=%s,
            image=%s, status=%s,
            cuisine_type=%s, preparation_time=%s, servings=%s, ingredients=%s, allergens=%s,
            is_spicy=%s, spice_level=%s, dietary_options=%s, storage_instructions=%s,
            reheating_instructions=%s, is_bestseller=%s, origin_location=%s,
            nutritional_info=%s, is_available_today=%s, expiration_date=%s
        WHERE id=%s AND seller_id=%s
    """, (
        name, category, description, price, final_stock, image_path, new_status,
        cuisine_type, preparation_time, servings, ingredients, allergens,
        is_spicy, spice_level, dietary_options, storage_instructions,
        reheating_instructions, is_bestseller, origin_location,
        nutritional_info, is_available_today, expiration_date,
        product_id, user_id
    ))

    if new_status == "pending" and (product.get("status") or "").lower() != "pending":
        create_notification(
            "product_request",
            f"Product update submitted for approval: {name} by seller ID {user_id}",
            f"/admin/product-info/{product_id}",
            cursor=cursor,
            commit=False,
        )

    db.commit()
    cursor.close()
    flash("✅ Product updated successfully!", "success")
    return redirect(url_for("seller") + "#products")

@app.route("/delete_product/<int:product_id>", methods=["POST"])
def delete_product(product_id):
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    cursor = db.cursor()

    cursor.execute("""
        UPDATE products
        SET status = 'deleted', archived_at = %s
        WHERE id = %s AND seller_id = %s
    """, (datetime.now(), product_id, user_id))
    db.commit()
    cursor.close()

    flash("🗑️ Product moved to Deleted. It will be permanently removed after 30 days.", "info")
    return redirect(url_for("seller") + "#deleted")

@app.route("/restore_product/<int:product_id>", methods=["POST"])
def restore_product(product_id):
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT name, seller_id FROM products WHERE id=%s", (product_id,))
    product = cursor.fetchone() or {}

    # Restore product (you can choose 'approved' or 'pending' as restored status)
    cursor.execute("""
        UPDATE products
        SET status = 'pending', archived_at = NULL
        WHERE id = %s
    """, (product_id,))
    create_notification(
        "product_request",
        f"Product restored and resubmitted for approval: {product.get('name') or product_id} by seller ID {product.get('seller_id')}",
        f"/admin/product-info/{product_id}",
        cursor=cursor,
        commit=False,
    )

    db.commit()
    cursor.close()

    flash("✅ Product has been restored successfully!", "success")
    return redirect(url_for("seller") + "#deleted")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("homepage"))

@app.route("/adminlogout")
def adminlogout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("admin_login"))

@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    """Handle forgot password request"""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        
        if not email:
            flash("Please enter your email address.", "error")
            return render_template("forgot_password.html")
        
        # Check if user exists
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT id, fullname, email FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        cursor.close()
        
        if not user:
            # Don't reveal if email exists for security reasons
            flash("If an account exists with that email, you will receive a password reset link.", "info")
            return render_template("forgot_password.html")
        
        # Generate reset token
        reset_token = secrets.token_urlsafe(32)
        token_expiry = datetime.now() + timedelta(hours=24)
        
        # Store reset token in database
        cursor = db.cursor()
        cursor.execute(
            "UPDATE users SET reset_token = %s, reset_token_expiry = %s WHERE id = %s",
            (reset_token, token_expiry, user['id'])
        )
        db.commit()
        cursor.close()
        
        # Create reset link
        reset_link = url_for('reset_password', token=reset_token, _external=True)
        
        # Send email
        send_forgot_password_email(user['email'], user['fullname'], reset_link)
        
        flash("If an account exists with that email, you will receive a password reset link.", "info")
        return render_template("forgot_password.html")
    
    return render_template("forgot_password.html")

@app.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Handle password reset"""
    # Verify token
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, fullname, email FROM users WHERE reset_token = %s AND reset_token_expiry > NOW()",
        (token,)
    )
    user = cursor.fetchone()
    cursor.close()
    
    if not user:
        flash("This password reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))
    
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        
        if not password or not confirm_password:
            flash("Please fill in all fields.", "error")
            return render_template("reset_password.html", token=token)
        
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token=token)
        
        password_error = validate_strong_password(password)
        if password_error:
            flash(password_error, "error")
            return render_template("reset_password.html", token=token)
        
        # Update password and clear reset token
        cursor = db.cursor()
        cursor.execute(
            "UPDATE users SET password = %s, reset_token = NULL, reset_token_expiry = NULL WHERE id = %s",
            (hash_password(password), user['id'])
        )
        db.commit()
        cursor.close()
        
        flash("Your password has been reset successfully. Please log in with your new password.", "success")
        return redirect(url_for("loginreg"))
    
    return render_template("reset_password.html", token=token)


@app.route("/settings")
def settings():
    # Provide a common settings endpoint used by templates. Sellers are redirected
    # to their seller-specific settings, other logged-in users see the customer settings.
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    # If seller, send to seller-specific settings
    if session.get("role") == "seller":
        return redirect(url_for("seller_settings"))

    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT fullname, profile_pic, email FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    cursor.execute("SELECT * FROM addresses WHERE user_id=%s ORDER BY id DESC", (user_id,))
    addresses = cursor.fetchall()
    cursor.close()

    # Fetch customer-targeted notifications (tagged with [customer:<id>])
    customer_notifications = []
    try:
        uid = user_id
        notif_cursor = db.cursor(dictionary=True)
        tag = f"[customer:{uid}]"
        notif_cursor.execute("SELECT * FROM notifications WHERE message LIKE %s ORDER BY created_at DESC LIMIT 50", (f"%{tag}%",))
        customer_notifications = notif_cursor.fetchall() or []
        notif_cursor.close()
    except Exception:
        customer_notifications = []

    return render_template("settings.html", user=user, addresses=addresses, notifications=customer_notifications)


@app.route("/update_profile", methods=["POST"])
def update_profile():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in first.", "error")
        return redirect(url_for("loginreg"))

    current_user_cursor = db.cursor(dictionary=True)
    current_user_cursor.execute("SELECT fullname, phone, profile_pic FROM users WHERE id = %s", (user_id,))
    current_user = current_user_cursor.fetchone() or {}
    current_user_cursor.close()

    fullname = request.form.get("fullname")
    phone = request.form.get("phone")
    profile_pic = request.files.get("profile_pic") or request.files.get("profile_picture")
    # Rider-specific fields (if any)
    vehicle_type = request.form.get("vehicle_type")
    plate_number = request.form.get("plate_number")
    drivers_license_file = request.files.get("drivers_license")

    pic_path = None
    uploaded_profile_filename = getattr(profile_pic, "filename", None)
    if has_uploaded_file(profile_pic):
        pic_path = save_file(profile_pic, os.path.join(app.config.get("UPLOAD_FOLDER", "static/uploads"), "profile_pics"))

    cursor = db.cursor()
    try:
        password_updated = False
        
        # Handle password change if requested
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_new_password')
        current_password = request.form.get('current_password')
        if new_password:
            password_error = validate_strong_password(new_password)
            if password_error:
                flash(password_error, 'error')
            elif new_password != confirm_password:
                flash('New passwords do not match', 'error')
            elif not current_password:
                flash('Current password is required', 'error')
            else:
                # Verify current password
                curpwd_cur = db.cursor()
                curpwd_cur.execute('SELECT password FROM users WHERE id=%s', (user_id,))
                row = curpwd_cur.fetchone()
                curpwd_cur.close()
                stored_password = row[0] if row else None
                if not row or not verify_password(stored_password, current_password):
                    flash('Current password is incorrect', 'error')
                else:
                    cursor.execute('UPDATE users SET password=%s WHERE id=%s', (hash_password(new_password), user_id))
                    password_updated = True
                    flash('Password updated successfully!', 'success')
        
        # Update profile information if provided
        profile_updated = False
        next_fullname = fullname if fullname is not None else current_user.get("fullname")
        next_phone = phone if phone is not None else current_user.get("phone")
        next_pic_path = pic_path if pic_path else current_user.get("profile_pic")

        if fullname is not None or phone is not None or pic_path:
            profile_updated = True
            cursor.execute(
                "UPDATE users SET fullname=%s, phone=%s, profile_pic=%s WHERE id=%s",
                (next_fullname, next_phone, next_pic_path, user_id)
            )
        
        # Only commit if something was actually updated
        if password_updated or profile_updated:
            db.commit()
        
        # Refresh session fields for immediate reflect in UI - only if values are not None/empty
        if next_fullname is not None and str(next_fullname).strip():
            session['fullname'] = next_fullname
        if next_phone is not None and str(next_phone).strip():
            session['phone'] = next_phone
        elif phone is not None:
            session['phone'] = next_phone
        if next_pic_path:
            session['profile_pic'] = next_pic_path

        if pic_path:
            persisted_pic = fetch_scalar("SELECT profile_pic FROM users WHERE id=%s", (user_id,))
            if persisted_pic != next_pic_path:
                raise RuntimeError(
                    f"Profile image update did not persist for user {user_id}. "
                    f"Uploaded file: {uploaded_profile_filename or 'unknown'}"
                )
        
        # Show appropriate message based on what was updated
        if profile_updated and not password_updated:
            flash("Profile updated successfully!", "success")
        elif password_updated and not profile_updated:
            pass  # Password flash message already shown above
        elif password_updated and profile_updated:
            flash("Profile and password updated successfully!", "success")
        # Return JSON for AJAX requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'message': 'Profile updated successfully'})
        # If rider, update their rider profile (vehicle, plate number, license)
        if session.get('role') == 'rider':
            try:
                license_path = None
                if has_uploaded_file(drivers_license_file):
                    license_path = save_file(drivers_license_file, os.path.join(app.config.get("UPLOAD_FOLDER", "static/uploads"), "licenses"))
                # Upsert riders row
                rcur = db.cursor()
                execute_safe(rcur, "SELECT user_id FROM riders WHERE user_id=%s", (user_id,))
                exists = rcur.fetchone() is not None
                rcur.close()
                rcur = db.cursor()
                if exists:
                    if license_path:
                        execute_safe(rcur, "UPDATE riders SET vehicle_type=%s, plate_number=%s, drivers_license=%s WHERE user_id=%s", (vehicle_type, plate_number, license_path, user_id))
                    else:
                        execute_safe(rcur, "UPDATE riders SET vehicle_type=%s, plate_number=%s WHERE user_id=%s", (vehicle_type, plate_number, user_id))
                else:
                    execute_safe(rcur, "INSERT INTO riders (user_id, vehicle_type, plate_number, drivers_license) VALUES (%s, %s, %s, %s)", (user_id, vehicle_type, plate_number, license_path))
                db.commit()
                rcur.close()
                # Update session values for rider - only if values are not None/empty
                if vehicle_type and vehicle_type.strip():
                    session['vehicle_type'] = vehicle_type
                if plate_number and plate_number.strip():
                    session['plate_number'] = plate_number
            except Exception:
                db.rollback()
                try:
                    rcur.close()
                except Exception:
                    pass
    except Exception as e:
        db.rollback()
        flash(f"Failed to update profile: {e}", "error")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()

    # Redirect back to appropriate settings page per role
    role = session.get("role")
    if role == "seller":
        return redirect(url_for("seller_settings") + "#settings/profileTab")
    elif role == "rider":
        return redirect(url_for("rider") + "#settings/profileTab")
    elif role == "admin":
        # Admin no longer has a 'settings' section; redirect to admin dashboard
        return redirect(url_for("admin") + "#dashboard")
    return redirect(url_for("settings") + "#settings/profileTab")

@app.route('/rate_order/<int:order_id>', methods=['GET', 'POST'])
def rate_order(order_id):
    user = None
    user_id = session.get('user_id')
    if user_id:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT fullname, profile_pic FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        # Get all items for this order
        cursor.execute("""
            SELECT oi.product_id, p.name AS product_name
            FROM order_items oi
            LEFT JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = %s
        """, (order_id,))
        items = cursor.fetchall()
        # Get unique products (ignore variants)
        unique_products = {}
        for item in items:
            pid = item['product_id']
            if pid not in unique_products:
                unique_products[pid] = item['product_name']
        cursor.close()
    else:
        unique_products = {}
    if request.method == 'POST':
        if not user_id:
            flash('You must be logged in to rate an order.', 'error')
            return redirect(url_for('loginreg'))
        cursor = db.cursor(dictionary=True)
        
        # Get rider_id for this order
        cursor.execute("SELECT orr.rider_id FROM order_riders orr WHERE orr.order_id = %s LIMIT 1", (order_id,))
        rider_row = cursor.fetchone()
        rider_id = rider_row['rider_id'] if rider_row else None
        
        # Store rider performance rating
        rider_rating = request.form.get('rider_rating')
        if rider_rating and rider_id:
            cursor.execute("INSERT INTO ratings (order_id, rated_user_id, rating_user_id, rating, role, created_at) VALUES (%s, %s, %s, %s, 'rider', NOW())", 
                          (order_id, rider_id, user_id, rider_rating))
        
        # For each product, get rating and comment from form
        for pid in unique_products:
            rating = request.form.get(f'rating_{pid}')
            comment = request.form.get(f'comment_{pid}')
            if rating:
                cursor.execute("INSERT INTO product_ratings (order_id, user_id, product_id, rating, comment, created_at) VALUES (%s, %s, %s, %s, %s, NOW())", (order_id, user_id, pid, rating, comment))
        
        db.commit()
        cursor.close()
        flash('Thank you for rating your order!', 'success')
        return redirect(url_for('orders'))
    return render_template('rate_order.html', order_id=order_id, user=user, products=unique_products)

@app.route('/mark_notification_read', methods=['POST'])
def mark_notification_read():
    notif_id = request.form.get('notification_id')
    if notif_id:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM notifications WHERE id = %s", (notif_id,))
        notif_row = cursor.fetchone()
        if not notif_row:
            cursor.close()
            return jsonify({"success": False, "error": "Notification not found"}), 404
        # ownership/visibility check
        role = session.get('role')
        user_id = session.get('user_id')
        message = notif_row.get('message') or ''
        # Find tags like [seller:123], [customer:456], [rider:789], [rider:all]
        tags = re.findall(r"\[(seller|customer|rider|riders):(\d+|all)\]", message)
        allowed = False
        if role == 'admin':
            # Admin may only mark notifications that are not role-targeted
            if tags or notif_row.get('type') in ('order_seller', 'order_customer', 'order_rider', 'product_seller'):
                allowed = False
            else:
                allowed = True
        else:
            if not tags:
                # no tags - assume generic notification visible to all logged in
                allowed = True if user_id else False
            else:
                def tag_allowed(tag_role, tag_id):
                    if role == 'rider' and tag_role in ('rider', 'riders') and tag_id == 'all':
                        return True
                    if tag_id == 'all':
                        return False
                    if role == 'seller' and tag_role == 'seller':
                        return int(tag_id) == (user_id or 0)
                    if role in ('customer', 'user', 'buyer') and tag_role == 'customer':
                        return int(tag_id) == (user_id or 0)
                    if role == 'rider' and tag_role == 'rider':
                        return int(tag_id) == (user_id or 0)
                    return False
                # all role tags in the message must be visible to this user
                allowed = all(tag_allowed(tag_role, tag_id) for (tag_role, tag_id) in tags)
        if not allowed:
            cursor.close()
            return jsonify({"success": False, "error": "Permission denied"}), 403
        # safe to mark
        cursor.execute("UPDATE notifications SET is_read = 1 WHERE id = %s", (notif_id,))
        db.commit()
        cursor.close()
    # If this request is AJAX (X-Requested-With) return JSON to support client-side redirect
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({"success": True})
    # Otherwise continue redirect flow
    ref = request.referrer
    if ref:
        return redirect(ref)
    role = session.get('role')
    if role == 'seller':
        return redirect(url_for('seller'))
    if role == 'admin':
        return redirect(url_for('admin'))
    return redirect(url_for('homepage'))


@app.route('/mark_all_notifications_read', methods=['POST'])
def mark_all_notifications_read():
    role = session.get('role')
    cursor = db.cursor()
    try:
        if role == 'admin':
            # mark all system-wide notifications as read
            cursor.execute(
                """
                UPDATE notifications SET is_read = 1
                WHERE is_read = 0
                  AND (message IS NULL OR (
                    message NOT LIKE %s
                    AND message NOT LIKE %s
                    AND message NOT LIKE %s
                    AND message NOT LIKE %s
                  ))
                """,
                ("%[seller:%", "%[customer:%", "%[rider:%", "%[riders:%"),
            )
            db.commit()
            cursor.close()
            # If AJAX request, return JSON
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True})
            ref = request.referrer
            if ref:
                return redirect(ref)
            return redirect(url_for('admin'))

        if role == 'seller':
            user_id = session.get('user_id')
            if not user_id:
                cursor.close()
                flash('Please log in.', 'error')
                return redirect(url_for('loginreg'))
            tag = f"[seller:{user_id}]"
            cursor.execute("UPDATE notifications SET is_read = 1 WHERE message LIKE %s AND is_read = 0", (f"%{tag}%",))
            db.commit()
            cursor.close()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True})
            ref = request.referrer
            if ref:
                return redirect(ref)
            return redirect(url_for('seller'))

        if role == 'customer' or role == 'user' or role == 'buyer':
            # support possible role naming variants
            user_id = session.get('user_id')
            if not user_id:
                cursor.close()
                flash('Please log in.', 'error')
                return redirect(url_for('loginreg'))
            tag = f"[customer:{user_id}]"
            cursor.execute("UPDATE notifications SET is_read = 1 WHERE message LIKE %s AND is_read = 0", (f"%{tag}%",))
            db.commit()
            cursor.close()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True})
            ref = request.referrer
            if ref:
                return redirect(ref)
            return redirect(url_for('orders'))

        if role == 'rider':
            # rider-specific notifications
            user_id = session.get('user_id')
            if not user_id:
                cursor.close()
                flash('Please log in.', 'error')
                return redirect(url_for('loginreg'))
            tag = f"[rider:{user_id}]"
            cursor.execute(
                "UPDATE notifications SET is_read = 1 WHERE (message LIKE %s OR message LIKE %s OR message LIKE %s) AND is_read = 0",
                (f"%{tag}%", "%[rider:all]%", "%[riders:all]%"),
            )
            db.commit()
            cursor.close()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True})
            ref = request.referrer
            if ref:
                return redirect(ref)
            return redirect(url_for('rider'))

        # default
        cursor.close()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": True})
        ref = request.referrer
        if ref:
            return redirect(ref)
        return redirect(url_for('homepage'))
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        # no-op: don't log here. Action logs are created by specific endpoints during operations.
        try:
            cursor.close()
        except Exception:
            pass
        flash(f'Failed to mark notifications: {e}', 'error')
        return redirect(request.referrer or url_for('homepage'))

# ==================== Return Request Endpoints ====================

@app.route('/order/get_return_pickup_details', methods=['POST'])
def get_return_pickup_details():
    """Get customer and seller address for return pickup"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.get_json() or request.form
    order_id = data.get('order_id')
    return_id = data.get('return_id')
    
    if not order_id or not return_id:
        return jsonify({"success": False, "error": "Missing order_id or return_id"}), 400
    
    cursor = db.cursor(dictionary=True)
    
    # Get order and verify it belongs to the user
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    
    if not order or order.get('user_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Order not found or permission denied"}), 403
    
    # Get customer address
    customer_id = order.get('user_id')
    cursor.execute("SELECT address FROM users WHERE id=%s", (customer_id,))
    customer = cursor.fetchone()
    customer_address = customer.get('address') if customer else "Address not available"
    
    # Get seller address
    seller_id = order.get('seller_id')
    cursor.execute("SELECT address FROM sellers WHERE id=%s", (seller_id,))
    seller = cursor.fetchone()
    seller_address = seller.get('address') if seller else "Address not available"
    
    cursor.close()
    
    return jsonify({
        "success": True,
        "customer_address": customer_address,
        "seller_address": seller_address
    })

@app.route('/order/ready_for_pickup', methods=['POST'])
def ready_for_pickup():
    """Mark return request as ready for pickup"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.get_json() or request.form
    return_request_id = data.get('return_request_id')
    
    if not return_request_id:
        return jsonify({"success": False, "error": "Missing return_request_id"}), 400
    
    cursor = db.cursor(dictionary=True)
    
    # Get return request and verify it belongs to the user
    cursor.execute("SELECT * FROM return_requests WHERE id=%s", (return_request_id,))
    return_req = cursor.fetchone()
    
    if not return_req:
        cursor.close()
        return jsonify({"success": False, "error": "Return request not found"}), 404
    
    # Get order to verify user
    order_id = return_req.get('order_id')
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    
    if not order or order.get('user_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403
    
    # Check current status
    current_status = (return_req.get('status') or '').lower()
    if current_status not in ('requested', 'pending', 'approved'):
        cursor.close()
        return jsonify({"success": False, "error": f"Cannot mark as ready: current status is {current_status}"}), 400
    
    try:
        # Update return request status to 'ready_for_pickup'
        cursor.execute(
            "UPDATE return_requests SET status='ready_for_pickup', pickup_requested_at=NOW() WHERE id=%s",
            (return_request_id,)
        )
        db.commit()
        cursor.close()
        
        # Create notification for riders (non-blocking, don't crash if it fails)
        try:
            notif_cur = db.cursor()
            seller_id = order.get('seller_id')
            customer_id = order.get('user_id')
            
            # Notify all riders about this pickup opportunity
            rider_msg = f"[rider:all] New return pickup available! Order #{order_id}. Pickup from customer (Address available). Delivery to seller. Click to accept."
            notif_cur.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("rider_return_pickup", rider_msg, f"/rider?return_id={return_request_id}")
            )
            
            # Notify seller
            seller_msg = f"[seller:{seller_id}] Return request #{return_request_id} for order #{order_id} is ready for pickup by rider."
            notif_cur.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("seller_return", seller_msg, f"/seller?return_id={return_request_id}")
            )
            
            db.commit()
            notif_cur.close()
        except Exception as e:
            app.logger.warning(f"Failed to create notifications for ready_for_pickup: {e}")
            try:
                db.rollback()
            except Exception:
                pass
        
        return jsonify({
            "success": True,
            "message": "Return request marked as ready for pickup."
        })
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        cursor.close()
        app.logger.error(f"Error in ready_for_pickup: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/order/cancel_return_request', methods=['POST'])
def cancel_return_request():
    """Cancel a return request"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.get_json() or request.form
    return_request_id = data.get('return_request_id')
    
    if not return_request_id:
        return jsonify({"success": False, "error": "Missing return_request_id"}), 400
    
    cursor = db.cursor(dictionary=True)
    
    # Get return request
    cursor.execute("SELECT * FROM return_requests WHERE id=%s", (return_request_id,))
    return_req = cursor.fetchone()
    
    if not return_req:
        cursor.close()
        return jsonify({"success": False, "error": "Return request not found"}), 404
    
    # Get order to verify user
    order_id = return_req.get('order_id')
    cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cursor.fetchone()
    
    if not order or order.get('user_id') != user_id:
        cursor.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403
    
    # Only allow cancelling pending requests
    current_status = (return_req.get('status') or '').lower()
    if current_status != 'pending':
        cursor.close()
        return jsonify({"success": False, "error": f"Cannot cancel return request with status: {current_status}"}), 400
    
    try:
        # Delete or mark as cancelled
        cursor.execute("DELETE FROM return_requests WHERE id=%s", (return_request_id,))
        db.commit()
        
        # Notify seller about cancellation
        try:
            notif_cur = db.cursor()
            seller_id = order.get('seller_id')
            seller_msg = f"[seller:{seller_id}] Customer cancelled return request #{return_request_id} for order #{order_id}."
            notif_cur.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("seller_return", seller_msg, f"/seller")
            )
            db.commit()
            notif_cur.close()
        except Exception as e:
            app.logger.warning(f"Failed to notify seller of cancelled return: {e}")
            pass
        
        cursor.close()
        return jsonify({"success": True, "message": "Return request cancelled successfully."})
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        cursor.close()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/order/request_return', methods=['POST'])
def request_return():
    """Customer requests return for a completed order"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.get_json() or request.form
    order_id = data.get('order_id')
    reason = data.get('reason')
    notes = data.get('notes', '')
    
    if not order_id or not reason:
        return jsonify({"success": False, "error": "Missing order_id or reason"}), 400
    
    cursor = db.cursor(dictionary=True)
    
    try:
        # Get order and verify it belongs to the user
        cursor.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
        order = cursor.fetchone()
        
        if not order or order.get('user_id') != user_id:
            cursor.close()
            return jsonify({"success": False, "error": "Order not found or permission denied"}), 403
        
        # Check if order is in a valid status for return (completed)
        current_status = (order.get('status') or '').lower()
        if current_status != 'completed':
            cursor.close()
            return jsonify({"success": False, "error": f"Can only return completed orders. Current status: {current_status}"}), 400
        
        # Check if already has a return request
        cursor.execute("SELECT * FROM return_requests WHERE order_id=%s", (order_id,))
        if cursor.fetchone():
            cursor.close()
            return jsonify({"success": False, "error": "This order already has a return request"}), 400
        
        # Create return request
        cursor.execute("""
            INSERT INTO return_requests (order_id, requested_by, requested_by_role, reason, notes, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (order_id, user_id, 'customer', reason, notes, 'pending'))
        
        db.commit()
        return_request_id = cursor.lastrowid
        
        # Notify seller about return request
        try:
            notif_cur = db.cursor()
            seller_id = order.get('seller_id')
            seller_msg = f"[seller:{seller_id}] Customer requested return for order #{order_id}. Reason: {reason}"
            notif_cur.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("seller_return_request", seller_msg, f"/seller?return_id={return_request_id}")
            )
            
            # Also notify admin
            admin_msg = f"[admin:system] Customer #{user_id} requested return for order #{order_id}. Return ID: {return_request_id}"
            notif_cur.execute(
                "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                ("return_request", admin_msg, f"/admin?return_id={return_request_id}")
            )
            
            db.commit()
            notif_cur.close()
        except Exception as e:
            app.logger.warning(f"Failed to notify seller/admin of return request: {e}")
            pass
        
        cursor.close()
        return jsonify({
            "success": True,
            "message": "Return request submitted successfully! The seller has been notified and will review your request.",
            "return_id": return_request_id
        })
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        cursor.close()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/rider/available_return_pickups', methods=['GET'])
def rider_available_return_pickups():
    """Get available return pickups for the rider, filtered by vehicle type"""
    rider_id = session.get('user_id')
    if not rider_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Get rider's vehicle type from riders table
        cursor.execute("SELECT vehicle_type FROM riders WHERE user_id = %s", (rider_id,))
        rider_result = cursor.fetchone()
        rider_vehicle_type = rider_result['vehicle_type'].lower() if rider_result and rider_result['vehicle_type'] else None
        
        if not rider_vehicle_type:
            cursor.close()
            return jsonify({"success": True, "returns": []})
        
        # Get all return_requests with status 'ready_for_pickup' that haven't been assigned yet
        query = """
            SELECT rr.id, rr.order_id, rr.customer_id, o.seller_id, 
                   c.fullname as customer_name, s.fullname as seller_name, o.vehicle_assign_type
            FROM return_requests rr
            JOIN orders o ON rr.order_id = o.id
            JOIN users c ON o.user_id = c.id
            JOIN users s ON o.seller_id = s.id
            WHERE rr.status = 'ready_for_pickup' AND rr.pickup_rider_id IS NULL
            ORDER BY rr.pickup_requested_at ASC
        """
        cursor.execute(query)
        all_returns = cursor.fetchall() or []
        
        result = []
        for rr in all_returns:
            order_vehicle_type = (rr.get('vehicle_assign_type') or '').lower()
            
            if not order_vehicle_type or rider_vehicle_type == order_vehicle_type or order_vehicle_type == 'any':
                addr_query = """
                    SELECT CONCAT(street, ', ', barangay_name, ', ', city_name, ', ', province_name, ', ', region_name) as address
                    FROM addresses
                    WHERE user_id = %s AND is_default = 1
                    LIMIT 1
                """
                cursor.execute(addr_query, (rr['customer_id'],))
                customer_addr = cursor.fetchone()
                customer_address = customer_addr['address'] if customer_addr else 'Address not available'
                
                cursor.execute(addr_query, (rr['seller_id'],))
                seller_addr = cursor.fetchone()
                seller_address = seller_addr['address'] if seller_addr else 'Address not available'
                
                result.append({
                    'id': rr['id'],
                    'order_id': rr['order_id'],
                    'customer_name': rr['customer_name'],
                    'seller_name': rr['seller_name'],
                    'customer_address': customer_address,
                    'seller_address': seller_address,
                    'vehicle_type': order_vehicle_type
                })
        
        cursor.close()
        return jsonify({"success": True, "returns": result})
    except Exception as e:
        app.logger.error(f"Error fetching available return pickups: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/rider/accept_return_pickup', methods=['POST'])
def rider_accept_return_pickup():
    """Rider accepts a return pickup assignment"""
    rider_id = session.get('user_id')
    if not rider_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.get_json()
    return_id = data.get('return_request_id')
    
    if not return_id:
        return jsonify({"success": False, "error": "Missing return_request_id"}), 400
    
    try:
        cursor = db.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM return_requests WHERE id=%s", (return_id,))
        return_req = cursor.fetchone()
        
        if not return_req:
            cursor.close()
            return jsonify({"success": False, "error": "Return request not found"}), 404
        
        if return_req.get('status') != 'ready_for_pickup':
            cursor.close()
            return jsonify({"success": False, "error": "This return is no longer available"}), 400
        
        if return_req.get('pickup_rider_id') is not None:
            cursor.close()
            return jsonify({"success": False, "error": "This pickup has already been assigned"}), 400
        
        cursor.execute(
            "UPDATE return_requests SET pickup_rider_id = %s, status = 'assigned' WHERE id = %s",
            (rider_id, return_id)
        )
        db.commit()
        
        cursor.execute("SELECT customer_id FROM return_requests WHERE id = %s", (return_id,))
        rr = cursor.fetchone()
        customer_id = rr['customer_id'] if rr else None
        
        if customer_id:
            try:
                notif_cur = db.cursor()
                notif_cur.execute(
                    "INSERT INTO notifications (`type`, `message`, `target_url`) VALUES (%s, %s, %s)",
                    ("rider_assigned", f"A rider has been assigned to your return pickup (Return #{return_id})", f"/order?return_id={return_id}")
                )
                db.commit()
                notif_cur.close()
            except Exception as ne:
                app.logger.warning(f"Failed to create notification: {ne}")
        
        cursor.close()
        return jsonify({"success": True, "message": "Return pickup accepted successfully!"})
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        app.logger.error(f"Error accepting return pickup: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================
# SALES REPORT API ENDPOINTS
# ============================================

@app.route("/api/seller/sales-report", methods=["GET"])
def api_seller_sales_report():
    """Fetch product sales data for seller reports with filtering"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Count sales only after the customer confirms the order as received.
        # Cancelled, refund-requested, and refunded orders are excluded because
        # they never reach the completed status used by the received flow.
        query = '''
            SELECT 
                p.id,
                p.name,
                p.category,
                COALESCE(SUM(oi.quantity), 0) AS units_sold,
                COUNT(DISTINCT oi.order_id) AS total_sales,
                COALESCE(SUM(oi.price * oi.quantity), 0) AS revenue,
                MAX(o.created_at) AS last_sale_date,
                MIN(o.created_at) AS first_sale_date
            FROM products p
            JOIN order_items oi
                ON p.id = oi.product_id
                AND COALESCE(oi.seller_id, p.seller_id) = %s
            JOIN orders o
                ON oi.order_id = o.id
                AND o.status = 'completed'
            WHERE p.seller_id = %s
            GROUP BY p.id, p.name, p.category
            ORDER BY revenue DESC
        '''
        
        execute_safe(cursor, query, (user_id, user_id))
        sales_data = cursor.fetchall() or []
        cursor.close()
        
        # Convert datetime objects to ISO format strings for JSON
        for item in sales_data:
            if item.get('last_sale_date'):
                item['last_sale_date'] = item['last_sale_date'].isoformat()
            if item.get('first_sale_date'):
                item['first_sale_date'] = item['first_sale_date'].isoformat()
        
        app.logger.info(f"Seller {user_id} sales data returned: {len(sales_data)} items")
        if sales_data:
            app.logger.info(f"First item sample: {sales_data[0]}")
        
        return jsonify({
            "success": True,
            "data": sales_data
        })
    except Exception as e:
        app.logger.error(f"Error fetching seller sales report: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/sales-report", methods=["GET"])
def api_admin_sales_report():
    """Fetch sales and income data from all sellers for admin reports"""
    if session.get("role") != "admin":
        return jsonify({"error": "Access denied"}), 403
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Fetch sales data from all sellers with date information
        query = '''
            SELECT 
                u.id AS seller_id,
                u.fullname AS seller_name,
                COUNT(DISTINCT o.id) AS total_sales,
                COUNT(DISTINCT CASE WHEN o.status = 'completed' THEN o.id END) AS completed_orders,
                COALESCE(SUM(CASE WHEN o.status = 'completed' THEN COALESCE(
                    (SELECT SUM(amount) FROM earnings e WHERE e.order_id = o.id AND e.user_id = u.id AND e.role = 'seller'),
                    (SELECT SUM(amount) FROM income i WHERE i.order_id = o.id AND i.user_id = u.id AND i.role = 'seller'),
                    0
                ) ELSE 0 END), 0) AS seller_income,
                COALESCE(SUM(CASE WHEN o.status = 'completed' THEN COALESCE(
                    (SELECT SUM(amount) FROM earnings e WHERE e.order_id = o.id AND e.role = 'admin'),
                    (SELECT SUM(amount) FROM income i WHERE i.order_id = o.id AND i.role = 'admin'),
                    0
                ) ELSE 0 END), 0) AS admin_commission,
                COALESCE(SUM(CASE WHEN o.status = 'completed' THEN o.total ELSE 0 END), 0) AS total_revenue,
                MAX(o.created_at) AS last_sale_date,
                MIN(o.created_at) AS first_sale_date
            FROM users u
            LEFT JOIN orders o ON u.id = o.seller_id
            WHERE u.role = 'seller'
            GROUP BY u.id, u.fullname
            ORDER BY total_sales DESC
        '''
        
        execute_safe(cursor, query)
        sales_data = cursor.fetchall() or []
        
        # Convert datetime objects to ISO format strings for JSON
        for item in sales_data:
            if item.get('last_sale_date'):
                item['last_sale_date'] = item['last_sale_date'].isoformat()
            if item.get('first_sale_date'):
                item['first_sale_date'] = item['first_sale_date'].isoformat()
        
        # Calculate totals
        totals = {
            "total_sales": sum(s.get("total_sales", 0) for s in sales_data),
            "total_sellers": len(sales_data),
            "total_income": sum(float(s.get("seller_income", 0) or 0) for s in sales_data),
            "total_admin_commission": sum(float(s.get("admin_commission", 0) or 0) for s in sales_data)
        }
        
        cursor.close()
        
        return jsonify({
            "success": True,
            "data": sales_data,
            "totals": totals
        })
    except Exception as e:
        app.logger.error(f"Error fetching admin sales report: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/seller/categories", methods=["GET"])
def api_seller_categories():
    """Fetch all distinct categories for the current seller's products"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Fetch distinct categories for this seller's products
        query = '''
            SELECT DISTINCT COALESCE(category, 'Uncategorized') AS category
            FROM products
            WHERE seller_id = %s
            ORDER BY category ASC
        '''
        
        execute_safe(cursor, query, (user_id,))
        results = cursor.fetchall() or []
        cursor.close()
        
        # Filter out None and empty strings, but keep unique categories
        categories = list(set([result['category'] for result in results if result.get('category')]))
        categories.sort()
        
        app.logger.info(f"Seller {user_id} categories: {categories}")
        
        return jsonify({
            "success": True,
            "categories": categories
        })
    except Exception as e:
        app.logger.error(f"Error fetching seller categories: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
