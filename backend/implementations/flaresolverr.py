# -*- coding: utf-8 -*-

from __future__ import annotations

from asyncio import Semaphore
from asyncio import TimeoutError as AsyncTimeoutError
from datetime import datetime, timezone
from time import time
from typing import TYPE_CHECKING, Any, Dict, Mapping, Tuple, Union
from urllib.parse import urlparse

from aiohttp import ClientError
from requests import RequestException, Session as PlainSession

from backend.base.definitions import Constants, ProxyType, StatusType
from backend.base.helpers import Session
from backend.base.logging import LOGGER
from backend.internals.settings import Settings
from backend.internals.status import StatusHandlers

if TYPE_CHECKING:
    from backend.base.helpers import AsyncSession


FAILED_SOLVE_COOLDOWN = 3600 # seconds
"How long to skip a server after FlareSolverr failed to solve its challenge"


class FSCache:
    cookie_mapping: Dict[str, Tuple[str, float]] = {}
    ua_mapping: Dict[str, str] = {}
    failed_until: Dict[str, float] = {}
    "Domain to the time until which FlareSolverr isn't asked for it again"

    @classmethod
    def is_cooling_down(cls, url: str) -> bool:
        """Whether FlareSolverr recently failed to solve the challenge of the
        URL's domain, so asking again would only waste time."""
        return time() < cls.failed_until.get(urlparse(url).netloc, 0.0)

    @classmethod
    def mark_failed(cls, url: str, reason: str) -> None:
        """Skip the URL's domain for `FAILED_SOLVE_COOLDOWN` seconds."""
        domain = urlparse(url).netloc
        cls.failed_until[domain] = time() + FAILED_SOLVE_COOLDOWN
        LOGGER.warning(
            "FlareSolverr couldn't solve the challenge of %s (%s). Skipping"
            " it for %d minutes",
            domain, reason, FAILED_SOLVE_COOLDOWN // 60
        )

    @staticmethod
    def _build_cookie(cookie: Dict[str, Any]) -> str:
        result = f"{cookie['value']}; SameSite={cookie['sameSite']}; Partitioned"
        if cookie["httpOnly"]:
            result += "; HttpOnly"
        if cookie["secure"]:
            result += "; Secure"
        if cookie["path"]:
            result += f"; Path={cookie['path']}"
        if cookie["domain"]:
            result += f"; Domain={cookie['domain'].lstrip('.')}"
        if cookie["expiry"]:
            timestamp = datetime.fromtimestamp(
                cookie["expiry"], tz=timezone.utc
            )
            result += f"; Expires={timestamp.strftime('%a, %d %b %Y %H:%M:%S GMT')}"
        return result

    @classmethod
    def get_ua_cookies(cls, url: str) -> Tuple[str, str]:
        """Get the user agent and cookies for a certain URL. The UA and cookies
        can be cleared by CF, so use them to avoid challenges. In case the URL
        is not CF protected, or hasn't explicitly been cleared yet, then the
        default UA is returned and no cookie definitions.

        Args:
            url (str): The URL to get the UA and cookie for.

        Returns:
            Tuple[str, str]: First element is the UA, or default UA. Second
                element is the clearance cookie value.
        """
        domain = urlparse(url).netloc

        ua = cls.ua_mapping.get(domain, Constants.DEFAULT_USERAGENT)
        cookie = cls.cookie_mapping.get(domain, ('', 0.0))

        if cookie[0] and time() > cookie[1]:
            # Expired
            cls.ua_mapping.pop(domain, None)
            cls.cookie_mapping.pop(domain, None)
            ua = Constants.DEFAULT_USERAGENT
            cookie = ('', 0.0)

        return (ua, cookie[0])

    @classmethod
    def forget(cls, url: str) -> None:
        """Drop the cached UA and clearance cookie of the URL's domain."""
        domain = urlparse(url).netloc
        cls.ua_mapping.pop(domain, None)
        cls.cookie_mapping.pop(domain, None)
        return

    @classmethod
    def set_ua_cookies(cls, url: str, fs_response: Dict[str, Any]) -> None:
        """Cache the user agent and cookies for CF clearance.

        Args:
            url (str): The URL that the clearance is for.
            fs_response (Dict[str, Any]): The response from FS.
        """
        domain = urlparse(url).netloc
        for cookie in fs_response["cookies"]:
            if cookie["name"] == "cf_clearance":
                cls.ua_mapping[domain] = fs_response["userAgent"]
                cls.cookie_mapping[domain] = (
                    cls._build_cookie(cookie),
                    cookie["expiry"]
                )
                break

        return


class FlareSolverr:
    def __init__(self) -> None:
        settings = Settings().sv
        self.session_semaphore: Union[Semaphore, None] = None

        self.base_url = settings.flaresolverr_base_url or None

        self.proxy_data: Union[Dict[str, Any], None] = None
        if settings.proxy_type != ProxyType.NONE:
            self.proxy_data = {
                "proxy": {
                    "url": "%s://%s:%d" % (
                        settings.proxy_type.value.rstrip('h'),
                        settings.proxy_host, settings.proxy_port
                    )
                }
            }
            if settings.proxy_username and settings.proxy_password:
                self.proxy_data["proxy"]["username"] = settings.proxy_username
                self.proxy_data["proxy"]["password"] = settings.proxy_password

        return

    @staticmethod
    def __api_request(
        base_url: str,
        session: PlainSession,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        return session.post(
            base_url + Constants.FS_API_BASE,
            json=data,
            headers={'Content-Type': 'application/json'},
            timeout=Constants.REQUEST_TIMEOUT + Constants.FS_RESOLVE_TIMEOUT
        ).json()

    @staticmethod
    async def __async_api_request(
        base_url: str,
        session: AsyncSession,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await (await session.post(
            base_url + Constants.FS_API_BASE,
            json=data,
            headers={'Content-Type': 'application/json'},
            timeout=Constants.REQUEST_TIMEOUT + Constants.FS_RESOLVE_TIMEOUT,
            retry=False
        )).json()

    @staticmethod
    def test_flaresolverr(base_url: str) -> bool:
        """Test the connection to a FlareSolverr instance.

        Args:
            base_url (str): The base URL of the FlareSolverr instance. Supply
                base URL without API extension.

        Returns:
            bool: Whether the connection was successful.
        """
        with Session() as session:
            try:
                result = session.get(f"{base_url}/health")

                if result.status_code != 200:
                    return False

                result = result.json()
                if result.get("status") != "ok":
                    return False

            except RequestException:
                return False
        return True

    def is_enabled(self) -> bool:
        """Check if FlareSolverr is enabled.

        Returns:
            bool: Whether FlareSolverr is enabled.
        """
        return self.base_url is not None

    def get_clearance(self, url: str) -> Tuple[str, str]:
        """Get the user agent and clearance cookie value to send to the site
        of a URL. A site that CF cleared earlier gets the UA and cookie that
        passed the challenge, so it isn't challenged again. Otherwise the
        default UA and an empty cookie are returned.

        Args:
            url (str): The URL to get the UA and cookie for.

        Returns:
            Tuple[str, str]: The UA and the value of the cf_clearance cookie.
        """
        ua, cookie = FSCache.get_ua_cookies(url)
        return ua, cookie.split(';', 1)[0]

    @staticmethod
    def forget_clearance(url: str) -> None:
        """Forget the clearance of the URL's site, as CF rejected it, so the
        next challenge is solved again.

        Args:
            url (str): The URL whose clearance was rejected.
        """
        FSCache.forget(url)
        return

    def __should_solve(self, url: str, headers: Mapping[str, str]) -> bool:
        if (
            headers.get(Constants.CF_CHALLENGE_HEADER[0])
            != Constants.CF_CHALLENGE_HEADER[1]
        ):
            # Request not failed because of CF block
            return False

        if not self.base_url:
            LOGGER.warning(
                "Request blocked by CloudFlare and FlareSolverr not setup"
            )
            StatusHandlers().report(StatusType.CF_CHALLENGE_WITH_NO_FS, '')
            return False

        return not FSCache.is_cooling_down(url)

    def __solve_request(self, url: str) -> Dict[str, Any]:
        # Solve the site's front page instead of the URL itself: when the URL
        # is a file, the browser downloads it instead of showing a page, so
        # FlareSolverr never sees the challenge finish. The clearance cookie
        # is valid for the whole site.
        parsed = urlparse(url)
        return {
            'cmd': 'request.get',
            'url': f'{parsed.scheme}://{parsed.netloc}/',
            'maxTimeout': Constants.FS_RESOLVE_TIMEOUT * 1000,
            **(self.proxy_data or {})
        }

    @staticmethod
    def __store_clearance(url: str, solution: Dict[str, Any]) -> bool:
        if not any(
            c["name"] == "cf_clearance" for c in solution.get("cookies", [])
        ):
            FSCache.mark_failed(url, 'no clearance cookie in the solution')
            return False

        FSCache.set_ua_cookies(url, solution)
        return True

    def handle_cf_block(
        self,
        url: str,
        headers: Mapping[str, str]
    ) -> bool:
        """Let FS clear the site of a URL that got a CF challenge. The UA and
        cookie to retry the request with are then available using
        `get_clearance()`.

        Args:
            url (str): The URL that got the challenge.
            headers (Mapping[str, str]): The response headers from the
                (possibly) blocked request.

        Returns:
            bool: Whether the site was cleared, so the request can be retried.
        """
        if not self.__should_solve(url, headers):
            return False

        if self.get_clearance(url)[1]:
            # The site was cleared before, but the request didn't carry the
            # clearance, e.g. because it was redirected from another site
            return True

        try:
            # A plain session: retrying a failed solve only wastes time
            with PlainSession() as session:
                solution = self.__api_request(
                    self.base_url or '', session, self.__solve_request(url)
                )["solution"]

        except (RequestException, ValueError, KeyError) as e:
            FSCache.mark_failed(url, repr(e))
            return False

        return self.__store_clearance(url, solution)

    async def handle_cf_block_async(
        self,
        session: AsyncSession,
        url: str,
        headers: Mapping[str, str]
    ) -> bool:
        """Let FS clear the site of a URL that got a CF challenge. The UA and
        cookie to retry the request with are then available using
        `get_clearance()`.

        Args:
            session (AsyncSession): The session to make the request to FS with.
            url (str): The URL that got the challenge.
            headers (Mapping[str, str]): The response headers from the
                (possibly) blocked request.

        Returns:
            bool: Whether the site was cleared, so the request can be retried.
        """
        if not self.__should_solve(url, headers):
            return False

        if self.get_clearance(url)[1]:
            # The site was cleared before, but the request didn't carry the
            # clearance, e.g. because it was redirected from another site
            return True

        # Technically this makes it a max amount of FS sessions per AsyncSession
        # instance, but in most cases the application only has one AsyncSession
        # running at any point in time.
        if self.session_semaphore is None:
            self.session_semaphore = Semaphore(
                Constants.MAX_CONCURRENT_FS_SESSIONS
            )

        try:
            async with self.session_semaphore:
                # Another request may have cleared the site, or failed to,
                # while this one waited for its turn
                if self.get_clearance(url)[1]:
                    return True
                if FSCache.is_cooling_down(url):
                    return False

                solution = (await self.__async_api_request(
                    self.base_url or '', session, self.__solve_request(url)
                ))["solution"]

        except (
            ClientError, TimeoutError, AsyncTimeoutError, ValueError, KeyError
        ) as e:
            FSCache.mark_failed(url, repr(e))
            return False

        return self.__store_clearance(url, solution)
