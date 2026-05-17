# -*- coding: utf-8 -*-

"""Tests for apprise notification infrastructure."""
import unittest

from backend.features.notifications import (
    ApplicationUpdateEvent, DownloadEvent, HealthCheckEvent, TestEvent,
    VolumeAddEvent, format_application_update_notification,
    format_download_notification, format_health_check_notification,
    format_test_notification, format_volume_add_notification)


def make_download_event():
    return DownloadEvent(
        volume_id=1, volume_title='Batman', volume_year=2011,
        volume_comicvine_id=12345, volume_path='/comics/batman',
        issue_id=10, issue_comicvine_id=67890, issue_number='42',
        issue_title='The Dark Knight', file_path='/comics/batman/042.cbz',
        download_source='getcomics', is_upgrade=False
    )


class TestFormatters(unittest.TestCase):
    def test_format_download_returns_title_and_body(self):
        ev = make_download_event()
        title, body = format_download_notification(ev)
        self.assertEqual(title, 'Download Complete')
        self.assertIn('Batman', body)
        self.assertIn('42', body)
        self.assertIn('getcomics', body)

    def test_format_volume_add_returns_title_and_body(self):
        ev = VolumeAddEvent(
            volume_id=1, volume_title='Spider-Man', volume_year=1963,
            volume_comicvine_id=99999, volume_path='/comics/spidey',
            publisher='Marvel'
        )
        title, body = format_volume_add_notification(ev)
        self.assertEqual(title, 'Volume Added')
        self.assertIn('Spider-Man', body)
        self.assertIn('Marvel', body)

    def test_format_health_check_includes_level(self):
        ev = HealthCheckEvent(
            level='warning', message='Invalid API key',
            check_type='ComicVineApiKey'
        )
        title, body = format_health_check_notification(ev)
        self.assertIn('warning', title.lower())
        self.assertIn('ComicVineApiKey', body)
        self.assertIn('Invalid API key', body)

    def test_format_application_update_includes_versions(self):
        ev = ApplicationUpdateEvent(
            previous_version='1.0.0', new_version='1.1.0',
            message='Update available'
        )
        title, body = format_application_update_notification(ev)
        self.assertEqual(title, 'Application Updated')
        self.assertIn('1.0.0', body)
        self.assertIn('1.1.0', body)

    def test_format_test_returns_hardcoded_strings(self):
        ev = TestEvent()
        title, body = format_test_notification(ev)
        self.assertEqual(title, 'Test Notification')
        self.assertIn('Kapowarr', body)


class TestDiscordProvider(unittest.TestCase):
    def setUp(self):
        from backend.implementations.notification_providers.discord_apprise import \
            DiscordProvider
        self.provider = DiscordProvider()

    def test_validate_rejects_missing_url(self):
        from backend.base.custom_exceptions import InvalidNotificationSettings
        with self.assertRaises(InvalidNotificationSettings):
            self.provider.validate_settings({})

    def test_validate_rejects_non_discord_url(self):
        from backend.base.custom_exceptions import InvalidNotificationSettings
        with self.assertRaises(InvalidNotificationSettings):
            self.provider.validate_settings(
                {'webhook_url': 'https://example.com/webhook'})

    def test_validate_accepts_valid_url(self):
        # Should not raise
        self.provider.validate_settings({
            'webhook_url':
                'https://discord.com/api/webhooks/123456789/abcdefghij'
        })

    def test_send_calls_apprise_with_discord_url(self):
        from unittest.mock import MagicMock, patch
        settings = {
            'webhook_url':
                'https://discord.com/api/webhooks/123456789/abcdefghij'
        }
        mock_apprise = MagicMock()
        mock_apprise_instance = MagicMock()
        mock_apprise.return_value = mock_apprise_instance
        with patch(
            'backend.implementations.notification_providers'
            '.discord_apprise.apprise.Apprise',
            mock_apprise
        ):
            self.provider._send('Title', 'Body', settings)
        mock_apprise_instance.add.assert_called_once()
        url_arg = mock_apprise_instance.add.call_args[0][0]
        self.assertTrue(url_arg.startswith('discord://'))
        mock_apprise_instance.notify.assert_called_once_with(
            title='Title', body='Body')

    def test_on_download_sends_notification(self):
        from unittest.mock import patch
        ev = DownloadEvent(
            volume_id=1, volume_title='Batman', volume_year=2011,
            volume_comicvine_id=12345, volume_path='/comics/batman',
            issue_id=10, issue_comicvine_id=67890, issue_number='42',
            issue_title='The Dark Knight',
            file_path='/comics/batman/042.cbz',
            download_source='getcomics', is_upgrade=False
        )
        settings = {
            'webhook_url':
                'https://discord.com/api/webhooks/123456789/abcdefghij'
        }
        with patch.object(self.provider, '_send') as mock_send:
            self.provider.on_download(ev, settings)
        mock_send.assert_called_once()
        title_arg = mock_send.call_args[0][0]
        self.assertEqual(title_arg, 'Download Complete')


class TestProwlProvider(unittest.TestCase):
    def setUp(self):
        from backend.implementations.notification_providers.prowl_apprise import \
            ProwlProvider
        self.provider = ProwlProvider()

    def test_validate_rejects_missing_api_key(self):
        from backend.base.custom_exceptions import InvalidNotificationSettings
        with self.assertRaises(InvalidNotificationSettings):
            self.provider.validate_settings({})
        with self.assertRaises(InvalidNotificationSettings):
            self.provider.validate_settings({'api_key': ''})

    def test_validate_accepts_valid_settings(self):
        # Should not raise
        self.provider.validate_settings({'api_key': 'mykey123'})

    def test_build_url_minimal(self):
        url = self.provider._build_prowl_url({'api_key': 'mykey123'})
        self.assertTrue(url.startswith('prowl://mykey123'))

    def test_build_url_with_appname(self):
        url = self.provider._build_prowl_url({
            'api_key': 'mykey123',
            'application': 'MyApp'
        })
        self.assertIn('MyApp', url)

    def test_send_calls_apprise(self):
        from unittest.mock import MagicMock, patch
        settings = {'api_key': 'mykey123', 'application': 'Kapowarr'}
        mock_apprise = MagicMock()
        mock_instance = MagicMock()
        mock_apprise.return_value = mock_instance
        with patch(
            'backend.implementations.notification_providers'
            '.prowl_apprise.apprise.Apprise',
            mock_apprise
        ):
            self.provider._send('Title', 'Body', settings)
        mock_instance.add.assert_called_once()
        mock_instance.notify.assert_called_once_with(
            title='Title', body='Body')


if __name__ == '__main__':
    unittest.main()
