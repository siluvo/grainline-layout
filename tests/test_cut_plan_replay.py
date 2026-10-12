"""Regression: a kerf-consuming edge trim must not erase the cutting sequence."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from grainline._internal.adaptive_frontier_release_v1 import Rect, _build_cut_plan_modes
from grainline._internal.cut_plan_validation import validate_cut_plan
from grainline._internal.optimization import run_optimization


class CutPlanReplayTests(unittest.TestCase):
    def make_plan(self, placements, length, width, kerf):
        modes = _build_cut_plan_modes(base_steps=[], placements=placements, offcut_regions=[],
                                     kerf_mm=kerf, sheet_length=length, sheet_width=width,
                                     sheet_id=1, source_strategy='regression')
        return modes

    def replay(self, placements, steps, length, width, kerf):
        return validate_cut_plan([vars(p) for p in placements], steps,
                                 sheet_length=length, sheet_width=width, kerf_mm=kerf)

    def test_edge_kerf_can_leave_one_child_on_every_side(self):
        for margin in (3.0, 1.0):
            fixtures = [
                ([Rect(margin, 0, 700, 480, 'p', 'side')], 700 + margin, 480),
                ([Rect(0, 0, 700, 480, 'p', 'side')], 700 + margin, 480),
                ([Rect(0, margin, 700, 480, 'p', 'side')], 700, 480 + margin),
                ([Rect(0, 0, 700, 480, 'p', 'side')], 700, 480 + margin),
            ]
            for parts, length, width in fixtures:
                with self.subTest(margin=margin, parts=parts):
                    modes = self.make_plan(parts, length, width, 3)
                    for mode in ('default', 'protect'):
                        steps = modes[mode]['steps']
                        self.assertEqual(len(steps), 1)
                        self.assertEqual(len(steps[0]['workpieces_after']), 1)
                        report = self.replay(parts, steps, length, width, 3)
                        self.assertTrue(report['passed'], report)

    def test_exact_stock_part_needs_no_cut(self):
        parts = [Rect(0, 0, 100, 50, 'p', 'whole board')]
        modes = self.make_plan(parts, 100, 50, 3)
        report = self.replay(parts, modes['default']['steps'], 100, 50, 3)
        self.assertTrue(report['passed'], report)
        self.assertFalse(report['cut_required'])

    def test_zero_kerf_still_generates_internal_cut(self):
        parts = [Rect(0, 0, 50, 50, 'a', 'a'), Rect(50, 0, 50, 50, 'b', 'b')]
        steps = self.make_plan(parts, 100, 50, 0)['default']['steps']
        self.assertEqual(len(steps), 1)
        self.assertTrue(self.replay(parts, steps, 100, 50, 0)['passed'])

    def test_nonempty_but_incomplete_sequence_is_rejected(self):
        parts = [Rect(3, 3, 700, 480, 'p', 'side')]
        steps = self.make_plan(parts, 703, 483, 3)['default']['steps']
        self.assertEqual(len(steps), 2)
        report = self.replay(parts, steps[:-1], 703, 483, 3)
        self.assertFalse(report['passed'])
        self.assertEqual(report['unreleased_part_ids'], ['p'])

    def test_replay_rejects_wrong_geometry_order_or_centerline(self):
        parts = [Rect(3, 3, 700, 480, 'p', 'side')]
        steps = self.make_plan(parts, 703, 483, 3)['default']['steps']
        self.assertTrue(self.replay(parts, steps, 703, 483, 3)['passed'])
        wrong = copy.deepcopy(steps)
        wrong[0]['workpieces_after'][0]['width'] -= 1
        self.assertFalse(self.replay(parts, wrong, 703, 483, 3)['passed'])
        self.assertFalse(self.replay(parts, list(reversed(steps)), 703, 483, 3)['passed'])
        wrong = copy.deepcopy(steps)
        wrong[0]['start']['x'] += 1
        self.assertFalse(self.replay(parts, wrong, 703, 483, 3)['passed'])

    def test_kids_desk_cleanup_keeps_a_complete_cut_sequence(self):
        path = Path(__file__).resolve().parents[1] / 'examples/kids-desk-single-sheet.json'
        case = json.loads(path.read_text(encoding='utf-8'))
        boards = [SimpleNamespace(id=b['code'], **b) for b in case['boards']]
        parts = [SimpleNamespace(id=f'p{i}', board_id=p['board'], **p) for i, p in enumerate(case['parts'])]
        result = run_optimization(parts, boards, kerf_mm=case['kerf_mm'], algorithm='adaptive_frontier_release_v1')
        self.assertEqual(result['sheets_used'], 1)
        self.assertEqual(result['unplaced_count'], 0)
        group = result['boards'][0]
        sheet = group['layouts'][0]
        self.assertEqual(len(sheet['placements']), 10)
        self.assertEqual(sheet['debug']['cleanup_strategy'], 'partition-first-baseline')
        for mode in ('default', 'protect'):
            steps = sheet['cut_plan_modes'][mode]['steps']
            self.assertGreater(len(steps), 0)
            report = validate_cut_plan(sheet['placements'], steps,
                                       sheet_length=group['usable_length_mm'],
                                       sheet_width=group['usable_width_mm'], kerf_mm=case['kerf_mm'])
            self.assertTrue(report['passed'], report)


if __name__ == '__main__':
    unittest.main()
