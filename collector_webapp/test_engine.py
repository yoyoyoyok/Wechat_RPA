"""Offline regression tests for collector parsing and control logic."""
import sys
import unittest
from types import SimpleNamespace
from datetime import datetime, date, timedelta

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from collector_webapp.engine import Control, Engine, TaskStopped, delete_favorite_card
from src.pyweixin.Uielements import Regex_Patterns


class FakeElement:
    def __init__(self, text, top=100, left=100, parent=None):
        self._text = text
        self._rect = SimpleNamespace(top=top, bottom=top + 30, left=left, right=left + 200)
        self._parent = parent

    def window_text(self):
        return self._text

    def rectangle(self):
        return self._rect

    def class_name(self):
        return "article__item__title"

    def parent(self):
        return self._parent

    def exists(self):
        return True

    def descendants(self, control_type=None):
        return [FakeElement("阅读 100 赞 2")]

    def children(self, control_type=None):
        return self.descendants(control_type)


class CollectorOfflineTests(unittest.TestCase):
    def test_control_pause_and_stop(self):
        control = Control()
        control.pause()
        self.assertTrue(control.paused)
        control.resume()
        self.assertFalse(control.paused)
        control.stop()
        with self.assertRaises(TaskStopped):
            control.checkpoint()

    def test_title_cleanup_prefers_collected_title(self):
        pattern = Regex_Patterns.Article_Timestamp_pattern
        title, matched = Engine._clean_card_title(
            "链接一篇真实文章张三慧聪工程机械网2026年9月18日 16:43",
            pattern,
            "慧聪工程机械网",
            ["一篇真实文章"],
        )
        self.assertEqual(title, "一篇真实文章")
        self.assertTrue(matched)

    def test_title_filter_accepts_nested_article_signal(self):
        pattern = Regex_Patterns.Article_Timestamp_pattern
        parent = FakeElement("article")
        element = FakeElement("这是一篇长度足够的文章标题", parent=parent)
        viewport = SimpleNamespace(top=0, bottom=800, left=0, right=1200)
        self.assertTrue(Engine._is_article_title(element, pattern, viewport))

    def test_title_filter_rejects_timestamp_and_out_of_view(self):
        pattern = Regex_Patterns.Article_Timestamp_pattern
        parent = FakeElement("article")
        timestamp = FakeElement("2026年9月18日 16:43", parent=parent)
        outside = FakeElement("这是一篇长度足够的文章标题", top=900, parent=parent)
        viewport = SimpleNamespace(top=0, bottom=800, left=0, right=1200)
        self.assertFalse(Engine._is_article_title(timestamp, pattern, viewport))
        self.assertFalse(Engine._is_article_title(outside, pattern, viewport))

    def test_collapsed_article_group_detection(self):
        class Group:
            def __init__(self, class_name, text):
                self._class_name = class_name
                self._text = text

            def class_name(self):
                return self._class_name

            def descendants(self, control_type=None):
                return [SimpleNamespace(window_text=lambda: self._text)]

        self.assertTrue(Engine._is_collapsed_article_group(
            Group('article-list__more-bar', '余下 4 篇')))
        self.assertTrue(Engine._is_collapsed_article_group(
            Group('featured-msg-collapse-bar', '2个内容')))
        self.assertFalse(Engine._is_collapsed_article_group(
            Group('article-list__more-bar', '')))

    def test_scroll_uses_small_increment(self):
        import collector_webapp.engine as engine_module
        calls = []
        old_scroll = engine_module.pyautogui.scroll
        old_sleep = engine_module.time.sleep
        try:
            engine_module.pyautogui.scroll = lambda amount: calls.append(amount)
            engine_module.time.sleep = lambda _: None
            browser = SimpleNamespace(rectangle=lambda: SimpleNamespace(
                left=0, right=1000, top=0, bottom=800))
            Engine._scroll_page(browser)
        finally:
            engine_module.pyautogui.scroll = old_scroll
            engine_module.time.sleep = old_sleep
        self.assertEqual(calls, [-300])

    def test_collapsed_group_near_bottom_is_unsafe(self):
        viewport = SimpleNamespace(top=0, bottom=800, left=0, right=1200)
        near_bottom = SimpleNamespace(top=690, bottom=760, left=100, right=800)
        safe = SimpleNamespace(top=500, bottom=570, left=100, right=800)
        self.assertFalse(Engine._is_safe_collapsed_group_rect(near_bottom, viewport))
        self.assertTrue(Engine._is_safe_collapsed_group_rect(safe, viewport))

    def test_initial_pinned_collapsed_group_only_matches_upper_band(self):
        viewport = SimpleNamespace(top=100, bottom=900, left=0, right=1200)
        top = SimpleNamespace(top=220, bottom=280)
        historical = SimpleNamespace(top=600, bottom=660)
        self.assertTrue(Engine._is_initial_pinned_collapsed_rect(top, viewport))
        self.assertFalse(Engine._is_initial_pinned_collapsed_rect(historical, viewport))

    def test_collapsed_group_type_distinguishes_archive_from_pinned(self):
        self.assertFalse(Engine._is_pinned_collapsed_group_text('余下 5 篇'))
        self.assertTrue(Engine._is_pinned_collapsed_group_text('2 个内容'))
        self.assertTrue(Engine._is_pinned_collapsed_group_text('置顶'))

    def test_date_boundary_does_not_stop_on_mixed_viewport(self):
        self.assertEqual(Engine._update_old_date_rounds(0, 2, 0, 3), (0, False))
        self.assertEqual(Engine._update_old_date_rounds(1, 0, 0, 4), (2, True))
        self.assertEqual(Engine._update_old_date_rounds(1, 0, 1, 4), (0, False))

    def test_date_ranged_collection_does_not_stop_at_article_count(self):
        self.assertFalse(Engine._collection_target_reached(10, 10, '2026-09-16'))
        self.assertTrue(Engine._collection_target_reached(10, 10, None))
        self.assertTrue(Engine._collection_target_reached(11, 10, None))

    def test_wechat_relative_and_weekday_dates(self):
        now = datetime(2026, 9, 21, 12, 0)  # Monday
        self.assertEqual(Engine._parse_article_date('今天', now), '2026-09-21')
        self.assertEqual(Engine._parse_article_date('昨日', now), '2026-09-20')
        self.assertEqual(Engine._parse_article_date('前天', now), '2026-09-19')
        self.assertEqual(Engine._parse_article_date('星期日', now), '2026-09-20')
        self.assertEqual(Engine._parse_article_date('周一', now), '2026-09-14')

    def test_wechat_month_day_rolls_back_future_date_to_previous_year(self):
        now = datetime(2026, 1, 2, 12, 0)
        self.assertEqual(Engine._parse_article_date('12月31日', now), '2025-12-31')
        self.assertEqual(Engine._parse_article_date('2026年1月2日', now), '2026-01-02')

    def test_date_group_anchors_bind_titles_by_screen_position(self):
        class Text:
            def __init__(self, text, top):
                self._text = text
                self._rect = SimpleNamespace(top=top, bottom=top + 20)
            def window_text(self):
                return self._text
            def rectangle(self):
                return self._rect

        anchors = Engine._date_group_anchors([
            Text('昨天', 100), Text('星期日', 400), Text('公众号普通文本', 10),
        ])
        first = Text('第一篇文章标题足够长', 180)
        second = Text('第二篇文章标题足够长', 500)
        bound, inherited, labels = Engine._bind_article_dates([first, second], anchors)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        sunday = Engine._parse_article_date('星期日')
        self.assertEqual(bound[id(first)], yesterday)
        self.assertEqual(bound[id(second)], sunday)
        self.assertEqual(inherited, sunday)
        self.assertEqual([x[0] for x in labels], ['昨天', '星期日'])

    def test_click_rect_rejects_taskbar_edge_and_accepts_inner_card(self):
        viewport = SimpleNamespace(top=100, bottom=900, left=100, right=1400)
        taskbar_edge = SimpleNamespace(top=820, bottom=880, left=400, right=900)
        inner_card = SimpleNamespace(top=300, bottom=430, left=300, right=1000)
        self.assertFalse(Engine._is_safe_click_rect(taskbar_edge, viewport))
        self.assertTrue(Engine._is_safe_click_rect(inner_card, viewport))

    def test_card_fingerprint_normalizes_text(self):
        first = Engine._card_fingerprint('链接  标题\u200b 公众号', '标题')
        second = Engine._card_fingerprint('链接 标题 公众号', '标题')
        self.assertEqual(first, second)

    def test_collection_snapshot_filters_empty_items(self):
        class Item:
            def __init__(self, text):
                self.text = text

            def window_text(self):
                return self.text

        class List:
            def children(self, control_type=None):
                return [Item('第一条'), Item(''), Item('第二条')]

        self.assertEqual([x.window_text() for x in Engine._collection_items(List())], ['第一条', '第二条'])

    def test_collection_snapshot_includes_card_positions(self):
        class Item:
            def __init__(self, top):
                self.top = top
            def window_text(self):
                return '相同文本'
            def rectangle(self):
                return SimpleNamespace(left=100, top=self.top, right=900, bottom=self.top + 80)
        first = Engine._collection_snapshot([Item(100)])
        second = Engine._collection_snapshot([Item(20)])
        self.assertNotEqual(first, second)

    def test_collection_items_are_sorted_by_screen_position(self):
        class Item:
            def __init__(self, name, top):
                self.name, self.top = name, top
            def window_text(self):
                return self.name
            def rectangle(self):
                return SimpleNamespace(left=100, top=self.top, right=900, bottom=self.top + 80)
        class List:
            def children(self, control_type=None):
                return [Item('第三条', 500), Item('第一条', 100), Item('第二条', 300)]
        self.assertEqual(
            [item.window_text() for item in Engine._collection_items_sorted(List())],
            ['第一条', '第二条', '第三条'],
        )

    def test_collection_reset_to_top_rebinds_list_and_sends_home(self):
        import collector_webapp.engine as engine_module
        calls = []
        class List:
            def set_focus(self):
                calls.append('focus')
            def type_keys(self, key):
                calls.append(key)
        class Window:
            def child_window(self, **kwargs):
                calls.append(kwargs)
                return List()
        old_sleep = engine_module.time.sleep
        try:
            engine_module.time.sleep = lambda _: None
            result = Engine()._reset_collection_to_top(Window())
        finally:
            engine_module.time.sleep = old_sleep
        self.assertIsNotNone(result)
        self.assertIn('{HOME}', calls)

    def test_open_collection_link_list_activates_category_before_top_reset(self):
        import collector_webapp.engine as engine_module
        calls = []

        class Link:
            def exists(self, timeout=0):
                calls.append(('exists', timeout))
                return True

            def double_click_input(self):
                calls.append('activate-links')

        class List:
            def exists(self, timeout=0):
                return True

            def children(self, control_type=None):
                return [SimpleNamespace(window_text=lambda: '链接 目标文章 公众号')]

        class Window:
            def child_window(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get('title') == '链接':
                    return Link()
                return List()

        engine = Engine()
        old_sleep = engine_module.time.sleep
        try:
            engine_module.time.sleep = lambda seconds: calls.append(('sleep', seconds))
            engine._reset_collection_to_top = lambda window, link_list=None: calls.append('reset-top')
            result = engine._open_collection_link_list(Window())
        finally:
            engine_module.time.sleep = old_sleep

        self.assertIsNotNone(result[0])
        self.assertIsNotNone(result[1])
        self.assertLess(calls.index('activate-links'), calls.index('reset-top'))
        self.assertIn(('sleep', 3.0), calls)

    def test_collection_scroll_position_reads_uia_provider(self):
        class Scroll:
            CurrentVerticalScrollPercent = 37.5
        link_list = SimpleNamespace(iface_scroll=Scroll())
        self.assertEqual(Engine._collection_scroll_position(link_list), 37.5)

    def test_collection_candidate_must_intersect_current_viewport(self):
        viewport = SimpleNamespace(left=100, right=900, top=100, bottom=800)
        visible = SimpleNamespace(rectangle=lambda: SimpleNamespace(left=200, right=800, top=500, bottom=650))
        below = SimpleNamespace(rectangle=lambda: SimpleNamespace(left=200, right=800, top=900, bottom=1050))
        self.assertTrue(Engine._is_item_in_viewport(visible, viewport))
        self.assertFalse(Engine._is_item_in_viewport(below, viewport))

    def test_collection_scroll_uses_wheel_without_advancing_focus(self):
        import collector_webapp.engine as engine_module
        calls = []
        class List:
            def rectangle(self):
                return SimpleNamespace(left=100, right=900, top=100, bottom=800)
            def type_keys(self, *_args, **_kwargs):
                raise AssertionError('收藏列表滚动不应使用 Down 改变焦点')
        old_move = engine_module.pyautogui.moveTo
        old_scroll = engine_module.pyautogui.scroll
        old_sleep = engine_module.time.sleep
        try:
            engine_module.pyautogui.moveTo = lambda x, y: calls.append(('move', x, y))
            engine_module.pyautogui.scroll = lambda amount: calls.append(('scroll', amount))
            engine_module.time.sleep = lambda _: None
            self.assertTrue(Engine._scroll_collection_list(List()))
        finally:
            engine_module.pyautogui.moveTo = old_move
            engine_module.pyautogui.scroll = old_scroll
            engine_module.time.sleep = old_sleep
        self.assertEqual(calls, [('move', 500, 450), ('scroll', -1)])

    def test_collection_scroll_prefers_uia_scroll_provider(self):
        import collector_webapp.engine as engine_module
        calls = []
        class Scroll:
            CurrentVerticalScrollPercent = 20
            def SetScrollPercent(self, **kwargs):
                calls.append(kwargs)
        class List:
            iface_scroll = Scroll()
            def rectangle(self):
                raise AssertionError('UIA scroll provider should avoid mouse fallback')
        old_sleep = engine_module.time.sleep
        try:
            engine_module.time.sleep = lambda _: None
            self.assertTrue(Engine._scroll_collection_list(List()))
        finally:
            engine_module.time.sleep = old_sleep
        self.assertEqual(calls, [{'verticalPercent': 28.0, 'horizontalPercent': -1}])

    def test_collection_scroll_prefers_native_list_scroll(self):
        import collector_webapp.engine as engine_module
        calls = []
        class List:
            def scroll(self, *args, **kwargs):
                calls.append((args, kwargs))
            def set_focus(self):
                calls.append(('focus', {}))
            def rectangle(self):
                raise AssertionError('mouse fallback should not run')
        old_sleep = engine_module.time.sleep
        try:
            engine_module.time.sleep = lambda _: None
            self.assertTrue(Engine._scroll_collection_list(List()))
        finally:
            engine_module.time.sleep = old_sleep
        self.assertEqual(calls, [('focus', {}), (('down', 'page'), {'count': 1, 'retry_interval': 0.05})])

    def test_collection_scroll_uses_list_wheel_before_page_scroll(self):
        import collector_webapp.engine as engine_module
        calls = []
        class List:
            def wheel_mouse_input(self, **kwargs):
                calls.append(('wheel', kwargs))
            def scroll(self, *args, **kwargs):
                calls.append(('page', args, kwargs))
        old_sleep = engine_module.time.sleep
        try:
            engine_module.time.sleep = lambda _: None
            self.assertTrue(Engine._scroll_collection_list(List()))
        finally:
            engine_module.time.sleep = old_sleep
        self.assertEqual(calls, [('wheel', {'wheel_dist': -6})])

    def test_collection_click_zone_allows_normal_bottom_card(self):
        viewport = SimpleNamespace(left=0, right=1200, top=0, bottom=1000)
        # The reported item can extend below the viewport; only its click
        # centre needs to be in the safe zone.
        card = SimpleNamespace(left=100, right=1100, top=700, bottom=1100)
        self.assertTrue(Engine._is_safe_collection_click_rect(card, viewport))
        edge = SimpleNamespace(left=100, right=1100, top=900, bottom=1100)
        self.assertFalse(Engine._is_safe_collection_click_rect(edge, viewport))

    def test_collection_safe_zone_requires_rectangle_not_wrapper(self):
        viewport = SimpleNamespace(left=0, right=1200, top=0, bottom=1000)
        wrapper = SimpleNamespace(rectangle=lambda: SimpleNamespace(
            left=100, right=1100, top=300, bottom=600))
        self.assertTrue(Engine._is_safe_collection_click_rect(wrapper.rectangle(), viewport))

    def test_safe_visible_card_does_not_require_pre_copy_scroll(self):
        viewport = SimpleNamespace(left=0, right=1200, top=0, bottom=1000)
        visible = SimpleNamespace(left=100, right=1100, top=250, bottom=650)
        bottom = SimpleNamespace(left=100, right=1100, top=900, bottom=1100)
        self.assertTrue(Engine._is_safe_collection_click_rect(visible, viewport))
        self.assertFalse(Engine._is_safe_collection_click_rect(bottom, viewport))

    def test_delete_favorite_card_allows_immediate_delete_without_confirmation(self):
        class Menu:
            def __init__(self, exists=True):
                self.clicked = False
                self._exists = exists

            def exists(self, timeout=1):
                return self._exists

            def click_input(self):
                self.clicked = True

        class Card(Menu):
            def right_click_input(self):
                self.clicked = True

        card = Card()
        menu = Menu()
        confirm = Menu(exists=False)
        self.assertTrue(delete_favorite_card(card, menu, confirm))
        self.assertTrue(card.clicked)
        self.assertTrue(menu.clicked)


if __name__ == "__main__":
    unittest.main()
