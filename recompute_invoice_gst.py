"""One-off: recompute an existing invoice's GST on the aggregate-per-rate basis
(the same logic new invoices now use) WITHOUT uninvoicing — no delete, bills
untouched. Refuses invoices that already carry a SAP document number, since
those are posted and their amounts must not be altered.

Prints BEFORE/AFTER totals so you can eyeball the change, and logs it to
approval_log. Run from the app directory so it uses the same DATABASE_URL.

Usage:
    python recompute_invoice_gst.py "DPPL/26-27/175" [more numbers/ids ...]
"""
import sys
from database import get_db, get_cursor
from modules.FIN01 import model


def _find(cur, ref):
    cur.execute('SELECT * FROM invoice_header WHERE invoice_number=%s', [ref])
    row = cur.fetchone()
    if not row and str(ref).isdigit():
        cur.execute('SELECT * FROM invoice_header WHERE id=%s', [int(ref)])
        row = cur.fetchone()
    return dict(row) if row else None


def _line(inv, tag):
    return (f"  {inv['invoice_number']} {tag}: sub={inv['subtotal']} "
            f"cgst={inv['cgst_amount']} sgst={inv['sgst_amount']} "
            f"igst={inv['igst_amount']} total={inv['total_amount']}")


def recompute(ref):
    conn = get_db()
    cur = get_cursor(conn)
    try:
        inv = _find(cur, ref)
        if not inv:
            print(f'  {ref}: NOT FOUND')
            return
        if (inv.get('sap_document_number') or '').strip():
            print(f"  {inv['invoice_number']}: SKIPPED — has SAP document "
                  f"{inv['sap_document_number']} (posted; not altering)")
            return
        print(_line(inv, 'BEFORE'))
        model._reconcile_invoice_gst(cur, inv['id'])
        cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                       VALUES ('FINV01', %s, 'GST recomputed (aggregate per rate)', %s, %s)""",
                    [inv['id'], f"In-place GST reconciliation for {inv['invoice_number']}", 'recompute_script'])
        conn.commit()
        print(_line(_find(cur, ref), 'AFTER '))
    except Exception as e:
        conn.rollback()
        print(f'  {ref}: ERROR {e}')
    finally:
        conn.close()


if __name__ == '__main__':
    refs = sys.argv[1:]
    if not refs:
        print(__doc__)
        sys.exit(1)
    for ref in refs:
        recompute(ref)
