"""DEV non-admin clients may read only; MLflow still enforces resource grants.

The official authorization_function extension is also bridged to native FastAPI
artifact routes by the reviewed MLflow build. Authenticate first so missing or
invalid credentials retain the normal 401 challenge. Never log request headers.
"""
from flask import Response, request
from werkzeug.datastructures import Authorization


def authenticate():
    from mlflow.server.auth import authenticate_request_basic_auth, store
    authorization = authenticate_request_basic_auth()
    if isinstance(authorization, Response):
        return authorization
    if not isinstance(authorization, Authorization):
        return Response('Unauthorized', status=401)
    if request.method not in ('GET', 'HEAD'):
        if not store.get_user(authorization.username).is_admin:
            return Response('Read-only client', status=403)
    return authorization
