import os
import time
from pathlib import Path
from flask import Flask, render_template, jsonify, request
import psycopg2
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

app = Flask(__name__)

DATABASE_URL = os.getenv("NEON_DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("NEON_DATABASE_URL не задан в .env")


def get_conn(retries=5, delay=2):
    """Открывает соединение с БД, повторяя попытки при холодном старте."""
    last_err = None
    for i in range(retries):
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=15)
        except Exception as e:
            last_err = e
            time.sleep(delay)
    raise last_err


def put_conn(conn):
    """Закрывает соединение."""
    try:
        conn.close()
    except Exception:
        pass


# --- Инициализация таблиц ---
def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clicks (
                    id SERIAL PRIMARY KEY,
                    count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS upgrades (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) UNIQUE NOT NULL,
                    multiplier INTEGER DEFAULT 1,
                    cost INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id SERIAL PRIMARY KEY,
                    player VARCHAR(100) DEFAULT 'guest',
                    clicks_made INTEGER DEFAULT 0,
                    started_at TIMESTAMP DEFAULT NOW(),
                    ended_at TIMESTAMP
                );

                INSERT INTO clicks (count)
                SELECT 0
                WHERE NOT EXISTS (SELECT 1 FROM clicks);
            """)
        conn.commit()
    except Exception as e:
        print(f"[init_db] {type(e).__name__}: {e}")
    finally:
        put_conn(conn)


# --- Каталог доступных апгрейдов ---
CATALOG = [
    {"name": "Двойной клик",     "multiplier": 2, "cost": 50},
    {"name": "Тройной клик",     "multiplier": 3, "cost": 150},
    {"name": "Пятикратный клик", "multiplier": 5, "cost": 500},
]


# --- Работа с данными ---
def get_total_clicks():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, count FROM clicks ORDER BY id LIMIT 1;")
            row = cur.fetchone()
            if not row:
                cur.execute("INSERT INTO clicks (count) VALUES (0) RETURNING id, count;")
                row = cur.fetchone()
                conn.commit()
            return row[1] or 0, row[0]
    finally:
        put_conn(conn)


def set_total_clicks(row_id, value):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE clicks SET count = %s WHERE id = %s;", (value, row_id))
        conn.commit()
    finally:
        put_conn(conn)


def get_multiplier():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(multiplier), 0) FROM upgrades;")
            total = cur.fetchone()[0] or 0
            return total if total > 0 else 1
    finally:
        put_conn(conn)


def get_purchased_names():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM upgrades ORDER BY id;")
            return [r[0] for r in cur.fetchall()]
    finally:
        put_conn(conn)


def current_session():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, clicks_made FROM sessions
                WHERE ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1;
            """)
            return cur.fetchone()
    finally:
        put_conn(conn)


def start_session(player="guest"):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (player, clicks_made) VALUES (%s, 0) RETURNING id, clicks_made;",
                (player,),
            )
            row = cur.fetchone()
        conn.commit()
        return row
    finally:
        put_conn(conn)


def bump_session(session_id, clicks):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET clicks_made = %s WHERE id = %s;",
                (clicks, session_id),
            )
        conn.commit()
    finally:
        put_conn(conn)


# --- Роуты ---
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET"])
def api_state():
    try:
        total, _ = get_total_clicks()
        mult = get_multiplier()
        purchased = get_purchased_names()
        session = current_session()
        return jsonify({
            "count": total,
            "multiplier": mult,
            "purchased": purchased,
            "session_clicks": session[1] if session else 0,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/all", methods=["GET"])
def api_all():
    """Всё состояние одним соединением и одним SQL-запросом."""
    result = {
        "count": None,
        "multiplier": 1,
        "session_clicks": 0,
        "available": [],
        "purchased": [],
        "partial": False,
    }

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT (SELECT count FROM clicks ORDER BY id LIMIT 1),
                       COALESCE(SUM(multiplier), 0) FROM upgrades;
            """)
            total, mult_sum = cur.fetchone()

            cur.execute("""
                SELECT COALESCE(
                    (SELECT clicks_made FROM sessions
                     WHERE ended_at IS NULL
                     ORDER BY started_at DESC LIMIT 1), 0
                );
            """)
            session_row = cur.fetchone()

            cur.execute("SELECT name FROM upgrades ORDER BY id;")
            purchased = [r[0] for r in cur.fetchall()]

        result["count"] = total or 0
        result["multiplier"] = mult_sum if mult_sum and mult_sum > 0 else 1
        result["session_clicks"] = session_row[0] if session_row else 0
        result["purchased"] = purchased
        result["available"] = [u for u in CATALOG if u["name"] not in purchased]
    except Exception as e:
        result["partial"] = True
        result["error"] = str(e)
    finally:
        if conn is not None:
            put_conn(conn)

    status = 200 if (not result["partial"] or result["count"] is not None) else 500
    return jsonify(result), status


@app.route("/api/increment", methods=["POST"])
def api_increment():
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # 1. Текущий счёт и суммарный множитель
            cur.execute("""
                SELECT (SELECT count FROM clicks ORDER BY id LIMIT 1),
                       COALESCE(SUM(multiplier), 0) FROM upgrades;
            """)
            total, mult_sum = cur.fetchone()
            total = total or 0
            gain = mult_sum if mult_sum and mult_sum > 0 else 1

            # 2. Обновляем счёт
            new_total = total + gain
            cur.execute(
                "UPDATE clicks SET count = %s WHERE id = (SELECT id FROM clicks ORDER BY id LIMIT 1);",
                (new_total,),
            )

            # 3. Обновляем или создаём сессию
            cur.execute("""
                SELECT id, clicks_made FROM sessions
                WHERE ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1;
            """)
            row = cur.fetchone()
            if row:
                session_id, session_clicks = row
                new_session = (session_clicks or 0) + gain
                cur.execute(
                    "UPDATE sessions SET clicks_made = %s WHERE id = %s;",
                    (new_session, session_id),
                )
            else:
                cur.execute("""
                    INSERT INTO sessions (player, clicks_made)
                    VALUES ('guest', %s)
                    RETURNING id, clicks_made;
                """, (gain,))
                _, new_session = cur.fetchone()

        conn.commit()
        return jsonify({
            "count": new_total,
            "gain": gain,
            "session_clicks": new_session,
        })
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            put_conn(conn)


@app.route("/api/upgrades", methods=["GET"])
def api_upgrades():
    try:
        purchased = get_purchased_names()
        available = [u for u in CATALOG if u["name"] not in purchased]
        return jsonify({"available": available, "purchased": purchased})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/buy", methods=["POST"])
def api_buy():
    conn = None
    try:
        data = request.get_json() or {}
        name = data.get("name")
        if not name:
            return jsonify({"error": "name is required"}), 400

        upg = next((u for u in CATALOG if u["name"] == name), None)
        if not upg:
            return jsonify({"error": "upgrade not found"}), 404

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM upgrades;")
            purchased = [r[0] for r in cur.fetchall()]
            if name in purchased:
                return jsonify({"error": "already purchased"}), 400

            cur.execute("SELECT id, count FROM clicks ORDER BY id LIMIT 1;")
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "no clicks row"}), 500
            row_id, total = row

            if total < upg["cost"]:
                return jsonify({"error": "not enough clicks"}), 400

            new_total = total - upg["cost"]
            cur.execute("UPDATE clicks SET count = %s WHERE id = %s;", (new_total, row_id))
            cur.execute(
                "INSERT INTO upgrades (name, multiplier, cost) VALUES (%s, %s, %s);",
                (name, upg["multiplier"], upg["cost"]),
            )

        conn.commit()
        return jsonify({
            "count": new_total,
            "purchased": get_purchased_names(),
        })
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            put_conn(conn)


# --- Однократная инициализация при старте ---
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=5000)