import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from grainline import SearchOptions, optimize, validate

ROOT = Path(__file__).resolve().parents[1]


def small_problem(*, kerf=3, quantity=2):
    return {
        'units': 'mm', 'kerf_mm': kerf,
        'boards': [{'code': 'ply', 'length_mm': 100, 'width_mm': 50, 'grain_axis': 'long'}],
        'parts': [{'id': 'side', 'board': 'ply', 'length_mm': 40, 'width_mm': 40,
                   'quantity': quantity, 'grain_direction': 'length'}],
    }


def geometry(result):
    return [[(s['placements'], s['cut_plan_steps']) for s in b['layouts']] for b in result['boards']]


class CoreTests(unittest.TestCase):
    def test_complete_geometry_and_no_input_mutation(self):
        problem = small_problem()
        before = copy.deepcopy(problem)
        result = optimize(problem)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['sheets_used'], 1)
        self.assertEqual(validate(problem, result), [])
        self.assertEqual(problem, before)

    def test_zero_kerf_and_exact_fit(self):
        problem = small_problem(kerf=0)
        problem['parts'][0].update(length_mm=50, width_mm=50)
        result = optimize(problem)
        self.assertEqual(result['sheets_used'], 1)
        self.assertEqual(result['unplaced_count'], 0)
        self.assertEqual(validate(problem, result), [])

    def test_trim_and_edge_band_deduction(self):
        problem = small_problem(quantity=1)
        problem['boards'][0]['trim_mm'] = dict(left=10, right=10, top=5, bottom=5)
        problem['parts'][0].update(length_mm=82, width_mm=42, cut_length_mm=80, cut_width_mm=40)
        result = optimize(problem)
        self.assertEqual(validate(problem, result), [])
        placement = result['boards'][0]['layouts'][0]['placements'][0]
        self.assertEqual((placement['width'], placement['height']), (80, 40))
        self.assertEqual(placement['length_mm'], 82)

    def test_grain_constraints_reject_disallowed_rotation(self):
        problem = small_problem(quantity=1)
        problem['parts'][0].update(length_mm=40, width_mm=80)
        with self.assertRaisesRegex(ValueError, 'cannot fit'):
            optimize(problem)
        problem['parts'][0]['grain_direction'] = 'any'
        result = optimize(problem)
        self.assertTrue(result['boards'][0]['layouts'][0]['placements'][0]['rotated'])

    def test_legacy_grain_aliases_preserve_constraints(self):
        problem = small_problem()
        expected = geometry(optimize(problem))
        problem['parts'][0]['grain_direction'] = 'long'
        self.assertEqual(geometry(optimize(problem)), expected)
        problem['parts'][0]['grain_direction'] = 'typo'
        with self.assertRaises(ValueError):
            optimize(problem)

    def test_bad_inputs(self):
        mutations = [
            lambda p: p.update(kerf_mm=-1),
            lambda p: p.update(kerf_mm=float('nan')),
            lambda p: p['boards'][0].update(length_mm=float('inf')),
            lambda p: p['parts'][0].update(quantity=1.5),
            lambda p: p['parts'][0].update(quantity=True),
            lambda p: p['parts'][0].update(quantity=501),
            lambda p: p['parts'][0].update(board='missing'),
            lambda p: p['parts'][0].update(id='ambiguous:1'),
            lambda p: p['parts'][0].update(cut_length_mm=20),
            lambda p: p['parts'].append(dict(p['parts'][0])),
            lambda p: p['boards'][0].update(trim_mm={'left': 100}),
            lambda p: p.update(options={'partiton_tries': 10}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                problem = small_problem()
                mutation(problem)
                with self.assertRaises(ValueError):
                    optimize(problem)

    def test_invalid_options(self):
        for kwargs in [dict(seed_limit=1), dict(candidate_pool_limit=7), dict(partition_tries=-1),
                       dict(random_seed=True), dict(max_sheets=0), dict(partition_plans=0),
                       dict(time_limit=0), dict(time_limit=float('inf')), dict(seed_early_stop=1)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SearchOptions(**kwargs)

    def test_sheet_cap_returns_honest_partial(self):
        problem = small_problem(quantity=3)
        result = optimize(problem, options=SearchOptions(max_sheets=1))
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['sheets_used'], 1)
        self.assertEqual(result['unplaced_count'], 1)
        self.assertEqual(result['search']['stop_reason'], 'sheet_limit')
        self.assertEqual(validate(problem, result, require_complete=False), [])
        self.assertTrue(validate(problem, result))

    def test_soft_deadline_accounts_for_all_materials(self):
        problem = small_problem(quantity=3)
        problem['boards'].append({**problem['boards'][0], 'code': 'mdf'})
        problem['parts'].append({**problem['parts'][0], 'id': 'back', 'board': 'mdf'})
        result = optimize(problem, options=SearchOptions(time_limit=1e-9))
        self.assertEqual({b['board_id'] for b in result['boards']}, {'ply', 'mdf'})
        self.assertTrue(result['search']['time_limit_reached'])
        self.assertFalse(result['search']['reproducible_mode'])
        self.assertEqual(validate(problem, result, require_complete=False), [])

    def test_independent_validator_detects_tampering(self):
        problem = small_problem()
        result = optimize(problem)
        altered = copy.deepcopy(result)
        altered['boards'][0]['layouts'][0]['placements'][1]['x'] = 0
        self.assertTrue(any('overlap' in e for e in validate(problem, altered)))
        altered = copy.deepcopy(result)
        altered['boards'][0]['layouts'][0]['placements'][0]['width'] = 39
        self.assertTrue(any('dimensions' in e for e in validate(problem, altered)))
        altered = copy.deepcopy(result)
        rects = altered['boards'][0]['layouts'][0]['placements']
        rects[1]['part_id'] = rects[0]['part_id']
        self.assertTrue(any('duplicate' in e for e in validate(problem, altered)))

    def test_cabinet_budget_control_and_repeatability(self):
        problem = json.loads((ROOT / 'examples/cabinet-set-49.json').read_text(encoding='utf-8'))
        result = optimize(problem)
        self.assertEqual({b['board_id']: b['required_sheets'] for b in result['boards']}, {'PLY18': 5, 'MDF18': 1})
        self.assertTrue(result['validation']['cut_plan_available_for_all_sheets'])
        self.assertEqual(geometry(result), geometry(optimize(problem)))
        without_partition = optimize(problem, options=SearchOptions(partition_tries=0))
        self.assertEqual(without_partition['boards'][0]['required_sheets'], 6)
        self.assertEqual(validate(problem, without_partition), [])
        summary = result['telemetry']['board_summaries'][0]
        stats = summary['sheet_plan_fallback']['target_stats']
        self.assertGreater(stats[0]['trials_executed'], 0)
        self.assertLessEqual(stats[0]['trials_executed'], 6000)

    def test_pattern_search_finds_a_split_the_packer_misses(self):
        problem = json.loads((ROOT / 'examples/syn-rotation-allowed.json').read_text(encoding='utf-8'))
        result = optimize(problem)
        self.assertEqual(result['sheets_used'], 4)
        self.assertEqual(validate(problem, result), [])
        self.assertEqual(geometry(result), geometry(optimize(problem)))
        stats = result['telemetry']['board_summaries'][0]['sheet_plan_fallback']['target_stats'][0]
        self.assertEqual(stats['plans_found'], 1)
        self.assertGreater(stats['pattern_samples'], 0)

    def test_explicit_options_replace_embedded_options(self):
        problem = {**small_problem(), 'options': {'max_sheets': 0}}
        result = optimize(problem, options=SearchOptions.from_preset('fast', random_seed=7))
        self.assertEqual(result['search']['options']['random_seed'], 7)


class CliTests(unittest.TestCase):
    def call(self, *args, input=None, cwd=None):
        return subprocess.run([sys.executable, '-m', 'grainline', *args], input=input,
                              text=True, encoding='utf-8', capture_output=True, cwd=cwd)

    def test_stdin_stdout_and_options(self):
        problem = {**small_problem(), 'options': {'preset': 'fast', 'random_seed': 7}}
        run = self.call('optimize', '-', '--partition-tries', '0', '--random-seed', '42',
                        input=json.dumps(problem))
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result['search']['options']['seed_limit'], 2)
        self.assertEqual(result['search']['options']['random_seed'], 42)
        self.assertEqual(result['search']['options']['partition_tries'], 0)
        self.assertEqual(run.stderr, '')

    def test_output_error_does_not_clobber_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'problem.json'
            original = json.dumps(small_problem())
            path.write_text(original, encoding='utf-8')
            run = self.call('optimize', str(path), '-o', str(path))
            self.assertEqual(run.returncode, 2)
            self.assertEqual(path.read_text(encoding='utf-8'), original)
            self.assertEqual(run.stdout, '')

    def test_file_output_unicode_and_validation_outside_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'problem.json'
            output = Path(temp) / 'result.json'
            problem = small_problem()
            problem['parts'][0]['name'] = '\u67dc\u4f53\u4fa7\u677f'
            path.write_text(json.dumps(problem, ensure_ascii=False), encoding='utf-8-sig')
            run = self.call('optimize', str(path), '-o', str(output), cwd=temp)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, '')
            self.assertIn('\u67dc\u4f53\u4fa7\u677f', output.read_text(encoding='utf-8'))
            checked = self.call('validate', str(path), str(output), cwd=temp)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)['valid'])

    def test_error_and_incomplete_exit_codes(self):
        run = self.call('optimize', '-', input='{bad json')
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, '')
        run = self.call('optimize', '-', '--max-sheets', '1', input=json.dumps(small_problem(quantity=3)))
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertEqual(json.loads(run.stdout)['status'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
