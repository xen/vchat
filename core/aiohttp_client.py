import aiohttp
from aiohttp import (
    BaseConnector,
    ClientError,
    ClientResponseError,
    ClientSession,
    InvalidURL,
)

__all__ = [
    "client_session",
    "ClientError",
    "ClientResponseError",
    "ClientSession",
    "BaseConnector",
    "ApiClient",
    "InvalidURL",
]


def client_session(*, client_timeout=15, **kwargs):
    return ClientSession(
        timeout=aiohttp.ClientTimeout(total=client_timeout),
        **kwargs,
    )


class ApiClient:
    def __init__(
        self,
        session: ClientSession,
    ):
        """
        An accessor to somekind of external API. Splits the resource
        management from the actual data retrieval. Does not act as a
        context manager. The client session is expected to be managed
        by the caller.
        """
        super().__init__()
        self.session = session
