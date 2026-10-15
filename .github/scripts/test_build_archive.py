"""Regression tests for pagination, privacy, failure protection and no-op runs."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import build_archive as archive


def repo(index, name=None, created='2026-01-31T15:00:00Z'):
    name = name or f'repo-{index}'
    full = f'{archive.OWNER}/{name}'
    return {'id': index, 'name': name, 'full_name': full,
            'owner': {'login': archive.OWNER}, 'private': False, 'visibility': 'public',
            'html_url': f'https://github.com/{full}', 'created_at': created}


def profile(count):
    return {'login': archive.OWNER, 'public_repos': count}


class CollectionTests(unittest.TestCase):
    def test_all_pages_deduplication_and_kst_boundary(self):
        first = [repo(0, 'Choi-sArchive')] + [repo(i) for i in range(1, 100)]
        second = [repo(99), repo(100)]
        with patch.object(archive, 'get_json', side_effect=[profile(101), first, second, profile(101)]) as get:
            rows, count, pages = archive.collect_repositories()
        self.assertEqual((len(rows), count, pages), (100, 101, 2))
        self.assertEqual(rows[0][0].isoformat(), '2026-02-01T00:00:00+09:00')
        self.assertIn('page=2', get.call_args_list[2].args[0])
        self.assertNotIn('Choi-sArchive', [row[1] for row in rows])

    def test_private_repositories_never_rendered(self):
        private = {'private': True, 'name': 'private-do-not-publish'}
        with patch.object(archive, 'get_json', side_effect=[profile(2), [repo(0, 'Choi-sArchive'), repo(1), private], profile(2)]):
            rows, _, _ = archive.collect_repositories()
        output = '\n'.join(archive.render(rows, 'test').values())
        self.assertNotIn('private-do-not-publish', output)
        self.assertEqual(len(rows), 1)

    def test_partial_page_failure_never_writes(self):
        first = [repo(0, 'Choi-sArchive')] + [repo(i) for i in range(1, 100)]
        with patch.object(archive, 'get_json', side_effect=[profile(101), first, RuntimeError('HTTP 503')]), \
             patch.object(archive, 'update_files') as update:
            self.assertEqual(archive.main(), 1)
            update.assert_not_called()

    def test_empty_malformed_and_incomplete_responses_rejected(self):
        for batch in ([], {'message': 'API error'}, [repo(1)], [{'name': 'missing-visibility'}]):
            with self.subTest(batch=batch), \
                 patch.object(archive, 'get_json', side_effect=[profile(2), batch, profile(2)]), \
                 self.assertRaises(ValueError):
                archive.collect_repositories()

    def test_repository_count_race_rejected(self):
        with patch.object(archive, 'get_json', side_effect=[profile(2), [repo(0, 'Choi-sArchive'), repo(1)], profile(3)]), \
             self.assertRaises(ValueError):
            archive.collect_repositories()

    def test_invalid_creation_date_rejected(self):
        for created in ('not-a-date', '2026-01-01T00:00:00', '2026-01-01T00:00:00+09:00'):
            with self.subTest(created=created), \
                 patch.object(archive, 'get_json', side_effect=[profile(2), [repo(0, 'Choi-sArchive'), repo(1, created=created)]]), \
                 self.assertRaises(ValueError):
                archive.collect_repositories()

    def test_empty_archive_is_valid_when_only_self_remains(self):
        with patch.object(archive, 'get_json', side_effect=[profile(1), [repo(0, 'Choi-sArchive')], profile(1)]):
            self.assertEqual(archive.collect_repositories(), ([], 1, 1))

    def test_http_error_sanitized(self):
        error = urllib.error.HTTPError('https://api.github.com/test', 401, 'Unauthorized', {}, None)
        with patch.object(archive.urllib.request, 'urlopen', side_effect=error), \
             self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
            archive.get_json('/test')


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        created = dt.datetime(2026, 2, 1, tzinfo=archive.KST)
        self.rows = [(created, 'example', f'https://github.com/{archive.OWNER}/example')]

    def test_no_change_keeps_files_and_timestamp_identical(self):
        self.assertTrue(archive.update_files(self.root, self.rows))
        before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob('*') if p.is_file()}
        self.assertFalse(archive.update_files(self.root, self.rows))
        after = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue((self.root / 'archive/2026/02-February/README.md').exists())
        self.assertFalse((self.root / 'archive/2026/01-January').exists())

    def test_new_public_repo_added_and_deleted_or_private_repo_removed(self):
        archive.update_files(self.root, self.rows)
        next_row = (dt.datetime(2027, 1, 1, tzinfo=archive.KST), 'new',
                    f'https://github.com/{archive.OWNER}/new')
        self.assertTrue(archive.update_files(self.root, self.rows + [next_row]))
        self.assertTrue((self.root / 'archive/2027/01-January/README.md').exists())
        self.assertTrue(archive.update_files(self.root, [next_row]))
        self.assertFalse((self.root / 'archive/2026').exists())
        self.assertNotIn('/example)', (self.root / 'archive/README.md').read_text())

    def test_last_removal_preserves_unmanaged_files(self):
        archive.update_files(self.root, self.rows)
        note = self.root / 'archive/notes.txt'
        note.write_text('Keep this file')
        self.assertTrue(archive.update_files(self.root, []))
        self.assertTrue((self.root / 'archive/README.md').exists())
        self.assertFalse((self.root / 'archive/2026').exists())
        self.assertEqual(note.read_text(), 'Keep this file')

    def test_internal_markdown_links_resolve(self):
        import re
        archive.update_files(self.root, self.rows)
        for path in self.root.rglob('README.md'):
            for target in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                if not target.startswith('https://'):
                    self.assertTrue((path.parent / target).resolve().is_file(), (path, target))


if __name__ == '__main__':
    unittest.main()
