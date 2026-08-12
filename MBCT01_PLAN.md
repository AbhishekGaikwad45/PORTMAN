# MBCT01 — MBC Timings (plan)

Marine users record date/time values against **existing MBC01 documents**. They have not
settled on *which* times they want. So the field list is data, not code: an admin defines a
versioned field set, and can revert to any earlier version without losing captured values.

**MBCT01 never creates, renames or deletes an MBC document.** It only hangs values off
`mbc_header.id`. Same rule as the port-overview dashboards: the owning module owns existence.

---

## 1. The EAV shape (2 tables, JSONB — not classic entity/attribute/value)

Classic EAV needs 3 tables and a join + cast per field. Postgres JSONB does the same job in two
tables with no joins, so that is what this uses.

### `mbct_field_version` — the schema, versioned

| column | type | note |
|---|---|---|
| `id` | SERIAL PK | |
| `version_no` | INTEGER | 1, 2, 3 … monotonic |
| `label` | TEXT | free text, e.g. "Added tug times — Jul rev" |
| `fields_json` | JSONB | the whole field set for this version |
| `is_active` | BOOLEAN | exactly one true (partial unique index) |
| `created_by` | TEXT | |
| `created_at` | TIMESTAMP | default now() |

`fields_json` element shape:

```json
{"key": "pilot_on_board", "label": "Pilot On Board", "type": "datetime", "order": 1}
```

`type` ∈ `datetime` | `date` | `time`.

### `mbct_times` — the values, **not** versioned

| column | type | note |
|---|---|---|
| `mbc_id` | INTEGER PK | FK → `mbc_header(id) ON DELETE CASCADE` |
| `values_json` | JSONB | `{"pilot_on_board": "2026-08-04T14:30", …}` default `'{}'` |
| `updated_by` | TEXT | |
| `updated_at` | TIMESTAMP | |

### Why values are not keyed by version — the whole point

One row per MBC, one flat bag keyed by **field key**. Consequences, all of them wanted:

- **Revert is free and lossless.** Flipping `is_active` back to v1 re-shows v1's fields, and
  their old values are still sitting in the bag untouched.
- **A field dropped in v3 is hidden, never deleted.** Re-add the same key in v5 and the data
  reappears.
- **A field surviving v1 → v4 keeps one continuous history.** Per-version value rows would have
  fragmented it into four disconnected copies.

The one rule that makes this hold: **`key` is immutable once created; `label` is freely
editable.** Renaming a label is cosmetic. Renaming a key orphans data — so the UI generates the
key from the label on first creation only, then locks the key field.

---

## 2. Version lifecycle

- Editing the field set **never mutates a version in place.** Save always writes
  `version_no = MAX + 1` and activates it. This is what makes "revert" mean anything, and it is
  cheaper than a diff/audit trail.
- **Revert = activate an old version.** `UPDATE … SET is_active=false; UPDATE … SET is_active=true
  WHERE id=%s`. Two statements, zero data movement.
- Nothing is ever deleted from `mbct_field_version`. The list *is* the history.

---

## 3. Files

```
modules/MBCT01/__init__.py      MODULE_INFO = {'code':'MBCT01','name':'MBC Timings','table':'mbct_times'}
modules/MBCT01/model.py         DB access, mirrors modules/MBC01/model.py conventions
modules/MBCT01/views.py         Blueprint, mirrors modules/MBC01/views.py
modules/MBCT01/mbct01.html      extends base.html
alembic/versions/<rev>_mbct01_timings.py    down_revision = 'a3f5c81b2d47'   ← current head
```

Two edits to existing files:

- [app.py](app.py) — one import line beside the MBC01 import (~line 109), one `register_module`
  call beside MBC01's (~line 172).
- [templates/base.html:212-228](templates/base.html#L212-L228) — one `module-item` in the Marine
  accordion:
  ```html
  <div class="module-item" onclick="goToModule('MBCT01', 'MBC Timings')">
      <span class="code">MBCT01</span><span class="name">MBC Timings</span>
  </div>
  ```

No new dependency. No new base-template CSS.

---

## 4. Endpoints

All `@login_required`; data routes use `get_user_permissions(user_id, 'MBCT01')` exactly as
[modules/MBC01/views.py:23-26](modules/MBC01/views.py#L23-L26) does.

| route | method | gate | does |
|---|---|---|---|
| `/module/MBCT01/` | GET | `can_read` | renders page; passes active `fields_json` to the template |
| `/api/module/MBCT01/data` | GET | `can_read` | paginated MBC list + flattened values |
| `/api/module/MBCT01/save` | POST | `can_edit` | merge one MBC's values |
| `/api/module/MBCT01/versions` | GET | **admin** | version list (no `fields_json`, keeps it small) |
| `/api/module/MBCT01/versions/<id>` | GET | **admin** | one version's `fields_json` |
| `/api/module/MBCT01/versions/save` | POST | **admin** | create version MAX+1, activate it |
| `/api/module/MBCT01/versions/activate` | POST | **admin** | revert — flip `is_active` |

Admin gate is `session.get('is_admin')`, same test the base template already uses for the GSTCFG
item at [templates/base.html:331](templates/base.html#L331).

### The data query

```sql
SELECT h.id, h.doc_num, h.doc_date, h.mbc_name, h.operation_type,
       h.cargo_name, h.doc_status,
       COALESCE(t.values_json, '{}'::jsonb) AS values_json
FROM mbc_header h
LEFT JOIN mbct_times t ON t.mbc_id = h.id
{where} ORDER BY h.id DESC LIMIT %s OFFSET %s
```

Server flattens `values_json` into the row dict before returning, so Tabulator's
`field: "pilot_on_board"` binds with no client-side unpacking. Filter whitelist mirrors
[modules/MBC01/model.py:35-37](modules/MBC01/model.py#L35-L37) — `doc_num`, `mbc_name`,
`doc_date`, `operation_type`, `doc_status`.

### The save — one statement, non-destructive

```sql
INSERT INTO mbct_times (mbc_id, values_json, updated_by, updated_at)
VALUES (%s, %s::jsonb, %s, NOW())
ON CONFLICT (mbc_id) DO UPDATE
  SET values_json = mbct_times.values_json || EXCLUDED.values_json,
      updated_by  = EXCLUDED.updated_by,
      updated_at  = NOW()
```

`||` merges — keys absent from the payload survive. This is the line that guarantees a version
switch cannot destroy data. Server rejects any key not present in the **active** version's
`fields_json`, so a stale browser tab on an old version cannot write.

---

## 5. UI

Single Tabulator, no row expansion (nothing is nested here — one value set per MBC).

- **Frozen left:** Doc Num, Doc Date, MBC Name, Operation Type. Then one column per field in the
  active version, in `order`.
- **Editors, all already in [templates/base.html:718-780](templates/base.html#L718-L780):**
  `datetime` → `datetimeEditor`, `date` → `dateEditor`. `time` → a ~10-line editor wrapping
  native `<input type="time">` (the only new JS in the module).
- **Formatters:** `datetimeCellFormatter` / `dateCellFormatter`, already global.
- Autosave on `cellEdited`, one PATCH per edited MBC. Red row outline on failure, matching
  MBC01's `_autosave_error` treatment at [mbc01.html:753](modules/MBC01/mbc01.html#L753).
- Banner under the toolbar: `Field set: v4 — "Added tug times"`. Users must be able to see which
  version they are looking at when the columns change under them.

### Admin: "Manage Fields" button

`{% if session.is_admin %}` in the toolbar. Opens one modal, two panes:

**Left — field editor (the draft):** small Tabulator over the active version's fields —
Label (editable), Key (read-only once saved), Type (list: datetime/date/time), Order. Buttons:
`+ Add Field`, `Remove` (removes from the *next* version; values stay in the DB),
`Save as New Version` (asks for a label).

**Right — version history:** `v4 (active) · "Added tug times" · shubham · 30-07-2026` with an
`Activate` button on every non-active row. That button is the revert.

Removing a field warns once: *"Removed fields are hidden, not deleted. Re-adding the same field
restores its values."* — which is exactly the reassurance an indecisive user needs to stop
hoarding columns.

---

## 6. Migration

`down_revision = 'a3f5c81b2d47'` (head, `cnds01_fdcn_doc_series`). Follows the
`op.execute` + `IF NOT EXISTS` style used throughout `alembic/versions/`.

```sql
CREATE TABLE IF NOT EXISTS mbct_field_version (
    id SERIAL PRIMARY KEY,
    version_no INTEGER NOT NULL,
    label TEXT,
    fields_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mbct_one_active
    ON mbct_field_version (is_active) WHERE is_active;

CREATE TABLE IF NOT EXISTS mbct_times (
    mbc_id INTEGER PRIMARY KEY REFERENCES mbc_header(id) ON DELETE CASCADE,
    values_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by TEXT,
    updated_at TIMESTAMP
);
```

Seed v1 so the page is not blank on first open — three obvious ones, all renameable/removable
later: `pilot_on_board`, `anchor_aweigh`, `all_fast_alongside` (all `datetime`).

`downgrade()` drops both tables.

---

## 7. One test — `test_mbct01.py`

Root-level, plain asserts, matching the existing `test_*.py` files. Three assertions, all on the
behaviour that would silently lose user data if it broke:

1. Save `{a: x}` then `{b: y}` → row holds **both**. (The `||` merge.)
2. Activate v1 → v1's fields return, and a value written under v2 is still readable by key.
3. Activating a version leaves exactly one `is_active = true`.

---

## 8. Open questions — defaults chosen, say the word to change

1. **Billing lock.** MBC01 refuses all writes once the MBC is billed/invoiced
   ([views.py:48-72](modules/MBC01/views.py#L48-L72)). **Default here: no lock** — these timings
   are informational and don't feed billing. If any timing field ever drives a demurrage or
   detention charge, this must flip to calling `fin_model.source_lock_state`.
2. **Approved-MBC lock.** **Default: no lock.** Timings often land after the MBC is approved;
   locking them would defeat the module.
3. **Per-field permissions.** Not planned. Module-level `can_edit` only.
4. **Field types.** Only `datetime` / `date` / `time`. Adding `text` or `number` later is a
   one-line change to the type list plus one editor — but it is not in this plan, because the
   ask was date and time.

---

## 9. Build order

1. Migration + seed v1 → `alembic upgrade head`.
2. `model.py` — data read, merge save, version CRUD.
3. `views.py` — routes + admin gate.
4. Register in `app.py`, link in `base.html`.
5. `mbct01.html` — main grid first, ship it, then the admin modal.
6. `test_mbct01.py`.

Steps 1–5 are usable without the modal: v1's seeded fields are editable from the grid on day one,
and versions can be inserted by hand until the modal lands.
