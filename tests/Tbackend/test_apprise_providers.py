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


if __name__ == '__main__':
    unittest.main()
