# AUD02 — Record-level Audit Logs + Soft Delete

**Date:** 2026-06-26
**Modules in scope:** LDUD01, VCN01, LUEU01, MBC01
**New module:** AUD02 (Audit Trail viewer)

## Goal

Capture *who* added / edited / deleted records in LDUD01, VCN01, LUEU01, MBC01,
make all deletes in those modules **non-destructive (soft delete)**, and surface
the trail + deleted records in a new **AUD02** module with restore capability.

Hard constraint: **no existing data may be altered.** Changes are purely additive
(new nullable columns + one new table). No backfill, no `UPDATE` of existing rows.
Existing user workflows must behave exactly as before.

AUD01 already exists but only parses nginx HTTP access logs — it is unrelated and
stays as-is. AUD02 is the record/data-level trail.

## Decisions (confirmed with user)

- **Audit detail:** action-level only (who / when / module / table / record id /
  action / endpoint / status). No field-level value diffs.
- **Soft delete:** `deleted_at` flag column (non-destructive). Not an archive table.
- **AUD02:** allows restore of soft-deleted records (not display-only).

## A. Audit capture — single `after_request` hook

A new table:

```
audit_log (
  id           SERIAL PRIMARY KEY,
  module_code  TEXT NOT NULL,
  table_name   TEXT,
  record_id    INTEGER,
  action       TEXT NOT NULL,        -- INSERT | UPDATE | DELETE | RESTORE
  http_method  TEXT,
  path         TEXT,
  status_code  INTEGER,
  user_id      INTEGER,
  username     TEXT,
  created_at   TIMESTAMP NOT NULL DEFAULT now()
)
```
Index on `(module_code, created_at DESC)` and `(table_name, record_id)`.

One `after_request` hook registered in `app.py`. For each response it logs a row
only when **all** hold:
- `request.path` matches `^/api/module/(LDUD01|VCN01|LUEU01|MBC01)/`
- `request.method == 'POST'`
- `200 <= response.status_code < 300`

Derivation:
- **action:** path ends in `/delete` → `DELETE`; else `INSERT` when the request
  JSON has no truthy `id`, else `UPDATE`.
- **record_id:** request JSON `id`, else response JSON `id` (covers new inserts).
- **module_code:** from the path segment.
- **table_name:** mapped from the endpoint segment via a small dict
  (e.g. `nominations` → `vcn_nominations`, bare `save`/`delete` → the module's
  header table); falls back to the raw segment if unmapped.

Hook reads `session` for `user_id` / `username`. Failures inside the hook are
swallowed (logging must never break a user request).

> ponytail: action/table_name are inferred, not authoritative — but path, method,
> status, user, and timestamp are always exact. The inference only labels; it
> cannot lose or corrupt data.

This adds **zero edits** to the ~35 existing save/delete endpoints.

## B. Soft delete — `deleted_at` flag

### Tables (20 total)

| Module | Tables |
|--------|--------|
| VCN01  | vcn_header, vcn_nominations, vcn_delays, vcn_cargo_declaration, vcn_export_cargo_declaration, vcn_stowage_plan |
| LDUD01 | ldud_header, ldud_delays, ldud_barge_lines, ldud_anchorage, ldud_vessel_operations, ldud_barge_cleaning, ldud_hold_completion |
| LUEU01 | lueu_lines |
| MBC01  | mbc_header, mbc_load_port_lines, mbc_discharge_port_lines, mbc_cleaning_details, mbc_export_load_port_lines, mbc_customer_details |

### Migration
Add to every table above:
```
deleted_at  TIMESTAMP NULL
deleted_by  INTEGER NULL
```
Both default NULL → every existing row reads as "not deleted". No data touched.

### Model changes
- Each `delete_*` function: `DELETE FROM t WHERE id=%s`
  → `UPDATE t SET deleted_at = now(), deleted_by = %s WHERE id = %s`.
  The matching view delete endpoint passes `session.get('user_id')`.
- Every list/read SELECT against these tables gains `AND deleted_at IS NULL`
  (or `WHERE deleted_at IS NULL`). This is the **highest-risk** part: a missed
  filter shows a ghost (soft-deleted) row. Each model's read functions must be
  enumerated and filtered during implementation.

### Header cascade
Child tables reference headers with `ON DELETE CASCADE`. With soft delete the
`DELETE` never fires, so children are **not** removed — they survive intact and
are simply unreachable while the header is hidden from its list. Restoring the
header brings the children back unchanged.

> ponytail: soft-delete the header row only, not its children. Children are only
> reachable via the header (now hidden), and staying intact is exactly what makes
> restore correct and the operation non-destructive.

## C. AUD02 module

Folder `modules/AUD02/` with `__init__.py`, `views.py`, `aud02.html`, following
the AUD01/VCN01 blueprint pattern. One `register_module(...)` line in `app.py`
→ appears in the home menu and Admin permissions grid automatically (module
registry is in-code; permissions are per `(user_id, module_code)` rows, no seed).

Routes:
- `GET  /module/AUD02/` — page (requires `can_read`)
- `GET  /api/module/AUD02/data` — paginated `audit_log`, filterable by
  module_code, user, action, and date range; newest first
- `GET  /api/module/AUD02/deleted` — currently soft-deleted rows across the 20
  tables (module, table, record id, a label column, deleted_by username,
  deleted_at), via `UNION ALL` over the tables where `deleted_at IS NOT NULL`
- `POST /api/module/AUD02/restore` — body `{table, id}`; `table` validated
  against a hardcoded whitelist of the 20 names; sets `deleted_at = NULL,
  deleted_by = NULL`; requires `can_edit`; writes a `RESTORE` row to `audit_log`
  inline (the after_request hook only covers the four data modules, not AUD02)

`deleted_by` / `user_id` are joined to the `users` table for display names.

## D. Permissions

Reuse `module_permissions`: `can_read` to view AUD02, `can_edit` to restore.
Admin grants these in the existing Admin → permissions UI once AUD02 is registered.

## E. Test

One pytest (`test_aud02_soft_delete.py`) against the dev DB pattern used by the
existing `test_*.py` files:
1. soft-deleting a row keeps it in the table and sets `deleted_at`
2. the model's list SELECT no longer returns it
3. restore clears `deleted_at` and the row reappears
4. an `audit_log` row is written for a simulated POST mutation

## Non-goals

- No field-level value diffs.
- No changes to modules outside the four named.
- No nginx/AUD01 changes.
- No existing-row backfill or data migration.

## Files touched

- `alembic/versions/<new>_aud02_audit_and_soft_delete.py` — `audit_log` table +
  `deleted_at`/`deleted_by` on 20 tables
- `app.py` — `after_request` audit hook; import + `register_module` for AUD02
- `modules/{VCN01,LDUD01,LUEU01,MBC01}/model.py` — delete fns → UPDATE; SELECT
  filters
- `modules/{VCN01,LDUD01,LUEU01,MBC01}/views.py` — pass `user_id` into delete fns
- `modules/AUD02/{__init__.py,views.py,aud02.html}` — new module
- `test_aud02_soft_delete.py` — new test
