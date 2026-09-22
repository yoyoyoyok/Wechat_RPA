# -*- coding: utf-8 -*-
'''公众号文章链接采集控制台 —— 本地 Web 服务

启动：python app.py  然后浏览器打开 http://127.0.0.1:8765
全局热键：Ctrl+Alt+P 暂停/继续，Ctrl+Alt+S 停止当前任务
'''
import keyboard
import re
from datetime import date
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi import HTTPException
from pydantic import BaseModel, Field

try:
    import store
    from engine import engine
except ModuleNotFoundError:  # 支持 python -m collector_webapp.app
    from collector_webapp import store
    from collector_webapp.engine import engine

app = FastAPI(title='公众号文章链接采集台')

STATIC_DIR = Path(__file__).resolve().parent / 'static'
DESIGN_DIR = Path(__file__).resolve().parents[2] / '.design' / 'pages'
_DESIGN_PAGE_CACHE: dict[str, str] = {}


def _remove_task_center_demo_animation(html: str) -> str:
    """Remove the design-only delayed counter animation from task-center."""
    return re.sub(
        r'\s*// Count-up animation for stat numbers.*?(?=\s*// Refresh button feedback)',
        '\n',
        html,
        flags=re.DOTALL,
    )


class TaskIn(BaseModel):
    account: str = Field(min_length=1, description='公众号名称')
    number: int = Field(ge=1, le=200, description='目标篇数')
    remove_favorite: bool = False
    collection_start_date: str | None = None
    collection_end_date: str | None = None
    continue_on_failure: bool = False


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    default_number: int = Field(default=10, ge=1, le=200)
    group_id: int | None = None


class BatchTaskIn(BaseModel):
    accounts: list[str] = Field(min_length=1)
    number: int = Field(default=10, ge=1, le=200)
    remove_favorite: bool = False
    collection_start_date: str | None = None
    collection_end_date: str | None = None
    skip_failed: bool = True


class TaskBatchIn(BatchTaskIn):
    pass


class AccountImportIn(BaseModel):
    accounts: list[dict] = Field(min_length=1)
    enabled: bool = True
    default_number: int = Field(default=10, ge=1, le=200)


# ---------- 页面 ----------

@app.get('/')
def index():
    return RedirectResponse('/accounts.html')


@app.get('/design-adapter.js')
def design_adapter():
    return FileResponse(STATIC_DIR / 'design-adapter.js', media_type='application/javascript',
                        headers={'Cache-Control': 'no-cache, must-revalidate'})


@app.get('/design-shell.css')
def design_shell_css():
    return FileResponse(STATIC_DIR / 'design-shell.css', media_type='text/css',
                        headers={'Cache-Control': 'no-cache, must-revalidate'})


@app.get('/{page_name}.html')
def design_page(page_name: str):
    aliases = {'tasks': 'task-center', 'task-center': 'task-center',
               'accounts': 'accounts', 'articles': 'articles',
               'overview': 'overview', 'settings': 'settings'}
    target = aliases.get(page_name)
    if not target:
        raise HTTPException(status_code=404, detail='页面不存在')
    path = DESIGN_DIR / f'{target}.html'
    if not path.exists():
        raise HTTPException(status_code=404, detail='设计页面不存在')
    html = _DESIGN_PAGE_CACHE.get(target)
    if html is None:
        # Normalize source templates to UTF-8 before injecting them into the SPA.
        # A few design exports contain a legacy BOM/encoding marker; keeping it
        # in the cached fragment can make dynamic navigation render mojibake.
        html = path.read_text(encoding='utf-8-sig')
        if target == 'task-center':
            # The design export contains a delayed demo count-up animation.
            # It runs after design-adapter.js has rendered the database values
            # and can therefore put stale numbers (for example "2 running")
            # back into the live task center.  Keep the real task data as the
            # only source for these counters.
            html = _remove_task_center_demo_animation(html)
        # Set the loading class before the first paint.  The design templates
        # contain demo values and some pages run their own inline animations
        # immediately; waiting for design-adapter.js at the end of <body>
        # causes a visible flash of those values on the overview page.
        shell_bootstrap = '<link rel="stylesheet" href="/design-shell.css"><style id="adapter-bootstrap">html.adapter-loading .content-area,html.adapter-loading .main-content,html.adapter-loading .app-main{visibility:hidden}</style><script>document.documentElement.classList.add("adapter-loading");</script>'
        html = html.replace('</head>', shell_bootstrap + '</head>')
        html = html.replace('</body>', '<script src="/design-adapter.js"></script></body>')
        _DESIGN_PAGE_CACHE[target] = html
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache, must-revalidate'})


# ---------- 任务队列 ----------

@app.post('/api/tasks')
def add_task(t: TaskIn):
    account = t.account.strip()
    if not account:
        raise HTTPException(status_code=422, detail='公众号名称不能为空')
    end_date = t.collection_end_date or (date.today().isoformat() if t.collection_start_date else None)
    task = store.add_task(account, t.number, t.remove_favorite,
                          collection_start_date=t.collection_start_date,
                          collection_end_date=end_date,
                          continue_on_failure=t.continue_on_failure)
    # 新任务是用户明确的继续意图；清理上一次停止/暂停留下的全局队列状态，
    # 避免任务长期停留在 pending 而页面只显示“没反应”。
    engine.control.resume()
    engine.queue_halted = False
    engine.log(f"已加入队列：{task['account']}（{task['number']} 篇）")
    return task


@app.post('/api/tasks/batch')
def add_batch_tasks(t: BatchTaskIn):
    names = list(dict.fromkeys(x.strip() for x in t.accounts if x.strip()))
    if not names:
        raise HTTPException(status_code=422, detail='至少选择一个公众号')
    # Keep the legacy response (a plain task list), while honoring new options.
    tasks = [store.add_task(name, t.number, t.remove_favorite,
                            collection_start_date=t.collection_start_date,
                            collection_end_date=t.collection_end_date,
                            continue_on_failure=False) for name in names]
    engine.control.resume()
    engine.queue_halted = False
    engine.log(f'已加入连续任务：{len(tasks)} 个公众号')
    return tasks


@app.post('/api/task-batches')
def create_task_batch(t: TaskBatchIn):
    names = list(dict.fromkeys(x.strip() for x in t.accounts if x and x.strip()))
    if not names:
        raise HTTPException(status_code=422, detail='至少选择一个公众号')
    end_date = t.collection_end_date or (date.today().isoformat() if t.collection_start_date else None)
    try:
        result = store.create_batch(names, t.number, t.remove_favorite,
                                    t.collection_start_date, end_date, t.skip_failed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    engine.control.resume()
    engine.queue_halted = False
    engine.log(f'已创建批次 #{result["batch_id"]}：{len(names)} 个公众号，队列已恢复')
    return result


@app.get('/api/task-batches/{batch_id}')
def get_task_batch(batch_id: int):
    batch = store.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail='批次不存在')
    current = engine.current if engine.current and engine.current.get('batch_id') == batch_id else None
    if current:
        batch.update({'current_account': current.get('account'),
                      'current_task_id': current.get('id'),
                      'current_phase': current.get('phase'),
                      'current_progress': current.get('progress')})
    return batch


@app.post('/api/task-batches/{batch_id}/retry')
def retry_task_batch(batch_id: int):
    batch = store.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail='批次不存在')
    retried = []
    for task in batch['tasks']:
        if task['status'] in ('failed', 'stopped'):
            item = store.retry_task(task['id'])
            if item:
                retried.append(item)
    if retried:
        engine.queue_halted = False
        store.refresh_batch(batch_id)
        engine.log(f'批次 #{batch_id} 已重试 {len(retried)} 个失败任务')
    return {'batch_id': batch_id, 'retried': retried, 'count': len(retried)}


@app.get('/api/tasks')
def list_tasks(status: str | None = None, account: str | None = None, limit: int = 10000,
               batch_id: int | None = None, collection_start_date: str | None = None,
               collection_end_date: str | None = None):
    return store.query_tasks(status=status or None, account=account or None, limit=limit,
                             batch_id=batch_id, collection_start_date=collection_start_date,
                             collection_end_date=collection_end_date)


@app.get('/api/tasks/stats')
def task_stats(account: str | None = None):
    return store.task_stats(account=account or None)


@app.get('/api/task-center')
def task_center(account: str | None = None):
    """Return the task table and its counters from one consistent request."""
    return {
        'tasks': store.query_tasks(account=account or None, limit=10000),
        'stats': store.task_stats(account=account or None),
        'current': engine.current,
        'queue_halted': engine.queue_halted,
        'paused': engine.control.paused,
    }


@app.post('/api/tasks/{task_id}/retry')
def retry_task(task_id: int):
    task = store.retry_task(task_id)
    if not task:
        raise HTTPException(status_code=400, detail='只有失败或已停止任务可以重试')
    engine.queue_halted = False
    engine.log(f'任务 #{task_id} 已加入重试队列')
    return task


@app.post('/api/tasks/batch-delete')
def batch_delete_tasks(ids: list[int] | None = None):
    return {'deleted': store.delete_tasks(ids=ids or [], finished_only=True)}


@app.post('/api/tasks/clear-finished')
def clear_finished_tasks():
    return {'deleted': store.delete_tasks(finished_only=True)}


@app.delete('/api/tasks/{task_id}')
def delete_task(task_id: int):
    ok = store.delete_task(task_id)
    if not ok:
        return JSONResponse({'error': '只能删除待办/已结束的任务'}, status_code=400)
    return {'ok': True}


# ---------- 控制 ----------

@app.post('/api/pause')
def pause():
    engine.pause()
    return engine.status()


@app.post('/api/resume')
def resume():
    engine.resume()
    return engine.status()


@app.post('/api/stop')
def stop():
    engine.stop_current()
    return engine.status()


@app.get('/api/status')
def status():
    return {
        **engine.status(),
        'logs': engine.recent_logs(),
        'article_count': store.article_count(),
    }


# ---------- 采集结果 ----------

@app.get('/api/articles')
def articles(account: str | None = None, limit: int = 200, offset: int = 0,
             search: str | None = None, page: int = 1, page_size: int = 20,
             paged: bool = False, days: int | None = None,
             date_from: str | None = None, date_to: str | None = None):
    if date_from and not date_to:
        date_to = date.today().isoformat()
    if paged:
        return store.query_articles_page(account=account or None, search=search or None,
                                         page=page, page_size=page_size, days=days,
                                         date_from=date_from, date_to=date_to)
    return store.list_articles(account=account or None, limit=min(limit, 100000), offset=offset,
                                search=search or None, date_from=date_from, date_to=date_to)


@app.get('/api/articles/ids')
def article_ids(account: str | None = None, search: str | None = None,
                days: int | None = None, date_from: str | None = None,
                date_to: str | None = None):
    if date_from and not date_to:
        date_to = date.today().isoformat()
    ids = store.query_article_ids(account=account or None, search=search or None,
                                  days=days, date_from=date_from, date_to=date_to)
    return {'ids': ids, 'total': len(ids)}


@app.delete('/api/articles')
def delete_articles(ids: str | None = None, account: str | None = None):
    parsed = [int(x) for x in (ids or '').split(',') if x.strip().isdigit()]
    return {'deleted': store.delete_articles(parsed or None, account=account or None)}


@app.get('/api/articles/export')
def export_articles(account: str | None = None, date_from: str | None = None,
                    date_to: str | None = None, ids: str | None = None,
                    page: int | None = None, page_size: int | None = None):
    import csv, io
    from fastapi.responses import StreamingResponse
    if date_from and not date_to:
        date_to = date.today().isoformat()
    rows = store.list_articles(account=account or None, limit=100000,
                               date_from=date_from, date_to=date_to)
    selected_ids = {int(value) for value in (ids or '').split(',') if value.strip().isdigit()}
    if selected_ids:
        rows = [row for row in rows if row.get('id') in selected_ids]
    elif page is not None:
        # "当前页" must export the same slice that the article table displays.
        size = max(1, min(int(page_size or 20), 1000))
        current_page = max(1, int(page))
        start = (current_page - 1) * size
        rows = rows[start:start + size]
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['id', 'title', 'url', 'account', 'published_at', 'collected_at'])
    for row in rows:
        writer.writerow([row.get(k, '') for k in ('id', 'title', 'url', 'account', 'published_at', 'collected_at')])
    return StreamingResponse(iter([out.getvalue().encode('utf-8-sig')]),
                             media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename=articles.csv'})


@app.get('/api/articles/stats')
def article_stats():
    return store.overview()


@app.post('/api/articles/copy')
def copy_articles(ids: list[int]):
    rows = store.list_articles(limit=10000)
    selected = [row['url'] for row in rows if row['id'] in ids]
    return {'urls': selected, 'count': len(selected)}


@app.get('/api/accounts')
def list_accounts():
    accounts = store.list_accounts()
    with store._conn() as c:
        for row in accounts:
            row['article_count'] = c.execute('SELECT COUNT(*) FROM articles WHERE account=?', (row['name'],)).fetchone()[0]
            latest = c.execute('SELECT status,finished_at,created_at FROM tasks WHERE account=? ORDER BY id DESC LIMIT 1', (row['name'],)).fetchone()
            row['last_task_status'] = latest['status'] if latest else None
            row['last_collected_at'] = latest['finished_at'] or latest['created_at'] if latest else None
    return accounts


@app.post('/api/accounts')
def add_account(a: AccountIn):
    name = a.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail='公众号名称不能为空')
    # Creating a configuration must never silently turn into an update.  The
    # store uses the normalized account key (including known legacy aliases),
    # so this also catches names that only differ by spacing or case.
    if store.find_account(name):
        raise HTTPException(status_code=409, detail='该公众号已存在')
    row = store.upsert_account(name, a.enabled, a.default_number)
    if a.group_id is not None:
        store.update_account(row['id'], group_id=a.group_id)
        row = store.get_account(row['id'])
    return row


@app.post('/api/accounts/import')
def import_accounts(payload: AccountImportIn):
    """Import exported WeChat account records, using nickname as the stable name."""
    imported, skipped = [], 0
    seen = set()
    for item in payload.accounts:
        name = str(item.get('nickname') or item.get('name') or '').strip()
        if not name or name in seen or store.find_account(name):
            skipped += 1
            continue
        seen.add(name)
        imported.append(store.upsert_account(name, payload.enabled, payload.default_number))
    return {'imported': len(imported), 'skipped': skipped, 'accounts': imported}


@app.patch('/api/accounts/{account_id}')
def update_account(account_id: int, a: AccountIn):
    row = store.update_account(account_id, name=a.name.strip(), enabled=a.enabled,
                               default_number=a.default_number, group_id=a.group_id)
    if not row:
        raise HTTPException(status_code=404, detail='公众号不存在')
    return row


@app.delete('/api/accounts/{account_id}')
def delete_account(account_id: int):
    if not store.delete_account(account_id):
        raise HTTPException(status_code=404, detail='公众号不存在')
    return {'ok': True}


@app.post('/api/accounts/batch-enable')
def batch_enable_accounts(ids: list[int]):
    return {'updated': store.set_accounts_enabled(ids, True)}


@app.post('/api/accounts/batch-disable')
def batch_disable_accounts(ids: list[int]):
    return {'updated': store.set_accounts_enabled(ids, False)}


@app.post('/api/accounts/batch-delete')
def batch_delete_accounts(ids: list[int]):
    deleted = 0
    for account_id in ids:
        deleted += int(store.delete_account(account_id))
    return {'deleted': deleted}


@app.post('/api/accounts/batch-group')
def batch_group_accounts(payload: dict):
    ids = [int(value) for value in (payload.get('ids') or []) if str(value).isdigit()]
    group_id = payload.get('group_id')
    if group_id in ('', None, 'null'):
        return {'updated': store.clear_accounts_group(ids), 'group_id': None}
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail='分组无效')
    if not store.get_group(group_id):
        raise HTTPException(status_code=404, detail='分组不存在')
    return {'updated': store.set_accounts_group(group_id, ids), 'group_id': group_id}


@app.get('/api/account-groups')
def list_groups():
    return store.list_groups()


@app.post('/api/account-groups')
def add_group(payload: dict):
    name = str(payload.get('name', '')).strip()
    if not name:
        raise HTTPException(status_code=422, detail='分组名称不能为空')
    return store.add_group(name)


@app.patch('/api/account-groups/{group_id}')
def update_group(group_id: int, payload: dict):
    return store.update_group(group_id, payload.get('name'), payload.get('sort_order')) or JSONResponse({'error': '分组不存在'}, status_code=404)


@app.delete('/api/account-groups/{group_id}')
def delete_group(group_id: int):
    return {'ok': store.delete_group(group_id)}


@app.post('/api/account-groups/{group_id}/accounts')
def set_group_accounts(group_id: int, ids: list[int]):
    return {'updated': store.set_accounts_group(group_id, ids)}


@app.get('/api/overview')
def overview():
    return {**store.overview_details(), 'current': engine.current, 'queue_halted': engine.queue_halted,
            'paused': engine.control.paused, 'recent_tasks': store.query_tasks(limit=5),
            'recent_articles': store.list_articles(limit=5)}


@app.get('/api/settings')
def get_settings():
    return store.get_settings()


@app.get('/api/backup')
def backup_database():
    from fastapi.responses import FileResponse
    return FileResponse(store.DB_PATH, media_type='application/octet-stream',
                        filename='collector-backup.db')


@app.put('/api/settings')
def put_settings(payload: dict):
    for key, value in payload.items():
        store.set_setting(str(key), str(value))
    return store.get_settings()


# ---------- 启动 ----------

@app.on_event('startup')
def on_startup():
    store.init_db()
    interrupted = store.recover_interrupted_tasks()
    engine.start()
    # 全局热键：微信占用鼠标键盘时也能触发
    try:
        keyboard.add_hotkey('ctrl+alt+p', _toggle_pause)
        keyboard.add_hotkey('ctrl+alt+s', engine.stop_current)
    except Exception as exc:
        # 热键需要 Windows 输入权限；服务本身仍可通过网页控制台运行。
        engine.log(f'全局热键注册失败，网页控制仍可用：{exc}')
    engine.log('服务已启动，全局热键：Ctrl+Alt+P 暂停/继续，Ctrl+Alt+S 停止')
    if interrupted:
        engine.log(f'已将 {interrupted} 个中断任务标记为失败，可在任务中心重试')


@app.on_event('shutdown')
def on_shutdown():
    try:
        keyboard.unhook_all_hotkeys()
    except Exception:
        pass


def _toggle_pause():
    if engine.control.paused:
        engine.resume()
    else:
        engine.pause()


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8765)
