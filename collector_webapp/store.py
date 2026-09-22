# -*- coding: utf-8 -*-
'''SQLite 存储：任务队列与已采集文章链接'''
import sqlite3
import threading
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'collector.db')

# SQLite 连接跨线程使用需要加锁（worker线程写、API线程读）
_lock = threading.Lock()


def _account_name(value: str) -> str:
    """Return the display name used for account configuration records."""
    return " ".join(str(value or "").split())


def _account_key(value: str) -> str:
    """Normalized identity key used to prevent imported display-name aliases."""
    key = _account_name(value).casefold()
    # This legacy import typo refers to the already existing canonical account.
    return {'慧聪机械工程网': '慧聪工程机械网'}.get(key, key)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _conn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT NOT NULL,
                number INTEGER NOT NULL,
                remove_favorite INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',   -- pending/collecting/extracting/done/stopped/failed
                collected INTEGER DEFAULT 0,     -- 已收藏篇数
                extracted INTEGER DEFAULT 0,     -- 已提取链接数
                created_at TEXT,
                finished_at TEXT,
                error TEXT
            )''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                account TEXT,
                collected_at TEXT
            )''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS task_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT DEFAULT 'pending',
                total_count INTEGER DEFAULT 0,
                completed_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                skip_failed INTEGER DEFAULT 1,
                created_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                error TEXT
            )''')
        columns = {row[1] for row in c.execute('PRAGMA table_info(tasks)').fetchall()}
        if 'remove_favorite' not in columns:
            c.execute('ALTER TABLE tasks ADD COLUMN remove_favorite INTEGER DEFAULT 0')
        c.execute('''CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            group_id INTEGER, enabled INTEGER DEFAULT 1, default_number INTEGER DEFAULT 10,
            created_at TEXT, updated_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS account_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            sort_order INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)''')
        c.executemany('INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)', [
            ('default_number', '10', None),
            ('remove_favorite', 'false', None),
            ('log_limit', '200', None),
            ('refresh_interval', '5', None),
        ])
        c.execute('''INSERT OR IGNORE INTO accounts(name,created_at,updated_at)
                     SELECT DISTINCT account, COALESCE(created_at, datetime('now')), COALESCE(created_at, datetime('now'))
                     FROM tasks WHERE account IS NOT NULL AND account <> '' ''')
        c.execute('''INSERT OR IGNORE INTO accounts(name,created_at,updated_at)
                     SELECT DISTINCT account, COALESCE(collected_at, datetime('now')), COALESCE(collected_at, datetime('now'))
                     FROM articles WHERE account IS NOT NULL AND account <> '' ''')
        account_columns = {row[1] for row in c.execute('PRAGMA table_info(accounts)').fetchall()}
        if 'group_id' not in account_columns:
            c.execute('ALTER TABLE accounts ADD COLUMN group_id INTEGER')
        task_columns = {row[1] for row in c.execute('PRAGMA table_info(tasks)').fetchall()}
        for name, definition in {
            'account_id': 'INTEGER', 'retry_count': 'INTEGER DEFAULT 0',
            'started_at': 'TEXT', 'batch_id': 'INTEGER',
            'collection_start_date': 'TEXT', 'collection_end_date': 'TEXT',
            'continue_on_failure': 'INTEGER DEFAULT 0'
        }.items():
            if name not in task_columns:
                c.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
        article_columns = {row[1] for row in c.execute('PRAGMA table_info(articles)').fetchall()}
        for name, definition in {'task_id': 'INTEGER', 'account_id': 'INTEGER', 'published_at': 'TEXT'}.items():
            if name not in article_columns:
                c.execute(f'ALTER TABLE articles ADD COLUMN {name} {definition}')
        # Existing rows are backfilled by stable account name without altering history.
        c.execute('UPDATE tasks SET account_id=(SELECT id FROM accounts WHERE accounts.name=tasks.account) WHERE account_id IS NULL')
        c.execute('UPDATE articles SET account_id=(SELECT id FROM accounts WHERE accounts.name=articles.account) WHERE account_id IS NULL')
        _dedupe_accounts(c)
        # SQLite's normal UNIQUE(name) constraint is case/space sensitive.
        # Keep a second normalized index so future imports cannot recreate
        # duplicate account configurations after the initial migration.
        c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_name_normalized
                     ON accounts(lower(trim(name)))''')


def _dedupe_accounts(c: sqlite3.Connection) -> int:
    """Merge exact-name account duplicates while preserving task/article links."""
    rows = c.execute('SELECT * FROM accounts ORDER BY id').fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(_account_key(row['name']), []).append(row)
    groups = [rows for rows in grouped.values() if len(rows) > 1]
    removed = 0
    for rows in groups:
        rows = sorted(rows, key=lambda r: (-c.execute('SELECT COUNT(*) FROM articles WHERE account_id=?', (r['id'],)).fetchone()[0],
                                           -c.execute('SELECT COUNT(*) FROM tasks WHERE account_id=?', (r['id'],)).fetchone()[0], r['id']))
        keep = rows[0]
        for duplicate in rows[1:]:
            duplicate_id = duplicate['id']
            c.execute('UPDATE tasks SET account_id=? WHERE account_id=?', (keep['id'], duplicate_id))
            c.execute('UPDATE articles SET account_id=? WHERE account_id=?', (keep['id'], duplicate_id))
            # Historical names are retained; only the account configuration row is merged.
            c.execute('DELETE FROM accounts WHERE id=?', (duplicate_id,))
            removed += 1
    return removed


def dedupe_accounts() -> int:
    with _lock, _conn() as c:
        return _dedupe_accounts(c)


def recover_interrupted_tasks() -> int:
    """Mark tasks interrupted by a process restart as retryable failures."""
    now = datetime.now().isoformat(timespec='seconds')
    affected = set()
    with _lock, _conn() as c:
        affected = {r[0] for r in c.execute(
            "SELECT DISTINCT batch_id FROM tasks WHERE status IN ('collecting', 'extracting') AND batch_id IS NOT NULL").fetchall()}
        cur = c.execute(
            """UPDATE tasks SET status='failed', finished_at=?,
               error=COALESCE(error, '服务重启导致任务中断，请重试')
               WHERE status IN ('collecting', 'extracting')""", (now,))
    for batch_id in affected:
        refresh_batch(batch_id)
    return cur.rowcount


# ---------- 任务 ----------

def add_task(account: str, number: int, remove_favorite: bool = False,
             batch_id: int | None = None, collection_start_date: str | None = None,
             collection_end_date: str | None = None, continue_on_failure: bool = False) -> dict:
    now = datetime.now().isoformat(timespec='seconds')
    with _lock, _conn() as c:
        account_row = c.execute('SELECT id FROM accounts WHERE name=?', (account,)).fetchone()
        cur = c.execute(
            '''INSERT INTO tasks(account,account_id,number,remove_favorite,batch_id,
               collection_start_date,collection_end_date,continue_on_failure,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (account, account_row[0] if account_row else None, number, int(remove_favorite),
             batch_id, collection_start_date, collection_end_date, int(continue_on_failure), now))
        return {'id': cur.lastrowid, 'account': account, 'number': number,
                'remove_favorite': bool(remove_favorite),
                'status': 'pending', 'collected': 0, 'extracted': 0,
                'batch_id': batch_id, 'collection_start_date': collection_start_date,
                'collection_end_date': collection_end_date,
                'continue_on_failure': bool(continue_on_failure),
                'created_at': now, 'finished_at': None, 'error': None}


def get_task(task_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT * FROM tasks ORDER BY id DESC LIMIT 200').fetchall()]


def query_tasks(status=None, account=None, limit=200, batch_id=None,
                collection_start_date=None, collection_end_date=None) -> list[dict]:
    where, params = [], []
    if status:
        where.append('status=?'); params.append(status)
    if account:
        where.append('account=?'); params.append(account)
    if batch_id is not None:
        where.append('batch_id=?'); params.append(batch_id)
    if collection_start_date:
        where.append('collection_start_date=?'); params.append(collection_start_date)
    if collection_end_date:
        where.append('collection_end_date=?'); params.append(collection_end_date)
    sql = 'SELECT * FROM tasks' + ((' WHERE ' + ' AND '.join(where)) if where else '') + ' ORDER BY id DESC LIMIT ?'
    params.append(min(max(int(limit), 1), 10000))
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def task_stats(account: str | None = None) -> dict:
    """Return queue counters without relying on a truncated task listing."""
    today = datetime.now().strftime('%Y-%m-%d')
    where, params = [], []
    if account:
        where.append('account=?')
        params.append(account)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    with _conn() as c:
        running = c.execute(
            'SELECT COUNT(*) FROM tasks' + clause + (" AND " if clause else " WHERE ") +
            "status IN ('collecting','extracting')", params).fetchone()[0]
        pending = c.execute(
            'SELECT COUNT(*) FROM tasks' + clause + (" AND " if clause else " WHERE ") +
            "status='pending'", params).fetchone()[0]
        today_done_where = list(where) + ["status='done'", "finished_at LIKE ?"]
        today_failed_where = list(where) + ["status='failed'", "finished_at LIKE ?"]
        done_params = [*params, today + '%']
        failed_params = [*params, today + '%']
        today_done = c.execute(
            'SELECT COUNT(*) FROM tasks WHERE ' + ' AND '.join(today_done_where), done_params
        ).fetchone()[0]
        today_failed = c.execute(
            'SELECT COUNT(*) FROM tasks WHERE ' + ' AND '.join(today_failed_where), failed_params
        ).fetchone()[0]
    return {'running_count': running, 'pending_count': pending,
            'today_done_count': today_done, 'today_failed_count': today_failed}


def delete_task(task_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute('DELETE FROM tasks WHERE id=? AND status IN ("pending","done","stopped","failed")',
                        (task_id,))
        return cur.rowcount > 0


def update_task(task_id: int, **fields):
    if not fields:
        return
    cols = ','.join(f'{k}=?' for k in fields)
    with _lock, _conn() as c:
        c.execute(f'UPDATE tasks SET {cols} WHERE id=?', (*fields.values(), task_id))


def retry_task(task_id: int) -> dict | None:
    with _lock, _conn() as c:
        c.execute('''UPDATE tasks SET status='pending', collected=0, extracted=0,
                     error=NULL, finished_at=NULL, started_at=NULL, retry_count=retry_count+1
                     WHERE id=? AND status IN ('failed','stopped')''', (task_id,))
        row = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        result = dict(row) if row else None
    if result and result.get('batch_id'):
        refresh_batch(result['batch_id'])
    return result


def delete_tasks(ids=None, finished_only=False) -> int:
    with _lock, _conn() as c:
        if ids:
            marks = ','.join('?' for _ in ids)
            clause = f'id IN ({marks})' + (" AND status IN ('done','failed','stopped')" if finished_only else '')
            return c.execute(f'DELETE FROM tasks WHERE {clause}', ids).rowcount
        if finished_only:
            return c.execute("DELETE FROM tasks WHERE status IN ('done','failed','stopped')").rowcount
        return 0


# ---------- 文章链接 ----------

def insert_article(url: str, title: str, account: str, task_id: int | None = None,
                   published_at: str | None = None) -> bool:
    '''插入链接，URL 重复时忽略。返回是否为新插入'''
    with _lock, _conn() as c:
        account_row = c.execute('SELECT id FROM accounts WHERE name=?', (account,)).fetchone()
        cur = c.execute(
            '''INSERT OR IGNORE INTO articles(url,title,account,account_id,task_id,
               published_at,collected_at) VALUES(?,?,?,?,?,?,?)''',
            (url, title, account, account_row[0] if account_row else None, task_id,
             published_at, datetime.now().isoformat(timespec='seconds')))
        return cur.rowcount > 0


def list_articles(account: str | None = None, limit: int = 200, search: str | None = None,
                  offset: int = 0, date_from: str | None = None,
                  date_to: str | None = None) -> list[dict]:
    with _conn() as c:
        where, params = [], []
        if account:
            where.append('account=?'); params.append(account)
        if search:
            where.append('(title LIKE ? OR url LIKE ? OR account LIKE ?)')
            params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
        date_expr = "COALESCE(NULLIF(published_at,''), substr(collected_at,1,10))"
        if date_from:
            where.append(f'{date_expr} >= ?'); params.append(date_from)
        if date_to:
            where.append(f'{date_expr} <= ?'); params.append(date_to)
        sql = 'SELECT * FROM articles' + ((' WHERE ' + ' AND '.join(where)) if where else '') + ' ORDER BY id DESC LIMIT ? OFFSET ?'
        params.extend([min(max(int(limit), 1), 10000), max(int(offset), 0)])
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_articles_page(account: str | None = None, search: str | None = None,
                        page: int = 1, page_size: int = 20, days: int | None = None,
                        date_from: str | None = None, date_to: str | None = None) -> dict:
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    where, params = [], []
    if account:
        where.append('account=?'); params.append(account)
    if search:
        where.append('(title LIKE ? OR url LIKE ? OR account LIKE ?)')
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if days:
        where.append('collected_at >= ?')
        params.append((datetime.now() - timedelta(days=int(days))).isoformat(timespec='seconds'))
    date_expr = "COALESCE(NULLIF(published_at,''), substr(collected_at,1,10))"
    if date_from:
        where.append(f'{date_expr} >= ?'); params.append(date_from)
    if date_to:
        where.append(f'{date_expr} <= ?'); params.append(date_to)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    with _conn() as c:
        total = c.execute('SELECT COUNT(*) FROM articles' + clause, params).fetchone()[0]
        rows = c.execute('SELECT * FROM articles' + clause + ' ORDER BY id DESC LIMIT ? OFFSET ?',
                         [*params, page_size, (page - 1) * page_size]).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page,
             'page_size': page_size, 'pages': max((total + page_size - 1) // page_size, 1)}


def query_article_ids(account: str | None = None, search: str | None = None,
                      days: int | None = None, date_from: str | None = None,
                      date_to: str | None = None) -> list[int]:
    """Return all IDs matching the article filters, independent of pagination."""
    where, params = [], []
    if account:
        where.append('account=?'); params.append(account)
    if search:
        where.append('(title LIKE ? OR url LIKE ? OR account LIKE ?)')
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if days:
        where.append('collected_at >= ?')
        params.append((datetime.now() - timedelta(days=int(days))).isoformat(timespec='seconds'))
    date_expr = "COALESCE(NULLIF(published_at,''), substr(collected_at,1,10))"
    if date_from:
        where.append(f'{date_expr} >= ?'); params.append(date_from)
    if date_to:
        where.append(f'{date_expr} <= ?'); params.append(date_to)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    with _conn() as c:
        return [row[0] for row in c.execute('SELECT id FROM articles' + clause + ' ORDER BY id DESC', params).fetchall()]


def create_batch(accounts: list[str], number: int, remove_favorite: bool = False,
                 collection_start_date: str | None = None,
                 collection_end_date: str | None = None,
                 skip_failed: bool = True) -> dict:
    accounts = list(dict.fromkeys(str(x).strip() for x in accounts if str(x).strip()))
    if not accounts:
        raise ValueError('至少选择一个公众号')
    now = datetime.now().isoformat(timespec='seconds')
    with _lock, _conn() as c:
        cur = c.execute('''INSERT INTO task_batches(total_count,skip_failed,created_at)
                           VALUES(?,?,?)''', (len(accounts), int(skip_failed), now))
        batch_id = cur.lastrowid
        tasks = []
        for account in accounts:
            account_row = c.execute('SELECT id FROM accounts WHERE name=?', (account,)).fetchone()
            task_cur = c.execute('''INSERT INTO tasks(account,account_id,number,remove_favorite,batch_id,
                collection_start_date,collection_end_date,continue_on_failure,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', (account, account_row[0] if account_row else None,
                number, int(remove_favorite), batch_id, collection_start_date,
                collection_end_date, int(skip_failed), now))
            tasks.append({'id': task_cur.lastrowid, 'account': account, 'number': number,
                          'status': 'pending', 'batch_id': batch_id})
        return {'batch_id': batch_id, 'status': 'pending', 'total_count': len(tasks), 'tasks': tasks}


def get_batch(batch_id: int) -> dict | None:
    with _conn() as c:
        batch = c.execute('SELECT * FROM task_batches WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            return None
        tasks = [dict(r) for r in c.execute('SELECT * FROM tasks WHERE batch_id=? ORDER BY id', (batch_id,)).fetchall()]
        failed = [{'account': r['account'], 'task_id': r['id'], 'error': r['error']} for r in tasks if r['status'] == 'failed']
        done = [r for r in tasks if r['status'] in ('done', 'failed', 'stopped')]
        running = next((r for r in tasks if r['status'] in ('collecting', 'extracting')), None)
        result = dict(batch)
        result.update({'tasks': tasks, 'failed_accounts': failed,
                       'completed_count': len(done),
                       'success_count': sum(r['status'] == 'done' for r in tasks),
                       'failed_count': len(failed),
                       'current_account': running['account'] if running else None,
                       'current_task_id': running['id'] if running else None,
                       'progress': round(len(done) * 100 / max(1, len(tasks)), 1)})
        return result


def refresh_batch(batch_id: int):
    with _lock, _conn() as c:
        rows = c.execute('SELECT status FROM tasks WHERE batch_id=?', (batch_id,)).fetchall()
        if not rows:
            return
        statuses = [r['status'] for r in rows]
        done = all(s in ('done', 'failed', 'stopped') for s in statuses)
        failed = sum(s == 'failed' for s in statuses)
        success = sum(s == 'done' for s in statuses)
        stopped = sum(s == 'stopped' for s in statuses)
        status = ('stopped' if done and stopped else
                  'partial_failed' if done and failed and success else
                  'failed' if done and failed else 'completed' if done else 'running')
        c.execute('''UPDATE task_batches SET status=?, completed_count=?, success_count=?, failed_count=?,
                     started_at=COALESCE(started_at, CASE WHEN status <> 'pending' THEN ? END),
                     finished_at=CASE WHEN ? THEN COALESCE(finished_at, ?) ELSE finished_at END
                     WHERE id=?''', (status, sum(s in ('done','failed','stopped') for s in statuses), success,
                                     failed, datetime.now().isoformat(timespec='seconds'), done,
                                     datetime.now().isoformat(timespec='seconds'), batch_id))


def article_count() -> int:
    with _conn() as c:
        return c.execute('SELECT COUNT(*) FROM articles').fetchone()[0]


def delete_articles(ids=None, account=None) -> int:
    with _lock, _conn() as c:
        if ids:
            marks = ','.join('?' for _ in ids)
            cur = c.execute(f'DELETE FROM articles WHERE id IN ({marks})', ids)
        elif account:
            cur = c.execute('DELETE FROM articles WHERE account=?', (account,))
        else:
            return 0
        return cur.rowcount


def list_accounts() -> list[dict]:
    dedupe_accounts()
    with _conn() as c:
        return [dict(r) for r in c.execute('SELECT * FROM accounts ORDER BY enabled DESC, name COLLATE NOCASE').fetchall()]


def find_account(name: str) -> dict | None:
    key = _account_key(name)
    with _conn() as c:
        row = next((r for r in c.execute('SELECT * FROM accounts ORDER BY id') if _account_key(r['name']) == key), None)
        return dict(row) if row else None


def upsert_account(name: str, enabled: bool = True, default_number: int = 10) -> dict:
    name = _account_name(name)
    if not name:
        raise ValueError('公众号名称不能为空')
    now = datetime.now().isoformat(timespec='seconds')
    with _lock, _conn() as c:
        key = _account_key(name)
        existing = next((r for r in c.execute('SELECT id,name FROM accounts ORDER BY id')
                         if _account_key(r['name']) == key), None)
        if existing:
            c.execute('''UPDATE accounts SET enabled=?,default_number=?,updated_at=?
                         WHERE id=?''', (int(enabled), default_number, now, existing['id']))
            return dict(c.execute('SELECT * FROM accounts WHERE id=?', (existing['id'],)).fetchone())
        cur = c.execute('''INSERT INTO accounts(name,enabled,default_number,created_at,updated_at)
                           VALUES(?,?,?,?,?)''', (name, int(enabled), default_number, now, now))
        return dict(c.execute('SELECT * FROM accounts WHERE id=?', (cur.lastrowid,)).fetchone())


def update_account(account_id: int, **fields) -> dict | None:
    allowed = {'name', 'enabled', 'default_number', 'group_id'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if 'name' in fields:
        fields['name'] = _account_name(fields['name'])
        if not fields['name']:
            raise ValueError('公众号名称不能为空')
    fields['updated_at'] = datetime.now().isoformat(timespec='seconds')
    with _lock, _conn() as c:
        if 'name' in fields:
            target_key = _account_key(fields['name'])
            collision = next((r for r in c.execute('SELECT id,name FROM accounts WHERE id<>? ORDER BY id', (account_id,))
                              if _account_key(r['name']) == target_key), None)
            if collision:
                # Editing a name onto an existing configuration merges the
                # historical links and keeps one canonical account row.
                survivor_id = collision['id']
                c.execute('UPDATE tasks SET account_id=? WHERE account_id=?', (survivor_id, account_id))
                c.execute('UPDATE articles SET account_id=? WHERE account_id=?', (survivor_id, account_id))
                fields.pop('name', None)
                c.execute('DELETE FROM accounts WHERE id=?', (account_id,))
                account_id = survivor_id
        cols = ','.join(f'{k}=?' for k in fields)
        c.execute(f'UPDATE accounts SET {cols} WHERE id=?', (*fields.values(), account_id))
        row = c.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        return dict(row) if row else None


def get_account(account_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        return dict(row) if row else None


def delete_account(account_id: int) -> bool:
    with _lock, _conn() as c:
        return c.execute('DELETE FROM accounts WHERE id=?', (account_id,)).rowcount > 0


def list_groups() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute('SELECT * FROM account_groups ORDER BY sort_order, name COLLATE NOCASE').fetchall()]


def get_group(group_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute('SELECT * FROM account_groups WHERE id=?', (group_id,)).fetchone()
        return dict(row) if row else None


def add_group(name: str) -> dict:
    now = datetime.now().isoformat(timespec='seconds')
    with _lock, _conn() as c:
        cur = c.execute('INSERT INTO account_groups(name,created_at,updated_at) VALUES(?,?,?)', (name, now, now))
        return dict(c.execute('SELECT * FROM account_groups WHERE id=?', (cur.lastrowid,)).fetchone())


def update_group(group_id: int, name: str | None = None, sort_order: int | None = None) -> dict | None:
    fields = {'updated_at': datetime.now().isoformat(timespec='seconds')}
    if name is not None: fields['name'] = name
    if sort_order is not None: fields['sort_order'] = sort_order
    with _lock, _conn() as c:
        cols = ','.join(f'{k}=?' for k in fields)
        c.execute(f'UPDATE account_groups SET {cols} WHERE id=?', (*fields.values(), group_id))
        row = c.execute('SELECT * FROM account_groups WHERE id=?', (group_id,)).fetchone()
        return dict(row) if row else None


def delete_group(group_id: int) -> bool:
    with _lock, _conn() as c:
        c.execute('UPDATE accounts SET group_id=NULL WHERE group_id=?', (group_id,))
        return c.execute('DELETE FROM account_groups WHERE id=?', (group_id,)).rowcount > 0


def set_accounts_group(group_id: int, account_ids: list[int]) -> int:
    with _lock, _conn() as c:
        marks = ','.join('?' for _ in account_ids)
        if account_ids:
            c.execute(f'UPDATE accounts SET group_id=? WHERE id IN ({marks})', [group_id, *account_ids])
        return len(account_ids)


def clear_accounts_group(account_ids: list[int]) -> int:
    with _lock, _conn() as c:
        if not account_ids:
            return 0
        marks = ','.join('?' for _ in account_ids)
        return c.execute(f'UPDATE accounts SET group_id=NULL WHERE id IN ({marks})', account_ids).rowcount


def set_accounts_enabled(account_ids: list[int], enabled: bool) -> int:
    with _lock, _conn() as c:
        if not account_ids: return 0
        marks = ','.join('?' for _ in account_ids)
        return c.execute(f'UPDATE accounts SET enabled=? WHERE id IN ({marks})', [int(enabled), *account_ids]).rowcount


def set_setting(key: str, value: str):
    now = datetime.now().isoformat(timespec='seconds')
    with _lock, _conn() as c:
        c.execute('INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (key, value, now))


def get_settings() -> dict:
    with _conn() as c:
        return {r['key']: r['value'] for r in c.execute('SELECT key,value FROM settings').fetchall()}


def overview() -> dict:
    today = datetime.now().strftime('%Y-%m-%d')
    with _conn() as c:
        return {
            'account_count': c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],
            'article_count': c.execute('SELECT COUNT(*) FROM articles').fetchone()[0],
            'today_article_count': c.execute("SELECT COUNT(*) FROM articles WHERE collected_at LIKE ?", (today + '%',)).fetchone()[0],
            'success_task_count': c.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0],
            'failed_task_count': c.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0],
        }


def overview_details() -> dict:
    """Return only data that is already present in the local database."""
    today = datetime.now().strftime('%Y-%m-%d')
    base = overview()
    with _conn() as c:
        today_done = c.execute("SELECT COUNT(*) FROM tasks WHERE status='done' AND created_at LIKE ?", (today + '%',)).fetchone()[0]
        today_failed = c.execute("SELECT COUNT(*) FROM tasks WHERE status='failed' AND created_at LIKE ?", (today + '%',)).fetchone()[0]
        total_finished = c.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('done','failed')").fetchone()[0]
        success_rate = round(base['success_task_count'] * 100 / total_finished, 1) if total_finished else 0
        queued = c.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
        distribution = [dict(r) for r in c.execute(
            'SELECT account, COUNT(*) AS count FROM articles GROUP BY account ORDER BY count DESC LIMIT 5').fetchall()]
        recent_articles = [dict(r) for r in c.execute(
            'SELECT * FROM articles ORDER BY id DESC LIMIT 5').fetchall()]
        trend = []
        for days_ago in range(6, -1, -1):
            day = (datetime.now().date()).fromordinal(datetime.now().date().toordinal() - days_ago).isoformat()
            trend.append({'date': day, 'count': c.execute(
                'SELECT COUNT(*) FROM articles WHERE collected_at LIKE ?', (day + '%',)).fetchone()[0]})
    return {**base, 'today_done_task_count': today_done, 'today_failed_task_count': today_failed,
            'queued_task_count': queued, 'success_rate': success_rate,
            'distribution': distribution, 'trend': trend,
            'recent_articles': recent_articles}
