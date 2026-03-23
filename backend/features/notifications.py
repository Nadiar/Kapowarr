# -*- coding: utf-8 -*-

"""
Notification system: event dataclasses, provider ABC, and dispatch service.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Thread
from typing import TYPE_CHECKING, Dict, List, Type, Union

from backend.base.helpers import Singleton
from backend.base.logging import LOGGER

if TYPE_CHECKING:
    pass


# region Event dataclasses

@dataclass
class DownloadEvent:
    volume_id: int
    volume_title: str
    volume_year: int
    volume_comicvine_id: int
    volume_path: str
    issue_id: Union[int, None]
    issue_number: str
    issue_title: str
    file_path: str
    download_source: str
    is_upgrade: bool


@dataclass
class VolumeAddEvent:
    volume_id: int
    volume_title: str
    volume_year: int
    volume_comicvine_id: int
    volume_path: str
    publisher: str


@dataclass
class HealthCheckEvent:
    level: str          # 'warning' or 'error'
    message: str
    check_type: str     # e.g. 'ComicVineApiKey', 'RootFolder'


@dataclass
class ApplicationUpdateEvent:
    previous_version: str
    new_version: str
    message: str


@dataclass
class TestEvent:
    pass


# region Provider ABC

class NotificationProvider(ABC):
    """Abstract base class for all notification providers."""

    @abstractmethod
    def on_download(
        self, event: DownloadEvent, settings: Dict
    ) -> None: ...

    @abstractmethod
    def on_volume_add(
        self, event: VolumeAddEvent, settings: Dict
    ) -> None: ...

    @abstractmethod
    def on_health_check(
        self, event: HealthCheckEvent, settings: Dict
    ) -> None: ...

    @abstractmethod
    def on_application_update(
        self, event: ApplicationUpdateEvent, settings: Dict
    ) -> None: ...

    @abstractmethod
    def on_test(self, event: TestEvent, settings: Dict) -> None: ...

    @abstractmethod
    def validate_settings(self, settings: Dict) -> None:
        """Validate provider-specific settings.

        Raises:
            InvalidNotificationSettings: Settings are invalid.
        """
        ...


# Provider registry: maps provider_type string -> provider class
provider_registry: Dict[str, Type[NotificationProvider]] = {}


# region Notification Service

class NotificationService(metaclass=Singleton):
    """Central service that dispatches events to registered providers.

    Note: Singleton
    """

    def _dispatch_async(
        self,
        event_flag: str,
        event,
        method_name: str
    ) -> None:
        """Spawn a background thread to dispatch an event."""
        from backend.internals.server import Server

        def _run() -> None:
            self._dispatch(event_flag, event, method_name)

        t = Server().get_db_thread(
            target=_run,
            name=f'notification-{method_name}'
        )
        t.daemon = True
        t.start()

    def _dispatch(
        self,
        event_flag: str,
        event,
        method_name: str
    ) -> None:
        """Dispatch an event to all enabled matching connections."""
        from backend.internals.db_models_notifications import \
            NotificationConnection

        try:
            connections = NotificationConnection.get_enabled_for_event(
                event_flag
            )
        except Exception:
            LOGGER.exception(
                'Failed to fetch notification connections for %s',
                event_flag
            )
            return

        for conn in connections:
            provider_type = conn['provider_type']
            if provider_type not in provider_registry:
                LOGGER.warning(
                    'Unknown notification provider type: %s (connection %d)',
                    provider_type, conn['id']
                )
                continue
            try:
                provider = provider_registry[provider_type]()
                getattr(provider, method_name)(event, conn['settings'])
            except Exception:
                LOGGER.exception(
                    'Notification provider %s (connection %d "%s") '
                    'failed for event %s',
                    provider_type, conn['id'], conn['name'], method_name
                )
        return

    def notify_download(self, event: DownloadEvent) -> None:
        """Dispatch a download-complete event asynchronously."""
        self._dispatch_async('on_download', event, 'on_download')

    def notify_volume_add(self, event: VolumeAddEvent) -> None:
        """Dispatch a volume-added event asynchronously."""
        self._dispatch_async('on_volume_add', event, 'on_volume_add')

    def notify_health_check(self, event: HealthCheckEvent) -> None:
        """Dispatch a health-check event asynchronously."""
        self._dispatch_async('on_health_check', event, 'on_health_check')

    def notify_application_update(
        self, event: ApplicationUpdateEvent
    ) -> None:
        """Dispatch an application-update event asynchronously."""
        self._dispatch_async(
            'on_application_update', event, 'on_application_update'
        )

    def send_test(self, connection_id: int) -> None:
        """Send a test event to a single specific connection.

        Args:
            connection_id (int): The connection to test.

        Raises:
            NotificationNotFound: Connection not found.
            InvalidNotificationSettings: Provider raised on test.
            Exception: Any provider error is re-raised so the API can report it.
        """
        from backend.internals.db_models_notifications import \
            NotificationConnection

        conn = NotificationConnection.get_one(connection_id)
        provider_type = conn['provider_type']
        if provider_type not in provider_registry:
            from backend.base.custom_exceptions import \
                InvalidNotificationSettings
            raise InvalidNotificationSettings(
                f'Unknown provider type: {provider_type}'
            )

        provider = provider_registry[provider_type]()
        provider.on_test(TestEvent(), conn['settings'])


# Import providers so they register themselves
def _import_providers() -> None:
    """Import provider modules to trigger registration in provider_registry."""
    from backend.implementations.notification_providers import (  # noqa: F401
        custom_script, webhook)


_import_providers()
