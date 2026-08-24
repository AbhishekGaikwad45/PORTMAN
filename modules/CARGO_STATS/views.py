from datetime import date, datetime
import json
from typing import Optional, List, Dict, Any

from flask import (
    Blueprint,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for
)

from database import get_db, get_cursor


# ============================================================
# BLUEPRINT
# ============================================================

bp = Blueprint(
    "cargo_stats",
    __name__,
    url_prefix="/module/CARGO_STATS",
    template_folder=".",
)


# ============================================================
# MODULE INFORMATION
# ============================================================

MODULE_INFO = {
    "code": "CARGO_STATS",
    "name": "Cargo Stats",
    "description": "Daily consumption, production and stock (RMHS / PNP) tracking",
    "icon": "bar-chart-2",
}


# ============================================================
# DATABASE
# ============================================================

TABLE_NAME = "stats_cargo"

VALID_SECTIONS = {
    "consumption",
    "production",
    "stock_rmhs",
    "stock_pnp",
}


# ============================================================
# SECTION VALIDATION
# ============================================================

def _validate_section(section: str):

    if section not in VALID_SECTIONS:

        raise ValueError(
            f"Invalid section '{section}'. "
            f"Must be one of {sorted(VALID_SECTIONS)}"
        )

    return section


# ============================================================
# LOGIN
# ============================================================

def _require_login():

    return "user_id" in session


# ============================================================
# UPDATE EXISTING RECORD
#
# IMPORTANT:
# This function NEVER creates a new ID.
# It updates the existing row using ID.
# ============================================================

def update_entry(
    entry_id: int,
    entry_date: date,
    section: str,
    data: Dict[str, Any]
) -> int:

    section = _validate_section(section)

    conn = get_db()
    cur = get_cursor(conn)

    try:

        # ----------------------------------------------------
        # First make sure the ID belongs to this section
        # ----------------------------------------------------

        cur.execute(
            f"""
            SELECT id
            FROM {TABLE_NAME}
            WHERE id = %s
              AND section = %s;
            """,
            (
                entry_id,
                section
            )
        )

        existing = cur.fetchone()

        if not existing:

            raise ValueError(
                f"Record with ID {entry_id} "
                f"does not exist."
            )


        # ----------------------------------------------------
        # UPDATE SAME ROW
        # ----------------------------------------------------

        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET
                entry_date = %s,
                data = %s,
                updated_at = now()
            WHERE id = %s
              AND section = %s
            RETURNING id;
            """,
            (
                entry_date,
                json.dumps(data),
                entry_id,
                section
            )
        )

        row = cur.fetchone()

        if not row:

            raise ValueError(
                f"Unable to update record "
                f"{entry_id}."
            )

        conn.commit()

        return row["id"]

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# ============================================================
# INSERT NEW RECORD
#
# This is ONLY called for a genuinely new row.
# ============================================================

def insert_entry(
    entry_date: date,
    section: str,
    data: Dict[str, Any]
) -> int:

    section = _validate_section(section)

    conn = get_db()
    cur = get_cursor(conn)

    try:

        # ----------------------------------------------------
        # Do not allow duplicate date + section
        # ----------------------------------------------------

        cur.execute(
            f"""
            SELECT id
            FROM {TABLE_NAME}
            WHERE entry_date = %s
              AND section = %s
            LIMIT 1;
            """,
            (
                entry_date,
                section
            )
        )

        existing = cur.fetchone()

        if existing:

            raise ValueError(
                f"A {section} record already "
                f"exists for {entry_date}."
            )


        # ----------------------------------------------------
        # INSERT
        # ----------------------------------------------------

        cur.execute(
            f"""
            INSERT INTO {TABLE_NAME}
            (
                entry_date,
                section,
                data,
                updated_at
            )
            VALUES
            (
                %s,
                %s,
                %s,
                now()
            )
            RETURNING id;
            """,
            (
                entry_date,
                section,
                json.dumps(data)
            )
        )

        row = cur.fetchone()

        conn.commit()

        return row["id"]

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# ============================================================
# GET RECORD BY ID
# ============================================================

def get_entry_by_id(
    entry_id: int,
    section: str
):

    section = _validate_section(section)

    conn = get_db()
    cur = get_cursor(conn)

    try:

        cur.execute(
            f"""
            SELECT
                id,
                entry_date,
                section,
                data,
                updated_at
            FROM {TABLE_NAME}
            WHERE id = %s
              AND section = %s;
            """,
            (
                entry_id,
                section
            )
        )

        return cur.fetchone()

    finally:

        conn.close()


# ============================================================
# GET HISTORY
# ============================================================

def get_history(
    section: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None
) -> List[Dict[str, Any]]:

    section = _validate_section(section)

    where_parts = [
        "section = %s"
    ]

    params: List[Any] = [
        section
    ]

    if start_date:

        where_parts.append(
            "entry_date >= %s"
        )

        params.append(
            start_date
        )

    if end_date:

        where_parts.append(
            "entry_date <= %s"
        )

        params.append(
            end_date
        )

    where_clause = (
        "WHERE " +
        " AND ".join(where_parts)
    )

    conn = get_db()
    cur = get_cursor(conn)

    try:

        cur.execute(
            f"""
            SELECT
                id,
                entry_date,
                section,
                data,
                updated_at
            FROM {TABLE_NAME}
            {where_clause}
            ORDER BY entry_date DESC, id DESC;
            """,
            params
        )

        return cur.fetchall()

    finally:

        conn.close()


# ============================================================
# DELETE BY ID
# ============================================================

def delete_entry_by_id(
    entry_id: int,
    section: str
):

    section = _validate_section(section)

    conn = get_db()
    cur = get_cursor(conn)

    try:

        cur.execute(
            f"""
            DELETE FROM {TABLE_NAME}
            WHERE id = %s
              AND section = %s;
            """,
            (
                entry_id,
                section
            )
        )

        conn.commit()

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# ============================================================
# SERIALIZE
# ============================================================

def serialize_row(row):

    if row is None:

        return None

    raw_data = row["data"]

    if isinstance(raw_data, str):

        raw_data = json.loads(
            raw_data
        )

    out = dict(
        raw_data or {}
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Always return database ID.
    # JavaScript uses this ID for updates.
    # --------------------------------------------------------

    out["id"] = row["id"]


    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    ed = row["entry_date"]

    if isinstance(ed, date):

        out["date"] = ed.isoformat()

    else:

        out["date"] = ed


    # --------------------------------------------------------
    # Section
    # --------------------------------------------------------

    out["section"] = row["section"]


    # --------------------------------------------------------
    # Updated
    # --------------------------------------------------------

    ua = row["updated_at"]

    if isinstance(ua, datetime):

        out["updated_at"] = ua.isoformat()

    else:

        out["updated_at"] = ua


    return out


# ============================================================
# PAGE
# ============================================================

@bp.route("/")
def index():

    if not _require_login():

        return redirect(
            url_for("login")
        )

    return render_template(
        "stats.html"
    )


# ============================================================
# GET DATA
# ============================================================

@bp.route(
    "/api/<section>",
    methods=["GET"]
)
def api_list(section):

    if not _require_login():

        return jsonify({
            "error": "Not logged in"
        }), 401

    try:

        rows = get_history(
            section
        )

    except ValueError as e:

        return jsonify({
            "error": str(e)
        }), 400

    return jsonify([
        serialize_row(row)
        for row in rows
    ])


# ============================================================
# SAVE
#
# EXISTING ROW:
#     ID exists -> UPDATE
#
# NEW ROW:
#     ID doesn't exist -> INSERT
# ============================================================

@bp.route(
    "/api/<section>",
    methods=["POST"]
)
def api_upsert(section):

    if not _require_login():

        return jsonify({
            "error": "Not logged in"
        }), 401


    # --------------------------------------------------------
    # Validate section
    # --------------------------------------------------------

    try:

        _validate_section(
            section
        )

    except ValueError as e:

        return jsonify({
            "error": str(e)
        }), 400


    # --------------------------------------------------------
    # Read JSON
    # --------------------------------------------------------

    payload = request.get_json(
        silent=True
    ) or {}


    entry_id = payload.get(
        "id"
    )

    entry_date_str = payload.get(
        "date"
    )

    entry_data = payload.get(
        "data"
    )


    # --------------------------------------------------------
    # Validate date
    # --------------------------------------------------------

    if not entry_date_str:

        return jsonify({
            "error": "Date is required."
        }), 400

    try:

        entry_date = date.fromisoformat(
            entry_date_str
        )

    except ValueError:

        return jsonify({
            "error": (
                "Invalid date. "
                "Expected YYYY-MM-DD."
            )
        }), 400


    # --------------------------------------------------------
    # Validate data
    # --------------------------------------------------------

    if not isinstance(
        entry_data,
        dict
    ):

        return jsonify({
            "error": "Data must be an object."
        }), 400


    # ========================================================
    # EXISTING ROW
    #
    # ID IS PRESENT
    #
    # UPDATE ONLY
    # ========================================================

    if entry_id is not None:

        try:

            entry_id = int(
                entry_id
            )

        except (
            ValueError,
            TypeError
        ):

            return jsonify({
                "error": "Invalid record ID."
            }), 400


        try:

            updated_id = update_entry(
                entry_id=entry_id,
                entry_date=entry_date,
                section=section,
                data=entry_data
            )


        except ValueError as e:

            return jsonify({
                "error": str(e)
            }), 404


        except Exception as e:

            error_text = str(e)

            if (
                "uq_stats_cargo_entry_date_section"
                in error_text
            ):

                return jsonify({
                    "error": (
                        f"A {section} record already "
                        f"exists for {entry_date}."
                    )
                }), 409

            return jsonify({
                "error": error_text
            }), 500


        # ----------------------------------------------------
        # Get SAME ROW after update
        # ----------------------------------------------------

        saved = get_entry_by_id(
            updated_id,
            section
        )


        return jsonify({
            "success": True,
            "created": False,
            "updated": True,
            "id": updated_id,
            "data": serialize_row(saved)
        }), 200


    # ========================================================
    # NEW ROW
    #
    # NO ID
    #
    # ONLY HERE DO WE INSERT
    # ========================================================

    try:

        new_id = insert_entry(
            entry_date=entry_date,
            section=section,
            data=entry_data
        )

    except ValueError as e:

        return jsonify({
            "error": str(e)
        }), 409

    except Exception as e:

        error_text = str(e)

        if (
            "uq_stats_cargo_entry_date_section"
            in error_text
        ):

            return jsonify({
                "error": (
                    f"A {section} record already "
                    f"exists for {entry_date}."
                )
            }), 409

        return jsonify({
            "error": error_text
        }), 500


    saved = get_entry_by_id(
        new_id,
        section
    )


    return jsonify({
        "success": True,
        "created": True,
        "updated": False,
        "id": new_id,
        "data": serialize_row(saved)
    }), 200


# ============================================================
# DELETE
# ============================================================

@bp.route(
    "/api/<section>/id/<int:entry_id>",
    methods=["DELETE"]
)
def api_delete_by_id(
    section,
    entry_id
):

    if not _require_login():

        return jsonify({
            "error": "Not logged in"
        }), 401

    try:

        delete_entry_by_id(
            entry_id,
            section
        )

    except ValueError as e:

        return jsonify({
            "error": str(e)
        }), 400

    return jsonify({
        "success": True,
        "deleted": True
    }), 200