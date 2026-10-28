# ruff: noqa: E501
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jllm_plugins.storage.paths import resolve_data_path


class DataPathTests(unittest.TestCase):
    def setUp(self):
        original = Path.cwd()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(os.chdir, original)
        os.chdir(temp.name)
        setting = patch.dict(os.environ, {"DATA_DIR": "data"})
        setting.start()
        self.addCleanup(setting.stop)

    def test_auth_directory_does_not_hide_existing_dictionary_and_assets(self):
        for name in ('readings.json', 'sounds.json', 'media.json', 'user_voices.json'):
            Path(name).write_text('{}')
        Path('sounds').mkdir()
        Path('data').mkdir()
        Path('data/sounds').mkdir()
        Path('data/.dashboard-auth.json').write_text('existing credential')
        for key, name in [('READINGS_JSON', 'readings.json'), ('SOUNDS_JSON', 'sounds.json'),
                          ('MEDIA_JSON', 'media.json'), ('USER_VOICES_PATH', 'user_voices.json'),
                          ('SOUNDS_DIR', 'sounds')]:
            self.assertEqual(resolve_data_path(key, name, env={}), name)
        self.assertEqual(resolve_data_path('READINGS_DASHBOARD_AUTH_FILE', '.dashboard-auth.json', env={}),
                         'data/.dashboard-auth.json')

    def test_new_resource_stays_in_legacy_layout(self):
        Path('sounds.json').write_text('{}')
        self.assertEqual(resolve_data_path('READINGS_JSON', 'readings.json', env={}), 'readings.json')
        self.assertEqual(resolve_data_path('READINGS_DASHBOARD_AUTH_FILE', '.dashboard-auth.json', env={}), '.dashboard-auth.json')

    def test_new_install_uses_data_and_explicit_paths_take_precedence(self):
        self.assertEqual(resolve_data_path('READINGS_JSON', 'readings.json', env={}), 'data/readings.json')
        Path('readings.json').write_text('{}')
        self.assertEqual(resolve_data_path('READINGS_JSON', 'readings.json', env={'DATA_DIR':'custom'}), 'custom/readings.json')
        self.assertEqual(resolve_data_path('READINGS_JSON', 'readings.json', env={'DATA_DIR':'custom', 'READINGS_JSON':'specific.json'}), 'specific.json')

    def test_existing_data_layout_kept_without_legacy_files(self):
        Path('data').mkdir()
        Path('data/readings.json').write_text('{}')
        self.assertEqual(resolve_data_path('READINGS_JSON', 'readings.json', env={}), 'data/readings.json')
