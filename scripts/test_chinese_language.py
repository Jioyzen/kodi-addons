import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ChineseLanguageTests(unittest.TestCase):
    def test_standard_identity_locale_and_unicode_font_are_preserved(self):
        root = ET.parse(ROOT / 'addons/resource.language.zh_cn/addon.xml').getroot()
        self.assertEqual(root.get('id'), 'resource.language.zh_cn')
        self.assertEqual(root.get('name'), 'Chinese（Jioyzen）')
        language = root.find("extension[@point='kodi.resource.language']")
        self.assertEqual(language.get('locale'), 'zh_CN')
        self.assertEqual(language.find('charsets/gui').get('unicodefont'), 'true')
        self.assertEqual(language.findtext('charsets/subtitle'), 'CP936')
        self.assertEqual(root.find("extension[@point='kodi.addon.metadata']/assets/icon").text, 'icon.png')

    def test_catalog_has_verified_coreelec_translations(self):
        data = (ROOT / 'addons/resource.language.zh_cn/resources/strings.po').read_text(encoding='utf-8')
        for translated in ['允许硬件加速', 'Amlogic', '禁用降噪', '色调映射']:
            self.assertIn(translated, data)
        self.assertIn('"Language: zh_cn\\n"', data)


if __name__ == '__main__':
    unittest.main(verbosity=2)
