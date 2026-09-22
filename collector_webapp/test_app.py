"""Regression tests for the web page adapter."""
import unittest

from collector_webapp.app import _remove_task_center_demo_animation


class DesignPageTests(unittest.TestCase):
    def test_task_center_demo_counter_animation_is_removed(self):
        source = """
        <script>
          // Count-up animation for stat numbers
          function countUp(el, target, duration) { requestAnimationFrame(step); }
          document.querySelectorAll('[data-count]').forEach(function(el) {
            setTimeout(function() { countUp(el, 2, 800); }, 200);
          });
          // Refresh button feedback
          function refreshTasks() {}
        </script>
        """
        rendered = _remove_task_center_demo_animation(source)
        self.assertNotIn('Count-up animation for stat numbers', rendered)
        self.assertNotIn('countUp(', rendered)
        self.assertIn('Refresh button feedback', rendered)


if __name__ == '__main__':
    unittest.main()
