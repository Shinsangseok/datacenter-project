"""DEV non-admin clients may read only; MLflow still enforces resource grants.

The official authorization_function extension is also bridged to native FastAPI
artifact routes by the reviewed MLflow build. Authenticate first so missing or
invalid credentials retain the normal 401 challenge. Never log request headers.
"""
from flask import Response, request
from werkzeug.datastructures import Authorization


READ_PATHS = frozenset({
    '/api/2.0/mlflow/users/current',
    '/api/2.0/mlflow/registered-models/get',
    '/api/2.0/mlflow/model-versions/get',
    '/api/2.0/mlflow/model-versions/get-download-uri',
    '/api/2.0/mlflow/experiments/get',
    '/api/2.0/mlflow/experiments/get-by-name',
    '/api/2.0/mlflow/runs/get',
    '/api/2.0/mlflow/artifacts/list',
    '/api/2.0/mlflow-artifacts/artifacts',
})


def readable_path(path):
    path = path.rstrip('/')
    return path in READ_PATHS or path.startswith('/api/2.0/mlflow-artifacts/artifacts/')


def authenticate():
    from mlflow.server.auth import authenticate_request_basic_auth, store
    authorization = authenticate_request_basic_auth()
    if isinstance(authorization, Response):
        return authorization
    if not isinstance(authorization, Authorization):
        return Response('Unauthorized', status=401)
    if not store.get_user(authorization.username).is_admin:
        if request.method not in ('GET', 'HEAD') or not readable_path(request.path):
            return Response('Read-only client', status=403)
    return authorization
