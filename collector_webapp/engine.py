# -*- coding: utf-8 -*-
'''可暂停的公众号文章采集引擎

不修改 pyweixin 源码，采集循环参考其 Collections.collect_offAcc_articles
与 Collections.cardLink_to_url 重写，在每篇文章/每条链接处理前插入暂停检查点。
'''
import time
import threading
import traceback
import re
import unicodedata
from datetime import datetime, date, timedelta
from collections import deque

import pythoncom
import win32clipboard
import pyautogui
from pywinauto import Desktop

import pyweixin.WeChatTools as wechat_tools
from pyweixin.WeChatTools import Navigator
from pyweixin.Uielements import Buttons, ListItems, MenuItems, Lists, Regex_Patterns

try:
    import store
except ModuleNotFoundError:  # 支持从项目根目录以 collector_webapp.engine 导入
    from collector_webapp import store


class TaskStopped(Exception):
    '''用户请求停止当前任务'''


class Control:
    '''暂停/停止控制信号'''

    def __init__(self):
        self.pause_event = threading.Event()   # set = 暂停中
        self.stop_event = threading.Event()    # set = 请求停止当前任务

    def pause(self):
        self.pause_event.set()

    def resume(self):
        self.pause_event.clear()

    @property
    def paused(self):
        return self.pause_event.is_set()

    def stop(self):
        self.stop_event.set()

    def reset_stop(self):
        self.stop_event.clear()

    def checkpoint(self):
        '''检查点：暂停时阻塞等待，收到停止信号抛出 TaskStopped'''
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(0.2)
        if self.stop_event.is_set():
            raise TaskStopped()


class Engine:
    '''单例采集引擎：后台 worker 线程按队列依次执行任务'''

    # UIA 元素的屏幕坐标包含标题栏/边框，靠近窗口底部时屏幕点击很容易
    # 落到微信窗口的缩放区域。保留一段安全边距，宁可多滚动一轮也不冒险点击。
    COLLAPSE_BOTTOM_MARGIN = 56
    COLLAPSE_TOP_MARGIN = 12
    CLICK_EDGE_MARGIN = 72
    COLLECTION_CLICK_BOTTOM_MARGIN = 24
    COLLECTION_SCROLL_CLICKS = -1
    COLLECTION_SCROLL_PERCENT = 5.0
    COLLECTION_SCROLL_SETTLE = 0.12
    COLLECTION_SCROLL_WHEEL_DIST = -6
    COLLECTION_NATIVE_SCROLL_INTERVAL = 0.05
    # The Favorites -> Links pane can keep rendering the previous virtualized
    # rows for a moment after activation. Keep this explicit, fixed settling
    # delay before probing the right-hand list.
    COLLECTION_LINK_LOAD_WAIT = 3.0

    def __init__(self):
        self.control = Control()
        self.queue_halted = False        # 停止任务后置 True，等待用户点继续
        self.current: dict | None = None  # 当前任务（含 phase/progress 动态字段）
        self.logs: deque = deque(maxlen=80)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---------- 对外接口 ----------

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run_loop, daemon=True, name='collector-worker')
        self._worker.start()

    def log(self, msg: str):
        stamp = time.strftime('%H:%M:%S')
        with self._lock:
            self.logs.appendleft(f'[{stamp}] {msg}')

    def recent_logs(self) -> list[str]:
        with self._lock:
            return list(self.logs)

    def status(self) -> dict:
        return {
            'paused': self.control.paused,
            'queue_halted': self.queue_halted,
            'worker_alive': bool(self._worker and self._worker.is_alive()),
            'current': self.current,
        }

    def pause(self):
        self.control.pause()
        self.log('已请求暂停（下一个检查点生效）')

    def resume(self):
        self.control.resume()
        self.queue_halted = False
        self.log('已继续')

    def stop_current(self):
        self.control.stop()
        self.log('已请求停止当前任务（下一个检查点生效）')

    # ---------- worker 主循环 ----------

    def _run_loop(self):
        pythoncom.CoInitialize()
        # pyweixin creates its module-level Desktop wrapper at import time. That
        # wrapper is COM-apartment bound, so a worker thread must replace it
        # after initializing COM or UIA calls fail with a COM subscriber error.
        wechat_tools.desktop = Desktop(backend='uia')
        try:
            while True:
                time.sleep(0.5)
                if self.queue_halted:
                    continue
                task = self._next_pending()
                if not task:
                    continue
                self._execute(task)
        finally:
            pythoncom.CoUninitialize()

    def _next_pending(self) -> dict | None:
        with store._lock, store._conn() as c:
            row = c.execute(
                """SELECT t.* FROM tasks t LEFT JOIN task_batches b ON b.id=t.batch_id
                   WHERE t.status='pending' AND (t.batch_id IS NULL OR b.status NOT IN ('stopped','completed','failed','partial_failed'))
                   ORDER BY t.id LIMIT 1""").fetchone()
            return dict(row) if row else None

    def _execute(self, task: dict):
        self.control.reset_stop()
        self.current = {**task, 'phase': '准备中', 'progress': '0/' + str(task['number'])}
        store.update_task(task['id'], status='collecting', started_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
        if task.get('batch_id'):
            store.refresh_batch(task['batch_id'])
        self.log(f"开始任务 #{task['id']}：{task['account']}，目标 {task['number']} 篇")
        try:
            collected, titles, title_dates = self._collect_phase(task)
            if collected <= 0:
                raise RuntimeError('未找到可收藏的文章，任务未完成；请检查公众号主页、文章标签和 UIA 可见性')
            store.update_task(task['id'], status='extracting', collected=collected)
            self.current['phase'] = '提取链接中'
            extracted = self._extract_phase(task, collected, titles, title_dates)
            if extracted <= 0:
                raise RuntimeError(f'已收藏 {collected} 篇，但未提取到有效链接')
            if extracted < collected:
                self.log(f'警告：收藏 {collected} 篇，仅提取 {extracted} 条有效链接')
            store.update_task(task['id'], status='done', extracted=extracted,
                              finished_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
            self.log(f"任务 #{task['id']} 完成：收藏 {collected} 篇，提取 {extracted} 条链接")
            if task.get('batch_id'):
                store.refresh_batch(task['batch_id'])
        except TaskStopped:
            store.update_task(task['id'], status='stopped',
                              finished_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
            if task.get('batch_id'):
                # Manual stop is a batch-wide stop: leave no pending task runnable.
                with store._lock, store._conn() as c:
                    c.execute("UPDATE tasks SET status='stopped', finished_at=? WHERE batch_id=? AND status='pending'",
                              (time.strftime('%Y-%m-%dT%H:%M:%S'), task['batch_id']))
                store.refresh_batch(task['batch_id'])
            self.queue_halted = True
            self.log(f"任务 #{task['id']} 已被手动停止")
        except Exception as e:
            traceback.print_exc()
            store.update_task(task['id'], status='failed', error=str(e),
                              finished_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
            batch_continue = bool(task.get('batch_id') and task.get('continue_on_failure'))
            if task.get('batch_id'):
                store.refresh_batch(task['batch_id'])
            self.queue_halted = not batch_continue
            self.log(f"任务 #{task['id']} 失败：{e}")
        finally:
            self.current = None

    # ---------- 阶段一：收藏公众号文章 ----------
    # 逻辑参考 pyweixin.Collections.collect_offAcc_articles（未修改源码，重写循环）

    def _collect_phase(self, task: dict) -> tuple[int, list[str], dict[str, str | None]]:
        '''微信4.1.13+适配：公众号主页开在内置浏览器窗口(Chrome_WidgetWin_0)内，
        右键文章标题弹出的菜单为ListItem"收藏"；无障碍树含全量文章，滚动后约2秒刷新。
        Returns: (实际收藏篇数, 已处理文章标题列表)'''
        account, number = task['account'], task['number']
        desktop = Desktop(backend='uia')
        clicked_titles: set = set()
        # 右键菜单偶发未及时出现时不要永久丢弃文章；记录次数后再有限放弃。
        failed_titles: dict[str, int] = {}
        title_dates: dict[str, str | None] = {}
        collected = 0
        stall = 0
        old_date_rounds = 0
        inherited_date: str | None = None
        start_date = task.get('collection_start_date')
        end_date = task.get('collection_end_date') or date.today().isoformat()

        self.log(f'打开公众号会话：{account}')
        self._close_stale_browsers(desktop)
        seperate_window = Navigator.open_seperate_dialog_window(friend=account, close_weixin=False)
        try:
            homepage_button = seperate_window.child_window(**Buttons.HomePageButton)
            # 主页窗口：微信内置浏览器（WeChatAppex宿主），不再是旧版标题"公众号"的Pane
            browser = desktop.window(title='微信', class_name='Chrome_WidgetWin_0')
            tab = browser.child_window(title='文章', control_type='Hyperlink')
            for attempt in range(3):  # 单击打开主页，双击会开→关切换；校验"文章"标签确实出现
                self.log(f'打开公众号主页，第 {attempt + 1}/3 次')
                homepage_button.click_input()
                if tab.exists(timeout=6):
                    break
                time.sleep(2)
            else:
                raise RuntimeError('公众号主页未能打开（内置浏览器内未出现"文章"标签）')
            seperate_window.close()
            try:
                browser.set_focus()
                self._maximize_capture_window(browser, '公众号文章页')
                # 最大化会改变部分版本微信内置浏览器的 UIA 坐标树，最大化后重新
                # 读取所有元素，避免把最大化前的坐标用于点击。
                time.sleep(1)
                # 切换到"文章"标签
                tab = browser.child_window(title='文章', control_type='Hyperlink')
                if tab.exists(timeout=3):
                    tab.click_input()
                    time.sleep(1.5)
                pattern = Regex_Patterns.Article_Timestamp_pattern
                self.current['phase'] = '收藏文章中'
                # With a date range, the end of the range is the completion
                # condition. The UI number is only a progress/reference value
                # and must not stop scanning before older date groups appear.
                while not self._collection_target_reached(collected, number, start_date):
                    self.control.checkpoint()
                    expanded = self._expand_collapsed_article_groups(
                        browser,
                        skipped_groups=set(),
                    )
                    if expanded:
                        self.log(f'展开折叠文章分组：{expanded} 个')
                    wr = browser.rectangle()
                    all_texts = browser.descendants(control_type='Text')
                    titles = [e for e in all_texts
                              if self._is_article_title(e, pattern, wr)]
                    date_anchors = self._date_group_anchors(all_texts)
                    title_dates_by_element, inherited_date, date_labels = self._bind_article_dates(
                        titles, date_anchors, inherited_date)
                    if date_labels:
                        self.log('日期分组：' + '，'.join(
                            f'{label} -> {value}' for label, value in date_labels))
                    self.log(f'文章扫描：文本 {len(all_texts)} 个，过滤通过 {len(titles)} 个，'
                             f'日期分组 {len(date_anchors)} 个，已收藏 {collected}/{number}')
                    fresh = [e for e in titles
                             if e.window_text() not in clicked_titles
                             and failed_titles.get(e.window_text(), 0) < 3]
                    if not fresh:
                        stall += 1
                        if stall >= 5:
                            self.log('已到达文章列表底部或无更多新文章，提前结束收藏')
                            break
                        self._scroll_page(browser)
                        continue
                    stall = 0
                    recent_in_round = 0
                    old_in_round = 0
                    unknown_date_in_round = 0
                    for e in fresh:
                        self.control.checkpoint()
                        # 滚动后元素可能移出视口，跳过等下一轮
                        r = e.rectangle()
                        if not (wr.top < r.top and r.bottom < wr.bottom):
                            continue
                        name = e.window_text()
                        published = title_dates_by_element.get(id(e), inherited_date)
                        if start_date:
                            if not published:
                                unknown_date_in_round += 1
                                self.log(f'发布日期解析失败，按日期任务跳过：{name[:40]}')
                                continue
                            if published > end_date:
                                clicked_titles.add(name)
                                continue
                            if published < start_date:
                                clicked_titles.add(name)
                                old_in_round += 1
                                self.log(f'跳过开始日期之前的文章：{name[:40]}（{published}）')
                                continue
                            recent_in_round += 1
                        # UIA 树可能包含视口外/部分被任务栏遮挡的元素；只有完整
                        # 进入安全区域后才允许右键收藏，否则滚动后重新扫描。
                        if not self._is_safe_click_rect(e.rectangle(), wr):
                            self.log(f'文章卡片靠近窗口边界，先滚动后再收藏：{name[:30]}')
                            continue
                        e.right_click_input()
                        time.sleep(0.5)
                        fav = browser.child_window(title='收藏', control_type='ListItem')
                        if not fav.exists(timeout=2):
                            failed_titles[name] = failed_titles.get(name, 0) + 1
                            self.log(f'右键菜单未出现，第 {failed_titles[name]}/3 次重试：{name[:30]}')
                            continue
                        fav.click_input()
                        clicked_titles.add(name)
                        title_dates[name] = published
                        collected += 1
                        self.current['progress'] = f'{collected}/{number}'
                        time.sleep(0.3)
                        if self._collection_target_reached(collected, number, start_date):
                            break
                    if start_date:
                        self.log(f'日期范围统计：范围内{recent_in_round}篇，早于开始日期{old_in_round}篇，'
                                 f'未识别{unknown_date_in_round}篇')
                        # A pinned group or a virtualized viewport can mix an
                        # old article with current-week articles. Do not stop
                        # at the first old card; require two consecutive full
                        # rounds with no in-range/unknown-date candidates.
                        old_date_rounds, stop_for_date = self._update_old_date_rounds(
                            old_date_rounds, recent_in_round, unknown_date_in_round,
                            old_in_round)
                        if stop_for_date:
                            self.log(f'连续 {old_date_rounds} 轮均早于 {start_date}，停止继续下滑')
                            break
                    self._scroll_page(browser)
                return collected, list(title_dates), title_dates
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        except TaskStopped:
            raise
        except Exception:
            try:
                seperate_window.close()
            except Exception:
                pass
            raise

    def _close_stale_browsers(self, desktop):
        stale = [w for w in desktop.windows(class_name='Chrome_WidgetWin_0')
                 if w.window_text() == '微信']
        if stale:
            self.log(f'清理残留内置浏览器窗口：{len(stale)} 个')
        for old in stale:
            try:
                old.close()
            except Exception as exc:
                self.log(f'关闭残留窗口失败：{exc}')
        if stale:
            time.sleep(1)

    def _maximize_capture_window(self, window, label: str):
        """最大化采集流程自己的窗口，并记录尺寸异常而不中断任务。"""
        try:
            window.set_focus()
        except Exception:
            pass
        try:
            window.maximize()
            time.sleep(0.4)
        except Exception as exc:
            self.log(f'{label}最大化失败，继续使用当前窗口：{exc}')
        try:
            rect = window.rectangle()
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width < 900 or height < 600:
                self.log(f'{label}尺寸偏小（{width}x{height}），折叠条将启用安全区域保护')
            else:
                self.log(f'{label}已准备（{width}x{height}）')
            return rect
        except Exception as exc:
            self.log(f'{label}窗口尺寸读取失败：{exc}')
            return None

    @staticmethod
    def _is_article_title(e, pattern, wr) -> bool:
        '''判断Text元素是否为视口内可见的文章标题：
        文本长度达标、坐标在视口内、且父Group为文章卡片——
        "全部"标签下卡片含"阅读"文本，"文章"标签下卡片含时间戳文本（如"2026年9月18日 16:43"），
        以此排除公众号简介等头部文本'''
        t = e.window_text() or ''
        if len(t) < 10 or pattern.search(t) or '阅读' in t or '内容' in t or '原创' == t:
            return False
        class_name = (e.class_name() or '').lower()
        # 4.1.15 exposes article titles with title/article CSS-backed class
        # names. Unnamed profile/description Text nodes share an ancestor with
        # article cards, so they must not be accepted solely by that ancestor's
        # 阅读 signal.
        if not class_name or ('title' not in class_name and 'article' not in class_name):
            return False
        r = e.rectangle()
        if not (wr.top < r.top and r.bottom < wr.bottom and wr.left < r.left < wr.right):
            return False
        try:
            # 微信不同版本会把标题包在一层或多层 Group 中，检查祖先的全部文本，
            # 但不把整棵浏览器树作为信号，避免把页面头部简介误判为文章卡片。
            parent = e.parent()
            # The nearest Group is the article card. Walking to the Document
            # ancestor would make profile text inherit a sibling article's
            # "阅读" signal and create false positives.
            for _ in range(1):
                if parent is None:
                    break
                children = parent.children(control_type='Text')
                descendants = parent.descendants(control_type='Text')
                texts = [c.window_text() or '' for c in [*children, *descendants]]
                if any('阅读' in ct or pattern.search(ct) for ct in texts):
                    return True
                parent = parent.parent()
        except Exception:
            pass
        return False

    def _expand_collapsed_article_groups(self, browser, skipped_groups=None,
                                         skip_initial_top: bool = False) -> int:
        '''展开当前视口内微信公众号的折叠文章分组。

        微信会把置顶文章和同一天的其余文章分别收进
        ``featured-msg-collapse-bar`` / ``article-list__more-bar``。这些文章
        在折叠状态下不会出现在 UIA 树中，必须先点击展开条，再进行标题扫描。
        每次只点击一个并重新取树，避免前一次展开导致后续元素坐标失效。
        '''
        viewport = browser.rectangle()
        skipped_groups = skipped_groups if skipped_groups is not None else set()
        expanded = 0
        unsafe_rounds = 0
        for _ in range(20):  # 防止异常 UIA 树导致死循环
            target = None
            unsafe_target = None
            for group in browser.descendants(control_type='Group'):
                if not self._is_collapsed_article_group(group):
                    continue
                try:
                    raw = self._normalize_card_text(
                        ' '.join([group.window_text() or ''] + [
                            x.window_text() or '' for x in group.descendants(control_type='Text')]))
                    group_key = self._collapsed_group_key(group, raw)
                    if group_key in skipped_groups:
                        continue
                    # "余下 N 篇" is the historical archive bar and must be
                    # expanded. "N 个内容" is the pinned-content bar shown in
                    # the screenshots; its contents are already represented by
                    # the visible pinned card and should stay collapsed.
                    if self._is_pinned_collapsed_group_text(raw):
                        skipped_groups.add(group_key)
                        self.log(f'跳过置顶折叠组（无需展开）：{raw[:40]}')
                        continue
                    rect = group.rectangle()
                    if rect.bottom <= viewport.top or rect.top >= viewport.bottom:
                        continue
                    if self._is_safe_collapsed_group_rect(rect, viewport):
                        target = group
                        break
                    unsafe_target = rect
                except Exception:
                    continue
            if target is None:
                if unsafe_target is not None:
                    unsafe_rounds += 1
                    self.log(
                        f'折叠条靠近窗口边界，暂不点击（bottom={unsafe_target.bottom}, '
                        f'viewport_bottom={viewport.bottom}），先小步滚动')
                    if unsafe_rounds >= 5:
                        self.log('折叠条连续无法进入安全区域，本轮跳过')
                        break
                    self._scroll_page(browser, amount=-180)
                    viewport = browser.rectangle()
                    continue
                break
            unsafe_rounds = 0
            try:
                target.click_input()
            except Exception:
                # 某些微信版本只有条内 Text 节点支持屏幕点击。
                try:
                    target.descendants(control_type='Text')[0].click_input()
                except Exception:
                    break
            expanded += 1
            time.sleep(0.8)
            viewport = browser.rectangle()
        return expanded

    @staticmethod
    def _collapsed_group_key(group, text='') -> str:
        class_name = ''
        try:
            class_name = group.class_name() or ''
        except Exception:
            pass
        return f'{class_name}\x1f{Engine._normalize_card_text(text)}'

    @staticmethod
    def _is_pinned_collapsed_group_text(text: str) -> bool:
        normalized = Engine._normalize_card_text(text)
        return bool(re.search(r'\d+\s*个内容', normalized) or '置顶' in normalized)

    @staticmethod
    def _update_old_date_rounds(old_rounds: int, recent_count: int,
                                unknown_count: int, old_count: int) -> tuple[int, bool]:
        """Advance the date-boundary guard without stopping on mixed viewports."""
        if recent_count or unknown_count:
            return 0, False
        if old_count:
            next_rounds = old_rounds + 1
            return next_rounds, next_rounds >= 2
        # A round with no date-qualified cards is not an old-date round.  In
        # virtualized WeChat lists this can happen while the viewport is being
        # rebuilt between scrolls; retaining the previous count would turn two
        # non-consecutive old rounds into a false "reached the date boundary".
        return 0, False

    @staticmethod
    def _collection_target_reached(collected: int, number: int,
                                   start_date: str | None) -> bool:
        """Only count-based tasks stop at ``number``; ranged tasks stop by date."""
        return not start_date and collected >= number

    @classmethod
    def _is_initial_pinned_collapsed_rect(cls, rect, viewport) -> bool:
        """Whether a collapsed bar is plausibly the first pinned item.

        The header consumes part of the browser viewport, so use a generous
        upper band rather than requiring the bar to touch the window edge.
        Historical groups lower in the first viewport must still expand.
        """
        if not rect or not viewport:
            return False
        height = max(1, viewport.bottom - viewport.top)
        return (rect.top >= viewport.top + cls.COLLAPSE_TOP_MARGIN and
                rect.top <= viewport.top + int(height * 0.34))

    @classmethod
    def _is_safe_collapsed_group_rect(cls, rect, viewport) -> bool:
        """折叠条必须完整位于内容视口，并远离底部缩放边界。"""
        margin = max(cls.COLLAPSE_BOTTOM_MARGIN,
                     int((viewport.bottom - viewport.top) * 0.06))
        return (rect.top >= viewport.top + cls.COLLAPSE_TOP_MARGIN and
                rect.bottom <= viewport.bottom - margin and
                rect.left >= viewport.left and rect.right <= viewport.right)

    @classmethod
    def _is_safe_click_rect(cls, rect, viewport) -> bool:
        """判断屏幕点击点不会落在窗口边框或屏幕任务栏区域。"""
        if not rect or not viewport:
            return False
        horizontal_margin = 8
        vertical_margin = cls.CLICK_EDGE_MARGIN
        # 列表卡片通常正好贴着列表左右边界，不能使用过大的水平边距；
        # 底部则额外留出任务栏和窗口缩放热区，避免 click_input 落到任务栏。
        return (rect.left >= viewport.left + horizontal_margin and
                rect.right <= viewport.right - horizontal_margin and
                rect.top >= viewport.top + cls.COLLAPSE_TOP_MARGIN and
                rect.bottom <= viewport.bottom - vertical_margin)

    @staticmethod
    def _is_collapsed_article_group(group) -> bool:
        class_name = (group.class_name() or '').lower()
        if class_name not in {'featured-msg-collapse-bar', 'article-list__more-bar'}:
            return False
        try:
            texts = [x.window_text() or '' for x in group.descendants(control_type='Text')]
        except Exception:
            texts = []
        return any(re.search(r'(\d+\s*个内容|余下\s*\d+\s*篇)', text) for text in texts)

    @staticmethod
    def _scroll_page(browser, amount: int = -300):
        '''小步滚轮向下滚动，保留相邻卡片重叠，避免跳过文章。'''
        r = browser.rectangle()
        # 内容区中心在当前微信版本最稳定；滚动量约四分之一屏，保留
        # 相邻文章重叠，避免一次跳过日期分组或折叠条。
        pyautogui.moveTo((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        pyautogui.scroll(amount)
        time.sleep(2.0)

    # ---------- 阶段二：从收藏提取链接 ----------
    # 逻辑参考 pyweixin.Collections.cardLink_to_url（未修改源码，重写循环）

    def _extract_phase(self, task: dict, collected: int, titles: list[str] | None = None,
                       title_dates: dict[str, str | None] | None = None) -> int:
        """Extract links using an explicit monotonic traversal strategy."""
        if collected <= 0:
            return 0
        account = task['account']
        pattern = Regex_Patterns.Article_Timestamp_pattern
        main_window = Navigator.open_collections(is_maximize=True)
        self._maximize_capture_window(main_window, '收藏窗口')
        try:
            link_item, link_list = self._open_collection_link_list(main_window)
            if link_item is None or link_list is None:
                return 0
            copylink_item = main_window.child_window(**MenuItems.CopyLinkMenuItem)
            delete_item = main_window.child_window(**MenuItems.DeleteMenuItem)
            delete_button = main_window.child_window(**Buttons.DeleteButton)
            # The category may preserve its previous virtual-list offset.  The
            # helper has already selected "Links", waited for fav_detail_list,
            # and reset that newly bound list before any card is inspected.
            if task.get('remove_favorite'):
                self.log(f'提取链接：删除模式，按顶部顺序处理 {collected} 张卡片')
                return self._extract_delete_top(
                    task, collected, account, pattern, titles or [], title_dates or {},
                    main_window, copylink_item, delete_item, delete_button,
                )
            self.log(f'提取链接：普通模式，严格按视觉顺序处理 {collected} 张卡片')
            return self._extract_sequential(
                task, collected, account, pattern, titles or [], title_dates or {},
                main_window, copylink_item,
            )
        finally:
            try:
                main_window.close()
            except Exception:
                pass

    def _open_collection_link_list(self, main_window):
        """Open Favorites -> Links and return freshly bound UIA wrappers.

        WeChat keeps the last selected Favorites category and its virtualized
        scroll offset.  A stale ``LinkListItem`` wrapper can therefore exist
        while the right pane still shows the previous view.  Explicitly click
        the category, wait for ``fav_detail_list`` to be present, then reset
        the newly bound pane to the top before handing it to extraction.
        """
        link_item = main_window.child_window(**ListItems.LinkListItem)
        try:
            if not link_item.exists(timeout=2):
                self.log('收藏界面内没有“链接”分类')
                return None, None
            # A single click can only focus the sidebar row on some WeChat
            # builds. Double-click when supported, with a single-click
            # fallback for wrappers that expose only the basic input method.
            activate = getattr(link_item, 'double_click_input', None)
            if activate is None:
                activate = link_item.click_input
            activate()
            time.sleep(self.COLLECTION_LINK_LOAD_WAIT)
        except Exception as exc:
            self.log(f'打开收藏“链接”分类失败：{exc}')
            return None, None

        link_list = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self.control.checkpoint()
            candidate = main_window.child_window(**Lists.CollectionRightList)
            try:
                if candidate.exists(timeout=0.2):
                    link_list = candidate
                    # Give the virtualized rows a short chance to materialize.
                    if self._collection_items(candidate):
                        break
            except Exception:
                pass
            time.sleep(0.15)
        if link_list is None:
            self.log('收藏“链接”分类已点击，但右侧列表未加载')
            return None, None

        self.log('已打开收藏“链接”分类，准备回到列表顶部')
        self._reset_collection_to_top(main_window, link_list=link_list)
        # HOME can rebuild the virtualized list. Never pass the pre-reset
        # wrapper into the extraction loop.
        rebound = main_window.child_window(**Lists.CollectionRightList)
        try:
            if not rebound.exists(timeout=1):
                self.log('收藏链接列表回到顶部后控件消失')
                return None, None
        except Exception:
            return None, None
        self.log('收藏链接列表已确认打开并从顶部开始扫描')
        return link_item, rebound

    def _reset_collection_to_top(self, main_window, link_list=None):
        """Bind the right pane after opening Favorites and reset its scroll.

        WeChat preserves the last Favorites scroll offset between openings.
        The extraction cursor is defined from the top, so the first scan must
        always explicitly return there and wait for the virtualized rows to
        rebuild before reading any ListItem wrappers.
        """
        time.sleep(0.5)
        link_list = link_list or main_window.child_window(**Lists.CollectionRightList)
        try:
            link_list.set_focus()
        except Exception:
            pass
        try:
            link_list.type_keys('{HOME}')
        except Exception:
            pass
        # HOME is delivered through keyboard focus and can be swallowed by
        # the host window without raising. When the list exposes ScrollPattern,
        # explicitly set the vertical position as the authoritative reset.
        try:
            scroll = getattr(link_list, 'iface_scroll', None)
            if scroll is not None:
                scroll.SetScrollPercent(verticalPercent=0.0, horizontalPercent=-1)
        except Exception:
            pass
        # Rebind after HOME because WeChat replaces virtualized ListItem
        # wrappers while it repaints the pane.
        time.sleep(0.5)
        rebound = main_window.child_window(**Lists.CollectionRightList)
        try:
            position = self._collection_scroll_position(rebound)
            if position is not None and position > 1.0:
                self.log(f'收藏列表顶部复位未生效（位置={position:.1f}%），再次使用 UIA 顶部定位')
                scroll = getattr(rebound, 'iface_scroll', None)
                if scroll is not None:
                    scroll.SetScrollPercent(verticalPercent=0.0, horizontalPercent=-1)
                    time.sleep(0.35)
        except Exception:
            pass
        self.log('收藏列表已打开并回到顶部，开始重新扫描')
        return rebound

    def _extract_sequential(self, task, collected, account, pattern, titles,
                            title_dates, main_window, copylink_item) -> int:
        """Process visible cards top-to-bottom; scroll only after the pane is drained."""
        extracted = 0
        seen_urls: set[str] = set()
        processed: set[str] = set()
        ignored: set[str] = set()
        failed_attempts: dict[str, int] = {}
        scanned = 0
        duplicate_urls = 0
        scroll_count = 0
        no_candidate_rounds = 0
        last_stall_key = None
        stop_reason = '达到目标'
        while extracted < collected:
            self.control.checkpoint()
            link_list = main_window.child_window(**Lists.CollectionRightList)
            items = self._collection_items_sorted(link_list)
            list_viewport = self._safe_rectangle(link_list)
            window_viewport = self._safe_rectangle(main_window)
            viewport = list_viewport or window_viewport
            candidate = None
            candidate_title = ''
            candidate_fp = ''
            visible_fps = []
            for item in items:
                raw_text = item.window_text() or ''
                if not self._is_item_in_viewport(item, viewport):
                    continue
                title, matched = self._clean_card_title(raw_text, pattern, account, titles)
                fp = self._card_fingerprint(raw_text, title)
                visible_fps.append(fp)
                if fp in processed or fp in ignored:
                    continue
                if titles and not matched:
                    ignored.add(fp)
                    scanned += 1
                    self.log(f'跳过其他公众号收藏卡片：{raw_text[:50]}')
                    continue
                try:
                    item_rect = item.rectangle()
                except Exception:
                    item_rect = None
                if self._is_safe_collection_click_rect(item_rect, viewport):
                    candidate, candidate_title, candidate_fp = item, title, fp
                    break

            if candidate is None:
                if not items:
                    stop_reason = '收藏列表为空或未加载'
                    break
                stall_key = (tuple(visible_fps), self._collection_scroll_position(link_list))
                no_candidate_rounds = no_candidate_rounds + 1 if stall_key == last_stall_key else 0
                last_stall_key = stall_key
                if no_candidate_rounds >= 5:
                    stop_reason = '连续滚动后没有新的可处理卡片'
                    break
                if not self._scroll_collection_list(link_list):
                    stop_reason = '收藏列表无法继续滚动'
                    break
                scroll_count += 1
                self.log(f'普通模式滚动 #{scroll_count}：视口卡片={len(visible_fps)}')
                continue

            no_candidate_rounds = 0
            last_stall_key = None
            scanned += 1
            try:
                item_rect = candidate.rectangle()
            except Exception:
                item_rect = None
            if not self._is_safe_collection_click_rect(item_rect, window_viewport or viewport):
                # Rebind on the next pass; never scroll because one stale
                # wrapper moved between selection and action.
                continue
            url = self._copy_with_retries(candidate, copylink_item, candidate_title, failed_attempts, candidate_fp)
            if not url:
                if failed_attempts.get(candidate_fp, 0) >= 3:
                    processed.add(candidate_fp)
                continue
            processed.add(candidate_fp)
            if url in seen_urls:
                duplicate_urls += 1
                self.log(f'任务内重复链接，跳过计数：{candidate_title[:40]}')
            else:
                seen_urls.add(url)
                is_new = store.insert_article(
                    url, candidate_title, account, task_id=task.get('id'),
                    published_at=title_dates.get(candidate_title))
                extracted += 1
                self.current['progress'] = f'{extracted}/{collected}'
                self.log(('新链接：' if is_new else '已存在，仍计入本次处理：') + candidate_title[:40])
            if extracted >= collected:
                stop_reason = '达到目标'
                break
        self.log(f'提取结束：模式=普通，收藏{collected}篇，复制{extracted}条，滚动{scroll_count}次，'
                 f'重复{duplicate_urls}条，扫描{scanned}项，原因：{stop_reason}')
        return extracted

    def _extract_delete_top(self, task, collected, account, pattern, titles,
                            title_dates, main_window, copylink_item, delete_item,
                            delete_button) -> int:
        """Process the first target card repeatedly; deletion moves the next one up."""
        extracted = 0
        handled_cards = 0
        seen_urls: set[str] = set()
        processed: set[str] = set()
        ignored: set[str] = set()
        failed_attempts: dict[str, int] = {}
        removed = 0
        duplicate_urls = 0
        scanned = 0
        stop_reason = '达到目标'
        while handled_cards < collected:
            self.control.checkpoint()
            link_list = main_window.child_window(**Lists.CollectionRightList)
            try:
                link_list.type_keys('{HOME}')
            except Exception:
                pass
            items = self._collection_items_sorted(link_list)
            list_viewport = self._safe_rectangle(link_list)
            window_viewport = self._safe_rectangle(main_window)
            candidate = None
            candidate_title = ''
            candidate_fp = ''
            for item in items:
                raw_text = item.window_text() or ''
                if not self._is_item_in_viewport(item, list_viewport or window_viewport):
                    continue
                title, matched = self._clean_card_title(raw_text, pattern, account, titles)
                fp = self._card_fingerprint(raw_text, title)
                if fp in processed or fp in ignored:
                    continue
                if titles and not matched:
                    ignored.add(fp)
                    scanned += 1
                    continue
                if self._is_safe_collection_click_rect(item.rectangle(), window_viewport or list_viewport):
                    candidate, candidate_title, candidate_fp = item, title, fp
                    break
            if candidate is None:
                stop_reason = '删除模式没有可见的目标收藏卡片，不滚动'
                break
            scanned += 1
            url = self._copy_with_retries(candidate, copylink_item, candidate_title, failed_attempts, candidate_fp)
            if not url:
                stop_reason = '复制失败，删除模式停止'
                break
            if not delete_favorite_card(candidate, delete_item, delete_button, viewport=window_viewport):
                stop_reason = '删除失败，保留当前收藏卡片'
                self.log(f'复制成功但删除失败：{candidate_title[:40]}')
                break
            processed.add(candidate_fp)
            handled_cards += 1
            removed += 1
            if url in seen_urls:
                duplicate_urls += 1
                self.log(f'删除模式重复链接，已处理并删除：{candidate_title[:40]}')
            else:
                seen_urls.add(url)
                is_new = store.insert_article(
                    url, candidate_title, account, task_id=task.get('id'),
                    published_at=title_dates.get(candidate_title))
                extracted += 1
                self.current['progress'] = f'{handled_cards}/{collected}'
                self.log(('新链接：' if is_new else '已存在，仍计入本次处理：') + candidate_title[:40])
            if handled_cards >= collected:
                break
            time.sleep(0.35)
        self.log(f'提取结束：模式=删除，收藏{collected}篇，复制{extracted}条，处理{handled_cards}张，'
                 f'删除{removed}条，重复{duplicate_urls}条，扫描{scanned}项，原因：{stop_reason}')
        return extracted

    def _copy_with_retries(self, candidate, copylink_item, title, failed_attempts, fingerprint) -> str:
        for attempt in range(3):
            try:
                url = copy_link_url(candidate, copylink_item)
                if url:
                    return url
            except Exception as exc:
                if attempt == 2:
                    self.log(f'复制链接失败：{title[:40]}（{exc}）')
                else:
                    time.sleep(0.25)
        failed_attempts[fingerprint] = failed_attempts.get(fingerprint, 0) + 1
        return ''

    @staticmethod
    def _safe_rectangle(element):
        try:
            return element.rectangle()
        except Exception:
            return None

    @staticmethod
    def _collection_items_sorted(link_list) -> list:
        items = Engine._collection_items(link_list)
        def top(item):
            try:
                return (item.rectangle().top, item.rectangle().left)
            except Exception:
                return (10**9, 10**9)
        return sorted(items, key=top)

    def _extract_phase_legacy(self, task: dict, collected: int, titles: list[str] | None = None,
                       title_dates: dict[str, str | None] | None = None) -> int:
        if collected <= 0:
            return 0
        account = task['account']
        pattern = Regex_Patterns.Article_Timestamp_pattern
        main_window = Navigator.open_collections(is_maximize=True)
        self._maximize_capture_window(main_window, '收藏窗口')
        try:
            link_item = main_window.child_window(**ListItems.LinkListItem)
            if not link_item.exists(timeout=0.5):
                self.log('收藏界面内没有卡片链接')
                return 0
            copylink_item = main_window.child_window(**MenuItems.CopyLinkMenuItem)
            delete_item = main_window.child_window(**MenuItems.DeleteMenuItem)
            delete_button = main_window.child_window(**Buttons.DeleteButton)
            link_item.double_click_input()
            time.sleep(0.5)
            extracted = 0
            seen_urls: set[str] = set()
            processed_fingerprints: set[str] = set()
            ignored_fingerprints: set[str] = set()
            failed_attempts: dict[str, int] = {}
            scanned = 0
            no_progress_rounds = 0
            max_rounds = max(collected * 12, collected + 30)
            removed = 0
            duplicate_urls = 0
            stop_reason = '达到目标'
            LinkList = None
            reset_to_top = True
            last_stall_key = None
            unchanged_scrolls = 0
            while extracted < collected and no_progress_rounds < max_rounds:
                self.control.checkpoint()
                LinkList = main_window.child_window(**Lists.CollectionRightList)
                if reset_to_top:
                    try:
                        LinkList.type_keys('{HOME}')
                    except Exception:
                        pass
                    reset_to_top = False
                items = self._collection_items(LinkList)
                snapshot = self._collection_snapshot(items)
                scroll_position = self._collection_scroll_position(LinkList)
                try:
                    # The outer window is useful for taskbar protection, but
                    # visibility must be evaluated against the actual right
                    # collection pane.  The old outer-window check treated
                    # only the first loaded page as visible.
                    list_viewport = LinkList.rectangle()
                except Exception:
                    list_viewport = None
                try:
                    viewport = main_window.rectangle()
                except Exception:
                    viewport = list_viewport
                candidate = None
                candidate_title = ''
                candidate_fp = ''
                for item in items:
                    raw_text = item.window_text() or ''
                    # UIA can expose cards that are already loaded but outside
                    # the current viewport.  Never select one of those as the
                    # next candidate: doing so makes every round scroll toward
                    # an off-screen element and skips visible cards.
                    if not self._is_item_in_viewport(item, list_viewport or viewport):
                        continue
                    title, matched = self._clean_card_title(raw_text, pattern, account, titles or [])
                    fp = self._card_fingerprint(raw_text, title)
                    if fp in processed_fingerprints or fp in ignored_fingerprints:
                        continue
                    if titles and not matched:
                        ignored_fingerprints.add(fp)
                        scanned += 1
                        self.log(f'跳过其他公众号收藏卡片：{raw_text[:50]}')
                        continue
                    # Prefer a card that can be clicked immediately.  The old
                    # code selected the first match and only then checked its
                    # rectangle, so one bottom-edge card caused a scroll even
                    # when the next visible card was already safe.
                    try:
                        item_rect = item.rectangle()
                    except Exception:
                        # UIA elements can disappear while WeChat refreshes
                        # the list.  Treat that card as temporarily unsafe and
                        # let the next scan bind a fresh element.
                        item_rect = None
                    # Visibility belongs to the right pane, but the click
                    # safety boundary must be the real window.  The List
                    # wrapper's rectangle can move with its virtual content;
                    # using it for safety rejects every card after a scroll.
                    if self._is_safe_collection_click_rect(item_rect, window_viewport or viewport):
                        candidate, candidate_title, candidate_fp = item, title, fp
                        break

                if candidate is None:
                    if not items:
                        stop_reason = '收藏列表为空或未加载'
                        break
                    if extracted >= collected:
                        stop_reason = '达到目标'
                        break
                    stall_key = (snapshot, scroll_position)
                    if stall_key == last_stall_key:
                        unchanged_scrolls += 1
                    else:
                        unchanged_scrolls = 0
                    last_stall_key = stall_key
                    # The first scan after a scroll can still expose the
                    # previous UIA tree. Allow a few refreshes, but stop once
                    # both card positions and the scroll provider are stable.
                    if scroll_position is not None and scroll_position >= 99.9:
                        stop_reason = '收藏列表滚动位置已到 100%'
                        break
                    if unchanged_scrolls >= 10:
                        stop_reason = '列表滚动后无变化，疑似到达底部'
                        break
                    no_progress_rounds += 1
                    if no_progress_rounds >= max_rounds:
                        stop_reason = '连续扫描无进展，疑似到达底部'
                        break
                    moved = self._scroll_collection_list(LinkList)
                    self.log(f'收藏列表继续滚动：位置={scroll_position!r}，结果={moved}')
                    continue

                no_progress_rounds = 0
                last_stall_key = None
                unchanged_scrolls = 0
                scanned += 1
                self.control.checkpoint()
                try:
                    # The List control's UIA rectangle may describe the
                    # scroll content rather than the actual window client
                    # area.  Use the maximized capture window as the outer
                    # safety boundary so valid cards are not discarded as
                    # permanently bottom-aligned.
                    current_rect = main_window.rectangle()
                    item_rect = candidate.rectangle()
                except Exception:
                    current_rect = None
                    item_rect = None
                if not self._is_safe_collection_click_rect(item_rect, current_rect):
                    self.log(f'收藏卡片位置已变化，重新扫描：{candidate_title[:40]}')
                    continue
                url = ''
                for attempt in range(3):
                    try:
                        url = copy_link_url(candidate, copylink_item)
                    except Exception as exc:
                        if attempt == 2:
                            self.log(f'复制链接失败，保留卡片待后续处理：{candidate_title[:40]}（{exc}）')
                        else:
                            time.sleep(0.5)
                    if url:
                        break
                if not url:
                    failed_attempts[candidate_fp] = failed_attempts.get(candidate_fp, 0) + 1
                    if failed_attempts[candidate_fp] >= 3:
                        processed_fingerprints.add(candidate_fp)
                        self.log(f'复制失败达到重试上限，跳过卡片：{candidate_title[:40]}')
                    continue

                processed_fingerprints.add(candidate_fp)
                if url in seen_urls:
                    duplicate_urls += 1
                    self.log(f'任务内重复链接，跳过计数：{candidate_title[:40]}')
                else:
                    seen_urls.add(url)
                    is_new = store.insert_article(
                        url, candidate_title, account, task_id=task.get('id'),
                        published_at=(title_dates or {}).get(candidate_title))
                    extracted += 1
                    self.current['progress'] = f'{extracted}/{collected}'
                    self.log(('新链接：' if is_new else '已存在，仍计入本次处理：') + candidate_title[:40])

                if task.get('remove_favorite'):
                    try:
                        if delete_favorite_card(candidate, delete_item, delete_button,
                                                 viewport=current_rect):
                            removed += 1
                            # 删除会使后续卡片前移；下一轮从顶部重建快照，不能复用旧元素。
                            time.sleep(0.5)
                            if extracted >= collected:
                                stop_reason = '达到目标，删除后立即结束'
                                break
                            reset_to_top = True
                            continue
                        self.log(f'复制成功但删除失败，保留收藏卡片：{candidate_title[:40]}')
                    except Exception as exc:
                        self.log(f'复制成功但删除失败，保留收藏卡片：{candidate_title[:40]}（{exc}）')

                if extracted >= collected:
                    stop_reason = '达到目标'
                    break

                # 非删除模式下，本轮候选已处理；下一轮仍会扫描当前快照，只有没有
                # 未处理候选时才向下滚动，避免固定 DOWN 导致第 6 -> 第 8 跳项。
                time.sleep(0.15)
            self.log(f'提取结束：收藏{collected}篇，复制{extracted}条，删除{removed}条，'
                     f'重复{duplicate_urls}条，扫描{scanned}项，原因：{stop_reason}')
            return extracted
        finally:
            try:
                main_window.close()
            except Exception:
                pass

    @staticmethod
    def _collection_items(link_list) -> list:
        """返回当前收藏列表快照；每次调用都重新获取子项对象。"""
        try:
            return [item for item in link_list.children(control_type='ListItem')
                    if (item.window_text() or '').strip()]
        except Exception:
            return []

    @classmethod
    def _collection_snapshot(cls, items) -> tuple:
        """Snapshot text and screen positions, not text alone.

        WeChat often reuses the same ListItem text nodes while scrolling. A
        text-only snapshot therefore falsely reports the bottom after the
        first viewport (typically six cards).
        """
        result = []
        for item in items:
            text = item.window_text() or ''
            rect_key = None
            try:
                rect = item.rectangle()
                rect_key = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            except Exception:
                pass
            result.append((cls._card_fingerprint(text, ''), rect_key))
        return tuple(result)

    @staticmethod
    def _collection_scroll_position(link_list):
        """Read the UIA scroll position when the provider exposes it."""
        try:
            value = float(link_list.iface_scroll.CurrentVerticalScrollPercent)
            return round(value, 3) if value >= 0 else None
        except Exception:
            return None

    @classmethod
    def _card_fingerprint(cls, raw_text: str, title: str) -> str:
        return f'{cls._normalize_card_text(raw_text)}\x1f{cls._normalize_card_text(title)}'

    @staticmethod
    def _is_item_in_viewport(item, viewport) -> bool:
        """Return whether a card has a visible intersection with the window."""
        if viewport is None:
            return True
        try:
            rect = item.rectangle()
            return (rect.bottom > viewport.top and rect.top < viewport.bottom and
                    rect.right > viewport.left and rect.left < viewport.right)
        except Exception:
            return False

    @classmethod
    def _is_safe_collection_click_rect(cls, rect, viewport) -> bool:
        """Check the point ``right_click_input`` will use, not full card bounds.

        WeChat often reports a ListItem rectangle taller than the visible
        portion of a card. Requiring the whole rectangle to fit made every
        bottom card look unsafe and caused a long pre-copy scroll. The click
        lands near the element centre, so protect that point instead.
        """
        if not rect or not viewport:
            return False
        try:
            center_x = (rect.left + rect.right) // 2
            center_y = (rect.top + rect.bottom) // 2
            return (viewport.left + 8 <= center_x <= viewport.right - 8 and
                    viewport.top + cls.COLLAPSE_TOP_MARGIN <= center_y <=
                    viewport.bottom - cls.COLLECTION_CLICK_BOTTOM_MARGIN)
        except Exception:
            return False

    @staticmethod
    def _scroll_collection_list(link_list) -> bool:
        """在收藏列表视口内小步滚动，不用 Down 跳过当前卡片。

        ``{DOWN}`` changes the focused ListItem in WeChat rather than moving
        the viewport reliably.  When a card is near the bottom safety zone it
        therefore advances past that card, which was the source of the
        observed 6 -> 8 skips.  A wheel event at the list centre moves the
        viewport while preserving the current visible-card overlap.
        """
        try:
            # Send the wheel event through the actual ListItem wrapper first.
            # This targets WeChat's virtualized right pane directly; calling
            # ScrollPattern/Page on this control can report success without
            # moving the rendered list in some WeChat builds.
            wheel = getattr(link_list, 'wheel_mouse_input', None)
            if wheel is not None:
                wheel(wheel_dist=Engine.COLLECTION_SCROLL_WHEEL_DIST)
                time.sleep(0.25)
                return True
        except Exception:
            pass
        try:
            # pywinauto's page scroll moves WeChat's virtualized List control
            # reliably. It advances one viewport at a time, which is the
            # useful middle ground here: substantially faster than line-by-
            # line scrolling while the next scan rebinds every card.
            native_scroll = getattr(link_list, 'scroll', None)
            if native_scroll is not None:
                try:
                    link_list.set_focus()
                except Exception:
                    pass
                native_scroll(
                    'down', 'page',
                    count=1,
                    retry_interval=Engine.COLLECTION_NATIVE_SCROLL_INTERVAL,
                )
                time.sleep(0.25)
                return True
        except Exception:
            pass
        try:
            scroll = getattr(link_list, 'iface_scroll', None)
            if scroll is not None:
                current = float(scroll.CurrentVerticalScrollPercent)
                if current >= 0:
                    next_pos = min(100.0, current + 8.0)
                    scroll.SetScrollPercent(verticalPercent=next_pos, horizontalPercent=-1)
                    time.sleep(Engine.COLLECTION_SCROLL_SETTLE)
                    return True
        except Exception:
            pass
        try:
            rect = link_list.rectangle()
            center_x = (rect.left + rect.right) // 2
            center_y = (rect.top + rect.bottom) // 2
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return False
            pyautogui.moveTo(center_x, center_y)
            # pyautogui uses wheel *clicks*, not pixels. Keep a few-click
            # overlap while moving quickly to the next batch of cards.
            pyautogui.scroll(Engine.COLLECTION_SCROLL_CLICKS)
        except Exception:
            return False
        time.sleep(Engine.COLLECTION_SCROLL_SETTLE)
        return True

    @staticmethod
    def _clean_card_title(card_text: str, pattern, account: str, titles: list[str]) -> tuple[str, bool]:
        '''清洗收藏卡片文本得到文章标题：
        去掉"链接"前缀与时间戳后，卡片文本 = 标题 + 收藏者昵称 + 公众号名。
        优先用收藏阶段记录的标题做前缀匹配；否则去掉公众号名后缀兜底。'''
        raw = Engine._normalize_card_text(pattern.sub('', (card_text or '')[2:]))
        if titles:
            for t in titles:
                normalized_title = Engine._normalize_card_text(t)
                if raw.startswith(normalized_title):
                    return t, True
        normalized_account = Engine._normalize_card_text(account)
        if normalized_account and raw.endswith(normalized_account):
            raw = raw[:-len(normalized_account)]
        return raw, False

    @staticmethod
    def _normalize_card_text(value: str) -> str:
        """统一全角/不可见字符及连续空白，稳定匹配收藏卡片标题。"""
        value = unicodedata.normalize('NFKC', value or '')
        value = ''.join(ch for ch in value if unicodedata.category(ch) not in {'Cf', 'Cc'})
        return re.sub(r'\s+', ' ', value).strip()

    @classmethod
    def _date_group_label(cls, text: str) -> str | None:
        """Return an exact WeChat date-group label, excluding card text."""
        value = cls._normalize_card_text(text)
        if value in {'今天', '昨日', '昨天', '前天'}:
            return value
        if re.fullmatch(r'(?:星期|周)[一二三四五六日天]', value):
            return value
        if re.fullmatch(r'(?:\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2})', value):
            return value
        return None

    @classmethod
    def _date_group_anchors(cls, elements) -> list[tuple[int, str, str]]:
        """Find visible date separators and sort them by screen position."""
        anchors = []
        for element in elements:
            label = cls._date_group_label(element.window_text() or '')
            if not label:
                continue
            try:
                rect = element.rectangle()
                parsed = cls._parse_article_date(label)
                if parsed:
                    anchors.append((int(rect.top), label, parsed))
            except Exception:
                continue
        anchors.sort(key=lambda item: item[0])
        return anchors

    @classmethod
    def _bind_article_dates(cls, titles, anchors, inherited_date=None):
        """Bind each title to the nearest date separator above it."""
        bound = {}
        labels = []
        current = inherited_date
        seen_labels = set()
        for _, label, parsed in anchors:
            current = parsed
            if (label, parsed) not in seen_labels:
                labels.append((label, parsed))
                seen_labels.add((label, parsed))
        for title in sorted(titles, key=lambda item: getattr(item.rectangle(), 'top', 10**9)):
            try:
                top = title.rectangle().top
            except Exception:
                bound[id(title)] = current
                continue
            for anchor_top, _, parsed in anchors:
                if anchor_top <= top:
                    current = parsed
                else:
                    break
            bound[id(title)] = current
        return bound, current, labels

    @staticmethod
    def _parse_article_date(text: str, now=None) -> str | None:
        """Parse the date formats currently rendered by WeChat article cards."""
        if not text:
            return None
        current = now or datetime.now()
        text = unicodedata.normalize('NFKC', str(text))
        normalized = re.sub(r'\s+', '', text)
        if normalized == '今天':
            return current.date().isoformat()
        if normalized in {'昨天', '昨日'}:
            return (current.date() - timedelta(days=1)).isoformat()
        if normalized == '前天':
            return (current.date() - timedelta(days=2)).isoformat()
        weekday_match = re.fullmatch(r'(?:星期|周)([一二三四五六日天])', normalized)
        if weekday_match:
            weekday_index = {'一': 0, '二': 1, '三': 2, '四': 3,
                             '五': 4, '六': 5, '日': 6, '天': 6}[weekday_match.group(1)]
            delta = (current.weekday() - weekday_index) % 7
            if delta == 0:
                delta = 7
            return (current.date() - timedelta(days=delta)).isoformat()
        match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', text)
        if not match:
            match = re.search(r'(\d{4})[-/]\s*(\d{1,2})[-/]\s*(\d{1,2})', text)
        if not match:
            match = re.search(r'(?<!\d)(\d{1,2})月\s*(\d{1,2})日', text)
            if match:
                year, month, day = current.year, int(match.group(1)), int(match.group(2))
                if date(year, month, day) > current.date():
                    year -= 1
            else:
                return None
        else:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

# ---------- 底层操作 ----------

def copy_link_url(link_listitem, copylink_item, delete_item=None, delete_button=None) -> str:
    '''右键卡片链接→复制链接→读剪贴板'''
    for attempt in range(3):
        try:
            link_listitem.right_click_input()
            copylink_item.click_input()
            time.sleep(0.35)
            win32clipboard.OpenClipboard()
            try:
                url = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ''
            finally:
                win32clipboard.CloseClipboard()
            if re.match(r'^https?://', url, re.I):
                return url
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.4)
    return ''


def delete_favorite_card(link_listitem, delete_item, delete_button, viewport=None) -> bool:
    """删除已成功复制的收藏卡片；失败时返回 False，不吞掉业务状态。"""
    if viewport is not None and not Engine._is_safe_collection_click_rect(
            link_listitem.rectangle(), viewport):
        return False
    link_listitem.right_click_input()

    # The context menu is rebuilt after every right-click. A wrapper captured
    # before the click may point at the previous popup, so rebind it globally
    # when the supplied specification is no longer present.
    menu = delete_item
    try:
        if not menu.exists(timeout=0.8):
            title = (getattr(delete_item, 'criteria', {}) or {}).get('title', '删除')
            for window in Desktop(backend='uia').windows():
                candidate = window.child_window(title=title, control_type='MenuItem')
                if candidate.exists(timeout=0.15):
                    menu = candidate
                    break
    except Exception:
        pass
    try:
        if not menu.exists(timeout=1):
            return False
        menu.click_input()
    except Exception:
        return False

    # Some WeChat builds delete immediately after the context-menu command;
    # newer builds show a confirmation button. The confirmation is optional,
    # but if it is present it must be clicked before reporting success.
    if delete_button is not None:
        try:
            if delete_button.exists(timeout=0.8):
                delete_button.click_input()
        except Exception:
            return False
    time.sleep(0.25)
    return True


engine = Engine()
