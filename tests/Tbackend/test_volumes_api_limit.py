"""GET /api/volumes pages its result only when a limit is asked for."""

import sys
import unittest
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from flask import Flask

from frontend.api import api

API_KEY = 'test-key'


class TestVolumesApiLimit(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        self.client = app.test_client()

        settings = patch('frontend.api.Settings').start()
        settings.return_value.sv.api_key = API_KEY
        patch('frontend.api.StartTypeHandlers').start()
        self.library = patch('frontend.api.Library').start()
        self.addCleanup(patch.stopall)

    def get_volumes(self, query: str = '') -> dict:
        response = self.client.get(f'/api/volumes?api_key={API_KEY}{query}')
        self.assertEqual(response.status_code, 200)
        return response.get_json()['result']

    def test_volumes_without_limit_returns_full_list(self):
        self.library.get_public_volumes.return_value = [{'id': 1}]

        result = self.get_volumes()

        self.assertEqual(result, [{'id': 1}])
        self.library.get_public_volumes.assert_called_once()
        self.assertEqual(
            len(self.library.get_public_volumes.call_args.args), 2
        )

    def test_volumes_with_limit_returns_page(self):
        self.library.get_public_volumes.return_value = ([{'id': 1}], True)

        result = self.get_volumes('&limit=1&offset=5')

        self.assertEqual(result, {'has_more': True, 'result': [{'id': 1}]})
        args = self.library.get_public_volumes.call_args.args
        self.assertEqual(args[2:], (1, 5))


if __name__ == '__main__':
    unittest.main()
