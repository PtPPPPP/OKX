class ExchangeError(RuntimeError):
    pass


class AuthenticationError(ExchangeError):
    def __init__(self, message: str, *, diagnostic: object | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class RateLimitError(ExchangeError):
    pass


class NetworkError(ExchangeError):
    pass


class RequestTimeout(NetworkError):
    pass


class OrderRejected(ExchangeError):
    pass


class OrderNotFound(ExchangeError):
    pass


class OrderStateUnknown(ExchangeError):
    pass
