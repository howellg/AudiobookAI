import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_convenience import parse_section_ranges, job_category, estimate_audio_duration

class ConvenienceTests(unittest.TestCase):
    def test_selection_ranges_spine_order_dedup(self):
        self.assertEqual(parse_section_ranges('8-10, 9, 2', [1,2,3,8,9,10]), [2,8,9,10])

    def test_invalid_and_missing_sections_do_not_partially_apply(self):
        for expression in ('4-2', '1,,2', '1-900000', '5-6', '4-x', ''):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    parse_section_ranges(expression, [1,2,3,4])

    def test_queue_filter_and_estimated_duration(self):
        self.assertEqual(job_category('interrupted'), 'Needs attention')
        self.assertEqual(job_category('queued'), 'Pending / active')
        self.assertEqual(job_category('complete'), 'Completed')
        self.assertEqual(estimate_audio_duration(130), 60)

if __name__ == '__main__':
    unittest.main()
