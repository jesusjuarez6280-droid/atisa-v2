CORRIDAS = {'v1': 0, 'v2': 0}


def _v1(conn):
    CORRIDAS['v1'] += 1
    conn.execute('''CREATE TABLE IF NOT EXISTS prb_cosas (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL, bloquea INTEGER DEFAULT 0)''')


def _v2(conn):
    CORRIDAS['v2'] += 1
    conn.execute("ALTER TABLE prb_cosas ADD COLUMN nota TEXT")


MIGRACIONES = [(1, _v1), (2, _v2)]
