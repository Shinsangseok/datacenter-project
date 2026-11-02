"""Bounded DEV verification over a private Unix socket, never the public app.

This module receives only the loaded model and a completed startup-gate latch.
It does not import the main app, a database client, a registry client or metrics.
Kubernetes exec authorization and Unix ownership are the caller boundary; the
activation environment is an additional deployment guard, not authentication.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import math
import multiprocessing
from numbers import Real
import os
from pathlib import Path
import socket
import stat
import sys
import time
from types import MappingProxyType
from typing import Callable, Mapping

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sklearn.ensemble import RandomForestClassifier
from starlette.responses import JSONResponse
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.middleware.message_logger import MessageLoggerMiddleware

from backend.inference import infer

SOCKET_DIRECTORY = Path('/tmp/datacenter-write-free')
SOCKET_PATH = SOCKET_DIRECTORY / 'inference.sock'
EXPECTED_UID = 10001
VERIFICATION_PATH = '/verify/predict'
MAX_REQUESTS = 10
MAX_SECONDS = 600.0
MAX_BODY_BYTES = 4096
BODY_TIMEOUT_SECONDS = 2.0
START_TIMEOUT_SECONDS = 5.0
FEATURES = ['cpu', 'memory', 'temperature', 'power']
ACTIVATION_KEYS = (
    'WRITE_FREE_INFERENCE_ENABLED', 'POD_NAMESPACE', 'MLFLOW_ENV',
    'MODEL_SOURCE', 'WEB_CONCURRENCY', 'UVICORN_WORKERS', 'UVICORN_RELOAD',
)


class VerifierError(RuntimeError):
    """Fixed, non-sensitive configuration or lifecycle failure."""


def _enabled(environment: Mapping[str, str]) -> bool:
    flag = environment.get('WRITE_FREE_INFERENCE_ENABLED')
    if flag in (None, 'false'):
        return False
    if flag != 'true':
        raise VerifierError('invalid write-free activation flag')
    if (environment.get('POD_NAMESPACE') != 'datacenter-app-dev'
            or environment.get('MLFLOW_ENV') != 'dev'
            or environment.get('MODEL_SOURCE') != 'registry'):
        raise VerifierError('write-free verification requires the approved DEV target')
    if any(environment.get(key) not in (None, '1')
           for key in ('WEB_CONCURRENCY', 'UVICORN_WORKERS')):
        raise VerifierError('write-free verification requires a single worker')
    if environment.get('UVICORN_RELOAD') not in (None, 'false'):
        raise VerifierError('write-free verification prohibits reload')
    return True


def _validate_worker() -> None:
    # A second worker must fail before racing to own the socket. The fixed
    # directory additionally refuses duplicate starts in the same Pod.
    if multiprocessing.parent_process() is not None:
        raise VerifierError('write-free verification requires a single process')
    arguments = sys.argv[1:]
    for index, value in enumerate(arguments):
        if value == '--reload' or value.startswith('--reload='):
            raise VerifierError('write-free verification prohibits reload')
        if value in ('--workers', '-w'):
            if index + 1 == len(arguments) or arguments[index + 1] != '1':
                raise VerifierError('write-free verification requires a single worker')
        if value.startswith('--workers=') and value != '--workers=1':
            raise VerifierError('write-free verification requires a single worker')
    if 'gunicorn' in Path(sys.argv[0]).name:
        raise VerifierError('write-free verification requires the approved single server')


def _validate_model(model) -> None:
    # The public API retains its original loader contract. This private path
    # deliberately rejects subclasses and unvalidated local model objects.
    if type(model) is not RandomForestClassifier:
        raise VerifierError('write-free model unavailable')
    if (list(getattr(model, 'feature_names_in_', [])) != FEATURES
            or list(getattr(model, 'classes_', [])) != [0, 1]):
        raise VerifierError('write-free model unavailable')


def _validate_result(result) -> None:
    predictions = np.asarray(result.raw_predictions)
    probabilities = np.asarray(result.raw_probabilities)
    if predictions.shape != (1,) or probabilities.shape != (1, 2):
        raise VerifierError('write-free prediction unavailable')
    label = predictions[0]
    if (isinstance(label, (bool, np.bool_)) or not isinstance(label, Real)
            or not math.isfinite(float(label)) or label not in (0, 1)):
        raise VerifierError('write-free prediction unavailable')
    for value in probabilities[0]:
        if (isinstance(value, (bool, np.bool_)) or not isinstance(value, Real)
                or not math.isfinite(float(value)) or not 0 <= value <= 1):
            raise VerifierError('write-free prediction unavailable')
    if not math.isclose(float(probabilities[0].sum()), 1.0, rel_tol=0, abs_tol=1e-8):
        raise VerifierError('write-free prediction unavailable')


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    cpu: float = Field(ge=0, le=100, allow_inf_nan=False)
    memory: float = Field(ge=0, le=100, allow_inf_nan=False)
    temperature: float = Field(ge=-50, le=150, allow_inf_nan=False)
    power: float = Field(ge=0, le=10000, allow_inf_nan=False)

    @field_validator('cpu', 'memory', 'temperature', 'power', mode='before')
    @classmethod
    def require_json_number(cls, value):
        try:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('finite JSON number required')
        except (OverflowError, TypeError, ValueError):
            raise ValueError('finite JSON number required') from None
        return value


class _RequestBoundary:
    """Count before routing and bound actual body bytes, not declared length."""

    def __init__(self, app, owner):
        self.app = app
        self.owner = owner

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            return
        owner = self.owner
        # These checks/increment contain no await: one event loop owns them.
        owner._requests += 1
        if not owner.running or asyncio.get_running_loop() is not owner._loop:
            await JSONResponse({'detail': 'verification unavailable'}, 503)(scope, receive, send)
            return
        if owner._requests > MAX_REQUESTS or owner._clock() - owner._started_at >= MAX_SECONDS:
            await JSONResponse({'detail': 'verification budget exhausted'}, 429)(scope, receive, send)
            return
        body = bytearray()
        too_large = False
        try:
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    if message['type'] != 'http.request':
                        raise VerifierError('invalid verifier request')
                    chunk = message.get('body', b'')
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        too_large = True
                        break
                    body.extend(chunk)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            await JSONResponse({'detail': 'verification body timeout'}, 408)(scope, receive, send)
            return
        if too_large:
            await JSONResponse({'detail': 'verification body too large'}, 413)(scope, receive, send)
            return
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, bounded_receive, send)


class _PrivateHTTPProtocol(H11Protocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Uvicorn's global access logger belongs to the public server. Disable
        # request logging only on this protocol instance, including query text.
        self.access_log = False


class _PrivateConfig(uvicorn.Config):
    def configure_logging(self):
        # Config(access_log=False) normally removes the *global* access logger
        # handlers. A supplementary listener must not alter public logging.
        pass

    def load(self):
        super().load()
        # A public server using TRACE must not enable scope/header/query logging
        # for this private listener. Its ASGI3 app needs no wrapper or factory.
        if isinstance(self.loaded_app, MessageLoggerMiddleware):
            self.loaded_app = self.loaded_app.app


class _AuxiliaryServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Only the main server owns SIGINT/SIGTERM and application shutdown.
        yield


class WriteFreeVerifier:
    def __init__(self, environ: Mapping[str, str] | None = None):
        source = os.environ if environ is None else environ
        # Never retain DB credentials, upstream credentials or unrelated env.
        self._environment = MappingProxyType({key: source[key] for key in ACTIVATION_KEYS if key in source})
        self.enabled = _enabled(self._environment)
        self._active = False
        self._started_once = False
        self._started_at = 0.0
        self._clock = time.monotonic
        self._requests = 0
        self._loop = None
        self._pid = None
        self._model = None
        self._model_provider: Callable | None = None
        self._ready_provider: Callable | None = None
        self._inference_task = None
        self._server = None
        self._server_task = None
        self._socket = None
        self._directory_fd = None
        self._directory_identity = None
        self._socket_identity = None
        self.app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None,
                           swagger_ui_oauth2_redirect_url=None, redirect_slashes=False)
        self.app.add_middleware(_RequestBoundary, owner=self)

        @self.app.exception_handler(RequestValidationError)
        async def invalid_request(request, error):
            # Validation details can contain an entire input payload.
            return JSONResponse({'detail': 'invalid verifier request'}, status_code=422)

        self.app.add_api_route(VERIFICATION_PATH, self._verify, methods=['POST'])

    @property
    def requests_seen(self) -> int:
        return self._requests

    @property
    def inference_active(self) -> bool:
        return self._inference_task is not None and not self._inference_task.done()

    @property
    def running(self) -> bool:
        return (self._active and self._pid == os.getpid()
                and self._server_task is not None and not self._server_task.done())

    def _current_model(self):
        try:
            if self._ready_provider() is not True:
                raise VerifierError('verification unavailable')
            model = self._model_provider()
            if model is not self._model:
                raise VerifierError('verification unavailable')
            _validate_model(model)
            return model
        except Exception:
            raise VerifierError('verification unavailable') from None

    def _calculate(self, model, request):
        result = infer(model, request.cpu, request.memory, request.temperature, request.power)
        _validate_result(result)
        return {'mode': 'write_free', 'persisted': False,
                'prediction': result.prediction, 'probability': round(result.raw_probability, 5)}

    def _calculation_finished(self, task):
        # Shielded work survives HTTP cancellation. Release only on actual
        # completion of the thread, including a late exception.
        if self._inference_task is task:
            self._inference_task = None
        if not task.cancelled():
            task.exception()

    async def _verify(self, request: VerificationRequest):
        # Body receipt can cross the lease boundary or overlap shutdown.
        if not self.running:
            raise HTTPException(503, 'verification unavailable')
        if self._clock() - self._started_at >= MAX_SECONDS:
            raise HTTPException(429, 'verification budget exhausted')
        try:
            model = self._current_model()
        except Exception:
            raise HTTPException(503, 'verification unavailable') from None
        if self.inference_active:
            raise HTTPException(429, 'verification already active')
        task = asyncio.create_task(asyncio.to_thread(self._calculate, model, request))
        self._inference_task = task
        task.add_done_callback(self._calculation_finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not cancel the underlying RF calculation or free its slot.
            raise
        except Exception:
            raise HTTPException(503, 'verification unavailable') from None

    @staticmethod
    def _identity(information):
        return information.st_dev, information.st_ino, information.st_uid

    def _bind(self):
        if os.geteuid() != EXPECTED_UID:
            raise VerifierError('write-free verification requires the application UID')
        if SOCKET_PATH.parent != SOCKET_DIRECTORY or SOCKET_PATH.name != 'inference.sock':
            raise VerifierError('invalid verifier socket configuration')
        # Never adopt an existing directory, including an empty/stale one.
        SOCKET_DIRECTORY.mkdir(mode=0o700)
        info = SOCKET_DIRECTORY.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != EXPECTED_UID
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise VerifierError('invalid verifier directory ownership')
        self._directory_identity = self._identity(info)
        self._directory_fd = os.open(SOCKET_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if self._identity(os.fstat(self._directory_fd)) != self._directory_identity:
            raise VerifierError('verifier directory changed')
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(str(SOCKET_PATH))
        info = os.stat(SOCKET_PATH.name, dir_fd=self._directory_fd, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != EXPECTED_UID:
            raise VerifierError('invalid verifier socket ownership')
        self._socket_identity = self._identity(info)
        os.chmod(SOCKET_PATH.name, 0o600, dir_fd=self._directory_fd, follow_symlinks=False)
        info = os.stat(SOCKET_PATH.name, dir_fd=self._directory_fd, follow_symlinks=False)
        if (self._identity(info) != self._socket_identity or stat.S_IMODE(info.st_mode) != 0o600
                or self._identity(SOCKET_DIRECTORY.lstat()) != self._directory_identity):
            raise VerifierError('verifier socket changed')
        # chmod/ownership validation happen before asyncio/uvicorn can listen.
        self._socket.setblocking(False)

    def _cleanup(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._directory_fd is not None:
            try:
                if self._socket_identity is not None:
                    try:
                        info = os.stat(SOCKET_PATH.name, dir_fd=self._directory_fd, follow_symlinks=False)
                        if self._identity(info) == self._socket_identity and stat.S_ISSOCK(info.st_mode):
                            os.unlink(SOCKET_PATH.name, dir_fd=self._directory_fd)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(self._directory_fd)
                self._directory_fd = None
        if self._directory_identity is not None:
            try:
                if self._identity(SOCKET_DIRECTORY.lstat()) == self._directory_identity:
                    SOCKET_DIRECTORY.rmdir()
            except (FileNotFoundError, OSError):
                # Never remove other contents or a replacement directory.
                pass

    async def start(self, model_provider: Callable, ready_provider: Callable):
        if not self.enabled:
            return
        if self._started_once:
            raise VerifierError('write-free verification cannot be restarted')
        self._started_once = True
        try:
            _validate_worker()
            if ready_provider() is not True:
                raise VerifierError('write-free verification requires the completed startup gate')
            model = model_provider()
            _validate_model(model)
            self._model = model
            self._model_provider = model_provider
            self._ready_provider = ready_provider
            self._loop = asyncio.get_running_loop()
            self._pid = os.getpid()
            self._bind()
            configuration = _PrivateConfig(
                self.app, host=None, port=0, workers=1, reload=False,
                log_config=None, log_level=None, access_log=False,
                proxy_headers=False, server_header=False, date_header=False,
                lifespan='off', http=_PrivateHTTPProtocol, ws='none', backlog=4,
                interface='asgi3',
                timeout_keep_alive=2, timeout_graceful_shutdown=5,
                h11_max_incomplete_event_size=MAX_BODY_BYTES,
            )
            self._server = _AuxiliaryServer(configuration)
            self._server_task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                while not self._server.started:
                    if self._server_task.done():
                        self._server_task.result()
                        raise VerifierError('write-free server initialization failed')
                    await asyncio.sleep(0.01)
            self._started_at = self._clock()
            self._active = True
        except BaseException as error:
            await self.stop()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise VerifierError('write-free server initialization failed') from None

    async def stop(self):
        self._active = False
        if self._server is not None:
            self._server.should_exit = True
        try:
            if self._server_task is not None:
                try:
                    await asyncio.shield(self._server_task)
                except (Exception, asyncio.CancelledError):
                    # Cleanup below still drains any shielded computation.
                    pass
            task = self._inference_task
            if task is not None:
                try:
                    await asyncio.shield(task)
                except Exception:
                    pass
        finally:
            self._cleanup()
