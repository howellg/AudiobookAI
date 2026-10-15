"""V2.5 dictionary semantics, stable snapshots and converter integration."""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import audiobookai_pronunciation as p


class PronunciationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path = root / 'dictionary.json'
        self.snapshots = root / 'jobs'
        self.book = root / 'novel.epub'
        self.other = root / 'other.epub'

    def test_missing_dictionary_is_empty(self):
        """An existing V2.4 installation starts with no substitution rules."""
        self.assertEqual(p.load_dictionary(self.path)['global'], [])
        self.assertEqual(p.apply_rules('Nynaeve', []), 'Nynaeve')

    def test_scope_and_override(self):
        """A book-specific rule overrides global pronunciation for that title."""
        p.save_rule('global', None, 'Nynaeve', 'Nigh-neev', self.path)
        p.save_rule('book', self.book, 'Nynaeve', 'Nih-nave', self.path)
        self.assertEqual(p.apply_rules('Nynaeve', p.effective_rules(self.book, self.path)), 'Nih-nave')
        self.assertEqual(p.apply_rules('Nynaeve', p.effective_rules(self.other, self.path)), 'Nigh-neev')

    def test_non_cascading_longest_phrase_and_boundaries(self):
        """Rules match whole terms once, not substrings or substitution results."""
        rules = [{'term': 'Aes Sedai', 'spoken': 'Eye-suh-dye'},
                 {'term': 'Sedai', 'spoken': 'wrong'},
                 {'term': 'Nynaeve', 'spoken': 'Sedai'}]
        result = p.apply_rules('Aes Sedai and Nynaeve; sedai not Unsedai.', rules)
        self.assertEqual(result, 'Eye-suh-dye and Sedai; wrong not Unsedai.')

    def test_case_insensitive_inline_whitespace(self):
        self.assertEqual(p.apply_rules('AES   SEDAI\nand aes\tsedai',
                         [{'term': 'Aes Sedai', 'spoken': 'Eye-suh-dye'}]),
                         'Eye-suh-dye\nand Eye-suh-dye')

    def test_duplicate_update_and_delete(self):
        p.save_rule('global', None, 'Logain', 'Lo-gain', self.path)
        p.save_rule('global', None, 'LOGAIN', 'Low-gain', self.path)
        self.assertEqual(len(p.scoped_rules('global', None, self.path)), 1)
        p.delete_rule('global', None, 'logain', self.path)
        self.assertEqual(p.scoped_rules('global', None, self.path), [])

    def test_reject_bad_input(self):
        for term, spoken in [('', 'x'), ('Nynaeve', ''), ('bad\ninput', 'x'),
                             ('Nynaeve', 'one two three four five six seven'),
                             ('.not a term', 'foo')]:
            with self.subTest(term=term):
                with self.assertRaises(ValueError):
                    p.save_rule('global', None, term, spoken, self.path)

    def test_job_snapshot_is_immutable(self):
        """Later dictionary edits must not rewrite yesterday's queued narration."""
        p.save_rule('global', None, 'Rand', 'Ranned', self.path)
        frozen = p.snapshot_for_job(23, self.book, self.path, self.snapshots)
        p.save_rule('global', None, 'Rand', 'Rond', self.path)
        self.assertEqual(p.apply_rules('Rand', p.load_snapshot(frozen)), 'Ranned')
        self.assertEqual(p.apply_rules('Rand', p.effective_rules(self.book, self.path)), 'Rond')
        with self.assertRaises(FileExistsError):
            p.snapshot_for_job(23, self.book, self.path, self.snapshots)

    def test_old_jobs_are_unchanged(self):
        """A pre-V2.5 job without a snapshot must not acquire current rules."""
        self.assertIsNone(p.snapshot_path_for_job(45, self.snapshots))

    def test_snapshot_invalid_is_rejected(self):
        file = self.snapshots / 'bad.json'
        file.parent.mkdir()
        file.write_text('{"version":99,"rules":[]}')
        with self.assertRaises(ValueError):
            p.load_snapshot(file)

    def test_converter_signature_uses_spoken_text_only_when_snapshot_active(self):
        """Cache signatures change with spoken hints but keep original EPUB text."""
        from epub_to_audiobook_v7_studio import chapter_signature
        from epub_to_audiobook_v7_studio import Section
        import epub_to_audiobook_v7_studio as converter
        class Engine:
            SMOOTH_VERSION = 'x'
            FINAL_CONTINUATION_PAUSE = 0
            EDGE_KEEP_SECONDS = 0
            MAX_EDGE_TRIM_SECONDS = 0
            FINAL_COMMA_PAUSE = 0
            FINAL_SENTENCE_PAUSE = 0
            FINAL_DIALOGUE_PAUSE = 0
            FINAL_PARAGRAPH_PAUSE = 0
            FINAL_SCENE_PAUSE = 0
            LEADING_PAD_FINAL = 0
            MAX_PEAK = 1
            def make_chunks(self, text):
                return [SimpleNamespace(text=text, boundary='end', dialogue=False)]
            def cache_key(self, text):
                return text
        section = Section(1, 'Chapter 1', ['Nynaeve speaks.'], '1.xhtml')
        original = converter.ACTIVE_PRONUNCIATIONS
        try:
            converter.ACTIVE_PRONUNCIATIONS = []
            chunks1, sig1 = chapter_signature(Engine(), section)
            converter.ACTIVE_PRONUNCIATIONS = [{'term':'Nynaeve','spoken':'Nigh-neev'}]
            chunks2, sig2 = chapter_signature(Engine(), section)
        finally:
            converter.ACTIVE_PRONUNCIATIONS = original
        self.assertEqual(chunks1[0].text, 'Nynaeve speaks.')
        self.assertEqual(chunks2[0].text, 'Nigh-neev speaks.')
        self.assertNotEqual(sig1, sig2)
        self.assertEqual(section.text, 'Nynaeve speaks.')

    def test_sqlite_migration_and_required_snapshot_flag(self):
        """Existing jobs default to legacy rules; new queued parts carry a required flag."""
        from audiobookai_storage import Store
        store = Store(Path(self.tmp.name) / 'queue.sqlite3')
        store.db.execute("INSERT INTO books(path,title,author,sections,words,added) "
                         "VALUES(?,?,?,?,?,?)", (str(self.book), 'Novel', 'Author', 1, 2, 'now'))
        store.db.commit()
        legacy = store.add_job(1, 'old', [1], 'old_part')
        fresh = store.add_job(1, 'new', [2], 'new_part', pronunciation_required=True)
        self.assertEqual(store.job(legacy)['pronunciation_required'], 0)
        self.assertEqual(store.job(fresh)['pronunciation_required'], 1)
        store.db.close()

    def test_missing_required_snapshot_blocks_generation(self):
        """A missing frozen rule file must not silently fall back to raw EPUB text."""
        gui = (SCRIPTS / 'audiobook_gui_v2.py').read_text()
        self.assertIn("if job['pronunciation_required'] and snapshot is None:", gui)
        self.assertIn("error='Queued pronunciation snapshot is missing'", gui)

    def test_manifest_and_editor_provenance_wiring(self):
        """Every chapter records a snapshot, which the editor uses for rebuilds."""
        gui = (SCRIPTS / 'audiobook_gui_v2.py').read_text()
        converter = (SCRIPTS / 'epub_to_audiobook_v7_studio.py').read_text()
        editor = (SCRIPTS / 'audiobookai_editor.py').read_text()
        self.assertIn('snapshot_for_job(new_job_id', gui)
        self.assertIn('snapshot_path_for_job(job_id)', gui)
        self.assertIn('"pronunciation_snapshot"', converter)
        self.assertIn("get('pronunciation_snapshot')", editor)
        self.assertIn("['--pronunciation-file', self.pronunciation_snapshot]", editor)


if __name__ == '__main__':
    unittest.main()
