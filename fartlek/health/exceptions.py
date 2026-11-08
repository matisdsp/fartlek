class HealthError(Exception):
    """Base for health context errors."""


class GarminAuthError(HealthError):
    """Garmin authentication failed or tokens missing/expired."""


class GarminConnectUnavailable(GarminAuthError):
    """Tokens exist but the optional `garminconnect` dependency is not installed."""


class GarminApiError(HealthError):
    """Garmin API returned an error or unexpected payload."""

    def __init__(self, message: str, *, status: int | None = None, endpoint: str | None = None):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
