"""Settings validation should keep enum-typed proxy settings intact."""

import sys
import unittest
from unittest.mock import MagicMock, patch

from backend.base.definitions import ProxyType

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()


class TestSettingsValidation(unittest.TestCase):

    def test_database_version_update_preserves_proxy_enum(self) -> None:
        from backend.internals.settings import Settings, SettingsValues

        settings = object.__new__(Settings)
        settings.get_settings = lambda: SettingsValues(
            proxy_type=ProxyType.HTTP,
            proxy_host='proxy.internal',
            proxy_port=8080,
            proxy_username='',
            proxy_password=''
        )

        with patch(
            'backend.internals.settings.build_proxy_url',
            return_value='http://proxy.internal:8080'
        ) as build_proxy_url, patch(
            'backend.internals.settings.test_proxy_url'
        ):
            settings._Settings__validate_settings({'database_version': 49})

        self.assertIs(build_proxy_url.call_args.args[0], ProxyType.HTTP)


if __name__ == '__main__':
    unittest.main()
