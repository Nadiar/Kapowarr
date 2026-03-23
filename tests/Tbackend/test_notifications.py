"""
Unit tests for the notification system.

Tests are designed to run without a live database by mocking DB calls
and external process/HTTP interactions.
"""

import os
import unittest
from dataclasses import dataclass
from typing import Dict, List
from unittest.mock import MagicMock, call, patch

from backend.features.notifications import (ApplicationUpdateEvent,
                                            DownloadEvent, HealthCheckEvent,
                                            NotificationService, TestEvent,
                                            VolumeAddEvent, provider_registry)
from backend.implementations.notification_providers.custom_script import \
    CustomScriptProvider
from backend.implementations.notification_providers.webhook import \
    WebhookProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_download_event():
    return DownloadEvent(
        volume_id=1,
        volume_title='Batman',
        volume_year=1940,
        volume_comicvine_id=12345,
        volume_path='/comics/batman',
        issue_id=10,
        issue_number='5',
        issue_title='The Dark Knight',
        file_path='/comics/batman/batman_005.cbz',
        download_source='getcomics',
        is_upgrade=False
    )


def make_volume_add_event():
    return VolumeAddEvent(
        volume_id=2,
        volume_title='Spider-Man',
        volume_year=1963,
        volume_comicvine_id=99999,
        volume_path='/comics/spiderman',
        publisher='Marvel'
    )


def make_health_event():
    return HealthCheckEvent(
        level='error',
        message='ComicVine API key is not set or invalid.',
        check_type='ComicVineApiKey'
    )


# ---------------------------------------------------------------------------
# CustomScriptProvider tests
# ---------------------------------------------------------------------------

class TestCustomScriptEnvVars(unittest.TestCase):

    def _run_with_capture(
        self,
        provider,
        event_method,
        event,
        path='/test/notify.sh'):
        """Call an event method and capture the env dict passed to subprocess."""
        settings = {'path': path}
        captured = {}

        def fake_run(args, env=None, timeout=None, capture_output=False):
            captured.update(env or {})
            m = MagicMock()
            m.returncode = 0
            return m

        with patch('subprocess.run', side_effect=fake_run):
            getattr(provider, event_method)(event, settings)

        return captured

    def test_custom_script_builds_env_vars_on_download(self):
        provider = CustomScriptProvider()
        event = make_download_event()
        env = self._run_with_capture(provider, 'on_download', event)

        self.assertEqual(env.get('kapowarr_eventtype'), 'Download')
        self.assertEqual(env.get('kapowarr_instancename'), 'Kapowarr')
        self.assertEqual(env.get('kapowarr_volume_id'), '1')
        self.assertEqual(env.get('kapowarr_volume_title'), 'Batman')
        self.assertEqual(env.get('kapowarr_volume_year'), '1940')
        self.assertEqual(env.get('kapowarr_volume_comicvine_id'), '12345')
        self.assertEqual(env.get('kapowarr_volume_path'), '/comics/batman')
        self.assertEqual(env.get('kapowarr_issue_id'), '10')
        self.assertEqual(env.get('kapowarr_issue_number'), '5')
        self.assertEqual(env.get('kapowarr_issue_title'), 'The Dark Knight')
        self.assertEqual(
            env.get('kapowarr_file_path'),
            '/comics/batman/batman_005.cbz')
        self.assertEqual(env.get('kapowarr_download_source'), 'getcomics')
        self.assertEqual(env.get('kapowarr_isupgrade'), 'FALSE')

    def test_custom_script_builds_env_vars_on_volume_add(self):
        provider = CustomScriptProvider()
        event = make_volume_add_event()
        env = self._run_with_capture(provider, 'on_volume_add', event)

        self.assertEqual(env.get('kapowarr_eventtype'), 'VolumeAdd')
        self.assertEqual(env.get('kapowarr_volume_id'), '2')
        self.assertEqual(env.get('kapowarr_volume_title'), 'Spider-Man')
        self.assertEqual(env.get('kapowarr_volume_year'), '1963')
        self.assertEqual(env.get('kapowarr_volume_publisher'), 'Marvel')

    def test_custom_script_builds_env_vars_on_health_check(self):
        provider = CustomScriptProvider()
        event = make_health_event()
        env = self._run_with_capture(provider, 'on_health_check', event)

        self.assertEqual(env.get('kapowarr_eventtype'), 'HealthIssue')
        self.assertEqual(env.get('kapowarr_health_issue_level'), 'error')
        self.assertEqual(
            env.get('kapowarr_health_issue_message'),
            'ComicVine API key is not set or invalid.'
        )
        self.assertEqual(
            env.get('kapowarr_health_issue_type'),
            'ComicVineApiKey')

    def test_custom_script_timeout_logs_warning(self):
        import subprocess
        settings = {'path': '/test/slow.sh'}
        provider = CustomScriptProvider()

        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(
            cmd='/test/slow.sh', timeout=60
        )):
            with patch(
                'backend.implementations.notification_providers'
                '.custom_script.LOGGER'
            ) as mock_log:
                provider.on_download(make_download_event(), settings)
                mock_log.warning.assert_called_once()
                self.assertIn('timed out', mock_log.warning.call_args[0][0])

    def test_custom_script_validate_settings_raises_on_missing_path(self):
        from backend.base.custom_exceptions import InvalidNotificationSettings
        provider = CustomScriptProvider()
        with self.assertRaises(InvalidNotificationSettings):
            provider.validate_settings({})
        with self.assertRaises(InvalidNotificationSettings):
            provider.validate_settings({'path': ''})


# ---------------------------------------------------------------------------
# WebhookProvider tests
# ---------------------------------------------------------------------------

class TestWebhookPayload(unittest.TestCase):

    def _send_with_capture(
        self,
        provider,
        event_method,
        event,
        url='https://example.com/hook'):
        settings = {'url': url, 'method': 'POST', 'headers': {}}
        captured = {}

        def fake_request(
                method, url, json=None, headers=None,
                auth=None, timeout=None):
            captured['method'] = method
            captured['json'] = json
            captured['headers'] = headers
            captured['auth'] = auth
            r = MagicMock()
            r.status_code = 200
            return r

        with patch('requests.request', side_effect=fake_request):
            getattr(provider, event_method)(event, settings)

        return captured

    def test_webhook_builds_payload_on_download(self):
        provider = WebhookProvider()
        event = make_download_event()
        result = self._send_with_capture(provider, 'on_download', event)

        payload = result['json']
        self.assertEqual(payload['eventType'], 'Download')
        self.assertEqual(payload['instanceName'], 'Kapowarr')
        self.assertEqual(payload['volume']['id'], 1)
        self.assertEqual(payload['volume']['title'], 'Batman')
        self.assertEqual(payload['issue']['number'], '5')
        self.assertEqual(payload['issue']['title'], 'The Dark Knight')
        self.assertEqual(
            payload['file']['path'],
            '/comics/batman/batman_005.cbz')
        self.assertEqual(payload['downloadSource'], 'getcomics')
        self.assertFalse(payload['isUpgrade'])

    def test_webhook_builds_payload_on_volume_add(self):
        provider = WebhookProvider()
        event = make_volume_add_event()
        result = self._send_with_capture(provider, 'on_volume_add', event)

        payload = result['json']
        self.assertEqual(payload['eventType'], 'VolumeAdd')
        self.assertEqual(payload['volume']['id'], 2)
        self.assertEqual(payload['volume']['title'], 'Spider-Man')
        self.assertEqual(payload['volume']['publisher'], 'Marvel')

    def test_webhook_non_2xx_logged(self):
        provider = WebhookProvider()
        settings = {
            'url': 'https://example.com/hook',
            'method': 'POST',
            'headers': {}}

        def fake_request(**kwargs):
            r = MagicMock()
            r.status_code = 500
            return r

        with patch('requests.request', side_effect=fake_request):
            with patch(
                'backend.implementations.notification_providers.webhook.LOGGER'
            ) as mock_log:
                provider.on_download(make_download_event(), settings)
                mock_log.warning.assert_called_once()

    def test_webhook_validate_settings_raises_on_invalid_url(self):
        from backend.base.custom_exceptions import InvalidNotificationSettings
        provider = WebhookProvider()
        with self.assertRaises(InvalidNotificationSettings):
            provider.validate_settings({})
        with self.assertRaises(InvalidNotificationSettings):
            provider.validate_settings({'url': 'ftp://bad'})

    def test_webhook_validate_settings_accepts_http(self):
        provider = WebhookProvider()
        # Should not raise
        provider.validate_settings({'url': 'http://example.com/hook'})
        provider.validate_settings({'url': 'https://example.com/hook'})


# ---------------------------------------------------------------------------
# NotificationService dispatch tests
# ---------------------------------------------------------------------------

class TestNotificationServiceDispatch(unittest.TestCase):

    def _make_connection(self, id, provider_type):
        return {
            'id': id,
            'name': f'conn-{id}',
            'provider_type': provider_type,
            'settings': {'path': f'/script{id}.sh'} if provider_type == 'custom_script'
            else {'url': 'https://example.com'},
        }

    def test_notification_service_dispatches_to_multiple(self):
        """Events should be dispatched to all matching enabled connections."""
        connections = [
            self._make_connection(1, 'custom_script'),
            self._make_connection(2, 'custom_script'),
        ]
        called = []

        class FakeProvider(CustomScriptProvider):
            def on_download(self_, event, settings):
                called.append(settings['path'])

        with patch(
            'backend.internals.db_models_notifications'
            '.NotificationConnection.get_enabled_for_event',
            return_value=connections
        ):
            with patch.dict(
                'backend.features.notifications.provider_registry',
                {'custom_script': FakeProvider}
            ):
                # Call _dispatch directly (sync, no threads)
                service = NotificationService()
                service._dispatch(
                    'on_download', make_download_event(), 'on_download'
                )

        self.assertEqual(len(called), 2)
        self.assertIn('/script1.sh', called)
        self.assertIn('/script2.sh', called)

    def test_notification_service_isolates_errors(self):
        """One provider failure must not prevent other providers from firing."""
        connections = [
            self._make_connection(1, 'custom_script'),
            self._make_connection(2, 'custom_script'),
        ]
        called = []

        class BrokenProvider(CustomScriptProvider):
            def on_download(self_, event, settings):
                if settings['path'] == '/script1.sh':
                    raise RuntimeError('intentional failure')
                called.append(settings['path'])

        with patch(
            'backend.internals.db_models_notifications'
            '.NotificationConnection.get_enabled_for_event',
            return_value=connections
        ):
            with patch.dict(
                'backend.features.notifications.provider_registry',
                {'custom_script': BrokenProvider}
            ):
                service = NotificationService()
                # Should not raise even though conn 1 fails
                service._dispatch(
                    'on_download', make_download_event(), 'on_download'
                )

        self.assertEqual(called, ['/script2.sh'])


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------

class TestHealthCheckRootFolder(unittest.TestCase):
    """
    root_folders.py imports settings.py which imports `grp` (Linux-only).
    To keep health-check tests runnable on Windows we inject a fake module
    into sys.modules instead of patching the real one.
    """

    def _fake_rf_module(self, mock_class):
        """Return a fake backend.implementations.root_folders module."""
        m = MagicMock()
        m.RootFolders = mock_class
        return m

    def test_detects_missing_root_folder(self):
        from backend.features.health_checks import _check_root_folders

        mock_rf = MagicMock()
        mock_rf.return_value.get_folder_list.return_value = [
                                                             '/nonexistent/path']

        with patch.dict('sys.modules', {
            'backend.implementations.root_folders': self._fake_rf_module(mock_rf)
        }):
            with patch('os.path.isdir', return_value=False):
                issues = _check_root_folders()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].check_type, 'RootFolder')
        self.assertEqual(issues[0].level, 'error')
        self.assertIn('does not exist', issues[0].message)

    def test_healthy_root_folder_returns_no_issues(self):
        from backend.features.health_checks import _check_root_folders

        mock_rf = MagicMock()
        mock_rf.return_value.get_folder_list.return_value = ['/comics']

        with patch.dict('sys.modules', {
            'backend.implementations.root_folders': self._fake_rf_module(mock_rf)
        }):
            with patch('os.path.isdir', return_value=True):
                with patch('os.access', return_value=True):
                    issues = _check_root_folders()

        self.assertEqual(issues, [])

    def test_detects_low_disk_space(self):
        from backend.features.health_checks import _check_disk_space

        mock_rf = MagicMock()
        mock_rf.return_value.get_folder_list.return_value = ['/comics']

        mock_usage = MagicMock()
        mock_usage.free = 500_000_000  # 500 MB — below 1 GB threshold

        with patch.dict('sys.modules', {
            'backend.implementations.root_folders': self._fake_rf_module(mock_rf)
        }):
            with patch('os.path.isdir', return_value=True):
                with patch('shutil.disk_usage', return_value=mock_usage):
                    with patch('os.stat') as mock_stat:
                        mock_stat.return_value.st_dev = 1
                        issues = _check_disk_space()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].check_type, 'DiskSpace')
        self.assertEqual(issues[0].level, 'warning')


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

class TestProviderRegistry(unittest.TestCase):

    def test_custom_script_registered(self):
        self.assertIn('custom_script', provider_registry)
        self.assertIs(provider_registry['custom_script'], CustomScriptProvider)

    def test_webhook_registered(self):
        self.assertIn('webhook', provider_registry)
        self.assertIs(provider_registry['webhook'], WebhookProvider)


if __name__ == '__main__':
    unittest.main()
