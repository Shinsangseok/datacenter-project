"""Policy B: read-only startup retries with a 120s target initialization budget.

The monotonic budget rejects late results and bounds retry scheduling. It does
not interrupt an in-flight SDK call/native deserialization. Kubernetes probes
and restart policy manage prolonged failures, without an exact exit deadline.
No request-time writes retry. Never log exception text or credentials.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import logging
import math
import os
import socket
import ssl
import time

from pymysql import err as mysql_errors
from pymysql.connections import Connection as PyMySQLConnection
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

LOG = logging.getLogger('backend.startup')


@dataclass(frozen=True)
class Policy:
    max_attempts: int = 3
    budget_seconds: float = 120.0
    base_delay: float = 1.0
    max_delay: float = 4.0

    def __post_init__(self):
        import math
        if (type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3
                or any(not math.isfinite(x) for x in
                       [self.budget_seconds, self.base_delay, self.max_delay])
                or not 0 < self.budget_seconds <= 120
                or not 0 <= self.base_delay <= self.max_delay <= 4):
            raise ValueError('Invalid startup retry policy')


@dataclass(frozen=True)
class Failure:
    category: str
    retryable: bool = False
    phase: str | None = None


class StartupError(RuntimeError):
    """Sanitized public failure; callers must never append the original error."""
    def __init__(self, failure, reason):
        self.failure = failure
        self.reason = reason
        super().__init__('FastAPI startup failed: ' + failure.category + ' (' + reason + ')')


class ReadFailure(Exception):
    def __init__(self, failure):
        self.failure = failure
        super().__init__(failure.category)


def _chain(error):
    """Inspect only typed exception links, never message strings/traceback text."""
    pending = [error]
    seen = set()
    while pending and len(seen) < 24:
        e = pending.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        for name in ('orig', 'original_exception', '__cause__', '__context__', 'reason'):
            child = getattr(e, name, None)
            if isinstance(child, BaseException):
                pending.append(child)


def _network_failure(chain):
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return Failure('tls_rejected')
    for e in chain:
        if isinstance(e, socket.gaierror):
            return Failure('dns_temporary', True) if e.errno == socket.EAI_AGAIN else Failure('dns_rejected')
    if any(isinstance(e, TimeoutError) for e in chain):
        return Failure('network_timeout', True)
    allowed = {errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
               errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH}
    if any(isinstance(e, OSError) and e.errno in allowed for e in chain):
        return Failure('network_unavailable', True)
    return None


def classify_db(error, phase):
    if phase not in {'db_connect', 'db_schema'}:
        return Failure('unknown')
    # Only the deployed PyMySQL driver (possibly wrapped by SQLAlchemy).
    driver = error.orig if isinstance(error, DBAPIError) else error
    if not isinstance(driver, mysql_errors.Error):
        return Failure('db_unknown', phase=phase)
    code = driver.args[0] if driver.args and type(driver.args[0]) is int else None
    if code in {1044, 1045, 1142, 1143, 1227, 1698}:
        return Failure('db_auth_or_permission', phase=phase)
    if code in {1049, 1054, 1146} or isinstance(driver, mysql_errors.ProgrammingError):
        return Failure('db_schema_or_target', phase=phase)
    if isinstance(driver, mysql_errors.OperationalError) and code in {2002, 2003, 2006, 2013}:
        network = _network_failure(list(_chain(driver)))
        if network:
            return Failure(network.category, network.retryable, phase)
    # A naked 2003/2013 or a non-PyMySQL OperationalError is not proof of a transient failure.
    return Failure('db_unknown', phase=phase)


def classify_registry(error, phase):
    from mlflow.exceptions import MlflowException, RestException
    from requests import exceptions as req
    from urllib3 import exceptions as url

    if phase not in {'registry_lookup', 'artifact_download'}:
        return Failure('unknown')
    if not isinstance(error, (MlflowException, req.RequestException)):
        return Failure('registry_unknown')
    chain = list(_chain(error))
    if any(isinstance(e, (ssl.SSLError, req.SSLError, url.SSLError)) for e in chain):
        return Failure('tls_rejected')
    # Permanent failures take precedence over nested transport exceptions.
    for e in chain:
        if isinstance(e, RestException):
            code = e.error_code
            if code in {'UNAUTHENTICATED', 'PERMISSION_DENIED', 'CUSTOMER_UNAUTHORIZED'}:
                return Failure('registry_auth_or_permission')
            if code in {'RESOURCE_DOES_NOT_EXIST', 'NOT_FOUND', 'INVALID_PARAMETER_VALUE', 'BAD_REQUEST'}:
                return Failure('registry_contract')
        if isinstance(e, req.HTTPError) and e.response is not None:
            if e.response.status_code in {401, 403}:
                return Failure('registry_auth_or_permission')
            if 400 <= e.response.status_code < 500:
                return Failure('registry_contract')  # includes 429: no unbounded Retry-After
    for e in chain:
        if isinstance(e, RestException) and e.error_code == 'TEMPORARILY_UNAVAILABLE':
            return Failure('registry_unavailable', True)
        if isinstance(e, req.HTTPError) and e.response is not None and e.response.status_code in {502, 503, 504}:
            return Failure('registry_unavailable', True)
    if any(isinstance(e, (req.Timeout, url.TimeoutError)) for e in chain):
        return Failure('network_timeout', True)
    network = _network_failure(chain)
    return network or Failure('registry_unknown')


class Startup:
    PHASES = {'configuration', 'registry_lookup', 'artifact_download', 'model_load', 'db_contract'}

    def __init__(self, policy=None, *, clock=time.monotonic, sleep=time.sleep):
        self.policy = policy or Policy()
        self.clock, self.sleep = clock, sleep
        self.started = clock()

    def remaining(self):
        return max(0.0, self.policy.budget_seconds - (self.clock() - self.started))

    def timeout(self, cap, *, whole_seconds=False):
        """Snapshot of remaining allowance, NOT cancellation of a running call.

        MLflow's environment timeout is an integer. Never round up or pass zero;
        reject its final fractional second instead.
        """
        if not math.isfinite(cap) or cap <= 0:
            raise ValueError('Invalid startup timeout cap')
        value = min(cap, self.remaining())
        if whole_seconds:
            value = math.floor(value)
        if value <= 0:
            raise StartupError(Failure('startup_budget_exhausted'), 'deadline_exceeded') from None
        return value

    def _event(self, phase, failure, attempt, reason):
        # All values originate in this module/call-site constants, never upstream messages.
        LOG.log(logging.INFO if failure.category == 'none' else logging.WARNING, json.dumps({
            'component': 'fastapi', 'startup_phase': failure.phase or phase,
            'error_category': failure.category, 'retryable': failure.retryable,
            'attempt': attempt, 'max_attempts': self.policy.max_attempts,
            'elapsed_ms': max(0, int((self.clock() - self.started) * 1000)),
            'exit_reason': reason,
        }, sort_keys=True))

    def run(self, phase, operation, classify=None):
        if phase not in self.PHASES:
            raise ValueError('Unsupported startup phase')
        for attempt in range(1, self.policy.max_attempts + 1):
            if not self.remaining():
                failure = Failure('startup_budget_exhausted')
                self._event(phase, failure, attempt, 'deadline_exceeded')
                raise StartupError(failure, 'deadline_exceeded') from None
            try:
                result = operation()
            except StartupError as error:
                # Preserve transport budget guards; never retry or reclassify them.
                self._event(phase, error.failure, attempt, error.reason)
                raise error from None
            except Exception as error:
                if isinstance(error, ReadFailure):
                    failure = error.failure
                elif classify is not None:
                    try:
                        failure = classify(error, phase)
                    except Exception:
                        # A malformed upstream exception must not escape with raw diagnostics.
                        failure = Failure('unknown')
                else:
                    failure = Failure({'configuration': 'configuration_rejected',
                                       'model_load': 'model_integrity_or_contract'}.get(phase, 'unknown'))
                if not failure.retryable:
                    reason = 'permanent_or_unknown'
                elif not self.remaining():
                    reason = 'deadline_exceeded'
                elif attempt == self.policy.max_attempts:
                    reason = 'attempts_exhausted'
                else:
                    delay = min(self.policy.base_delay * 2 ** (attempt - 1), self.policy.max_delay)
                    if delay >= self.remaining():
                        reason = 'deadline_exceeded'
                    else:
                        self._event(phase, failure, attempt, 'retry_scheduled')
                        self.sleep(delay)
                        continue
                self._event(phase, failure, attempt, reason)
                raise StartupError(failure, reason) from None
            if not self.remaining():
                failure = Failure('startup_budget_exhausted')
                self._event(phase, failure, attempt, 'deadline_exceeded')
                raise StartupError(failure, 'deadline_exceeded') from None
            self._event(phase, Failure('none'), attempt, 'phase_ready')
            return result


def configure_database_timeouts(engine, startup):
    """Remaining-budget adapters ONLY for the disposable startup engine.

    SQLAlchemy's public do_connect event preserves dialect/TLS/auth arguments.
    PyMySQL 1.2.0 has no public setters for socket I/O timeout after connecting;
    the schema adapter uses its two pinned private fields. Fail closed for an
    incompatible driver. Neither adapter bounds DNS or all reads in a query.
    """
    def before_connect(dialect, connection_record, cargs, cparams):
        timeout = startup.timeout(10)
        cparams.update(connect_timeout=min(3, timeout),
                       read_timeout=timeout, write_timeout=timeout)

    def before_query(connection, cursor, statement, parameters, context, executemany):
        timeout = startup.timeout(10)
        driver = connection.connection.driver_connection
        if (not isinstance(driver, PyMySQLConnection)
                or not hasattr(driver, '_read_timeout')
                or not hasattr(driver, '_write_timeout')):
            raise StartupError(Failure('db_timeout_adapter_incompatible'),
                               'permanent_or_unknown') from None
        driver._read_timeout = timeout
        driver._write_timeout = timeout

    event.listen(engine, 'do_connect', before_connect)
    event.listen(engine, 'before_cursor_execute', before_query)
    return engine


def validate_database(engine, startup):
    from backend.schema import validate_measurements

    def attempt():
        try:
            connection = engine.connect()
        except StartupError:
            raise
        except Exception as error:
            raise ReadFailure(classify_db(error, 'db_connect')) from None
        try:
            with connection as active:
                # Connect/dialect initialization may already have spent the budget.
                startup.timeout(10)
                validate_measurements(active)
        except StartupError:
            raise
        except Exception as error:
            raise ReadFailure(classify_db(error, 'db_schema')) from None
    return startup.run('db_contract', attempt)


@contextmanager
def registry_transport(startup):
    """Startup only, before serving requests. One retry owner; restore all state.

    MLflow may log raw upstream exception text. Silence library logging only
    during the call; the controller emits normalized events after this scope.
    No retries/redirects/progress or unbounded Retry-After from the HTTP adapter.
    """
    timeout = startup.timeout(10, whole_seconds=True)
    overrides = {'MLFLOW_HTTP_REQUEST_MAX_RETRIES': '0',
                 'MLFLOW_HTTP_REQUEST_TIMEOUT': str(timeout),
                 'MLFLOW_HTTP_RESPECT_RETRY_AFTER_HEADER': 'false',
                 'MLFLOW_ALLOW_HTTP_REDIRECTS': 'false',
                 'MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR': 'false'}
    saved = {k: os.environ.get(k) for k in overrides}
    disabled = logging.root.manager.disable
    try:
        os.environ.update(overrides)
        logging.disable(logging.CRITICAL)
        yield
    finally:
        logging.disable(disabled)
        for k, value in saved.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value
