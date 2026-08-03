"""Proves the cargo -> customer FK from fix_customer_name_link.sql actually holds.

Fails loudly if the constraint is missing — which is the state that let approved
MBCs disappear from FIN01 Generate Bill. Everything runs in one transaction and
is rolled back, so it is safe against any database including production.

    python test_customer_name_link.py
"""
from database import get_db, get_cursor

TABLES = ('mbc_customer_details', 'vcn_cargo_declaration', 'vcn_export_cargo_declaration')


def main():
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute("INSERT INTO vessel_customers (name) VALUES ('ZZ_TEST_CUST') RETURNING id")
        cust_id = cur.fetchone()['id']
        cur.execute("INSERT INTO mbc_header (doc_num, mbc_name, doc_status) "
                    "VALUES ('ZZ_TEST_MBC', 'ZZ Test', 'Approved') RETURNING id")
        mbc_id = cur.fetchone()['id']
        cur.execute("INSERT INTO mbc_customer_details (mbc_id, customer_name, quantity) "
                    "VALUES (%s, 'ZZ_TEST_CUST', 100) RETURNING id", [mbc_id])
        cd_id = cur.fetchone()['id']

        # 1. every declaration table is protected, not just the one that bit us
        cur.execute("""SELECT c.conrelid::regclass::text AS tbl
                       FROM pg_constraint c
                       WHERE c.contype = 'f'
                         AND c.confrelid = 'vessel_customers'::regclass
                         AND pg_get_constraintdef(c.oid) LIKE '%%customer_name%%'""")
        guarded = {r['tbl'] for r in cur.fetchall()}
        missing = set(TABLES) - guarded
        assert not missing, f'no customer_name FK on: {sorted(missing)} - run fix_customer_name_link.sql'

        # 2. renaming the master must carry the cargo with it
        cur.execute("UPDATE vessel_customers SET name = 'ZZ_TEST_RENAMED' WHERE id = %s", [cust_id])
        cur.execute("SELECT customer_name FROM mbc_customer_details WHERE id = %s", [cd_id])
        got = cur.fetchone()['customer_name']
        assert got == 'ZZ_TEST_RENAMED', f'rename did not cascade: cargo still says {got!r}'

        # 3. a name with no master row must be refused at write time
        cur.execute('SAVEPOINT sp')
        try:
            cur.execute("UPDATE mbc_customer_details SET customer_name = 'ZZ_NO_SUCH_CUSTOMER' "
                        "WHERE id = %s", [cd_id])
            raise AssertionError('unknown customer_name was accepted — FK is not enforcing')
        except AssertionError:
            raise
        except Exception:
            cur.execute('ROLLBACK TO SAVEPOINT sp')

        # 4. deleting a customer that still owns cargo must be refused
        cur.execute('SAVEPOINT sp2')
        try:
            cur.execute('DELETE FROM vessel_customers WHERE id = %s', [cust_id])
            raise AssertionError('deleted a customer that still owns cargo — orphans possible')
        except AssertionError:
            raise
        except Exception:
            cur.execute('ROLLBACK TO SAVEPOINT sp2')

        print('OK: rename cascades, unknown names rejected, in-use customer undeletable')
    finally:
        conn.rollback()
        conn.close()


if __name__ == '__main__':
    main()
