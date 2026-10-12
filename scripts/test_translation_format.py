import tempfile
import unittest
from pathlib import Path
from check_translation_format import errors

ROOT = Path(__file__).resolve().parents[1]


class TranslationFormatTests(unittest.TestCase):
    def catalog(self, source, translated):
        import json
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'strings.po'
        # No c++-format flag: gettext alone missed the reported crash.
        path.write_text('msgctxt "#40025"\nmsgid ' + json.dumps(source)
                        + '\nmsgstr ' + json.dumps(translated, ensure_ascii=False) + '\n', encoding='utf-8')
        return path

    def test_extras_fullwidth_closing_brace_is_rejected(self):
        self.assertTrue(errors(self.catalog('Extras Manager: {0:s}', '额外管理器：{0:s｝')))

    def test_unknown_argument_and_incompatible_type_are_rejected(self):
        self.assertTrue(errors(self.catalog('Value: {0:d}', '值：{1:d}')))
        self.assertTrue(errors(self.catalog('Value: {}', '值：{0:s}')))

    def test_reordered_fields_escaped_braces_and_empty_translation_are_valid(self):
        self.assertFalse(errors(self.catalog('{0:s}: {1:d}', '{1:d}：{0:s}')))
        self.assertFalse(errors(self.catalog('Extras: {0:s}', '额外内容{{标题}}：{0:s}')))
        self.assertFalse(errors(self.catalog('Extras: {0:s}', '')))

    def test_published_chinese_catalog_has_no_bad_fmt_placeholders(self):
        self.assertEqual(errors(ROOT / 'addons/resource.language.zh_cn/resources/strings.po'), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
