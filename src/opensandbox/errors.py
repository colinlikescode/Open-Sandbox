"""Stable errors shared by the API, CLI and SDK."""


class OpenSandboxError(Exception):
    code = "internal_error"
    http_status = 500

    def to_dict(self):
        return {"code": self.code, "message": str(self)}


class ValidationError(OpenSandboxError, ValueError):
    code = "invalid_request"
    http_status = 400


class ConfigurationError(ValidationError):
    code = "configuration_error"


class AuthenticationError(OpenSandboxError):
    code = "unauthorized"
    http_status = 401


class NotFoundError(OpenSandboxError):
    code = "not_found"
    http_status = 404


class ConflictError(OpenSandboxError):
    code = "conflict"
    http_status = 409


class CapacityError(OpenSandboxError):
    code = "capacity_timeout"
    http_status = 503


class RuntimeError(OpenSandboxError):
    code = "runtime_error"
    http_status = 502


class NetworkProxyError(RuntimeError):
    code = "proxy_error"
