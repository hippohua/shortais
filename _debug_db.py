import traceback
try:
    import sqlite3
    conn = sqlite3.connect('data/shortais.db')
    c = conn.cursor()

    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in c.fetchall()]

    f = open('_dbg.txt', 'w', encoding='utf-8')
    f.write("TABLES: " + str(tables) + "\n")
    f.flush()

    if 'rank1_history' in tables:
        c.execute("SELECT * FROM rank1_history ORDER BY date DESC")
        rows = c.fetchall()
        f.write("RANK1 COUNT: " + str(len(rows)) + "\n")
        for r in rows:
            f.write("RANK1: " + str(dict(r)) + "\n")
        f.flush()
    else:
        f.write("NO rank1_history table\n")
        f.flush()

    f.close()
    conn.close()
    print("OK")
except Exception as e:
    with open('_dbg.txt', 'w', encoding='utf-8') as f:
        f.write("ERROR: " + str(e) + "\n")
        f.write(traceback.format_exc())
    print("ERROR")
