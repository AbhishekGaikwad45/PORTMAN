"""Why are these approved MBCs absent from FIN01 -> Generate Bill?

Read-only. Name-mismatch is already ruled out (the cf01aa22bb33 FK applied with
no orphans), so this checks the remaining filter in FIN01 views.py:809-810 --
the is_billed / billed_quantity gate -- and prints a verdict per MBC line.

    python diag_missing_mbcs.py

Edit MATERIAL_POS to check a different set.
"""
from database import get_db, get_cursor

MATERIAL_POS = [
    '4300025524', '4300025533', '4300025529', '4300025530', '4300025531',
    '4300025532', '4300025534', '4300025535', '4300025732', '4300025665',
    '4300025668', '4300025747', '4300025761', '4300025759', '4300025770',
]

SQL = """
SELECT cd.material_po,
       mh.doc_num,
       mh.mbc_name,
       mh.doc_status,
       cd.customer_name,
       cd.quantity,
       COALESCE(cd.is_billed, 0)       AS is_billed,
       COALESCE(cd.billed_quantity, 0) AS billed_qty,
       cd.bill_id,
       bh.bill_number,
       bh.bill_status
FROM mbc_customer_details cd
JOIN mbc_header mh       ON mh.id = cd.mbc_id
LEFT JOIN bill_header bh ON bh.id = cd.bill_id
WHERE cd.material_po = ANY(%s)
ORDER BY cd.material_po
"""


def verdict(r):
    if r['customer_name'] is None:
        return 'customer_name is NULL -> can never match any customer'
    if not r['is_billed']:
        return (f"PASSES both gates -> should already be visible under "
                f"Customer = {r['customer_name']}. Check Customer Type = Customer, not Agent.")
    if r['bill_id'] is None:
        return ('flagged billed by the ADMIN cutover tool, no bill exists -> hidden from '
                'the billable list, shows only in the Billed tab')
    return (f"already billed on {r['bill_number'] or '?'} "
            f"(status {r['bill_status'] or '?'})")


def main():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(SQL, [MATERIAL_POS])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    found = {r['material_po'] for r in rows}
    for po in MATERIAL_POS:
        if po not in found:
            print(f'{po}  !! no mbc_customer_details row with this material_po')

    print()
    hdr = f"{'MaterialPO':<12}{'MBC':<16}{'Status':<10}{'Customer':<14}" \
          f"{'Qty':>10}{'Billed':>10}  {'Bill':<12}"
    print(hdr)
    print('-' * len(hdr))
    for r in rows:
        print(f"{r['material_po'] or '-':<12}{(r['mbc_name'] or '-')[:15]:<16}"
              f"{(r['doc_status'] or '-')[:9]:<10}{(r['customer_name'] or 'NULL')[:13]:<14}"
              f"{float(r['quantity'] or 0):>10.2f}{float(r['billed_qty'] or 0):>10.2f}  "
              f"{r['bill_number'] or '-':<12}")

    print()
    for r in rows:
        print(f"{r['material_po']}  {verdict(r)}")

    # so you can see at a glance whether it is one cause or several
    print()
    counts = {}
    for r in rows:
        key = verdict(r).split(' ->')[0].split(' (')[0]
        counts[key] = counts.get(key, 0) + 1
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f'{n:>3}  {k}')


if __name__ == '__main__':
    main()
