"""App DB targets; retain compatibility for existing implicit PROD settings."""
from sqlalchemy.engine import URL

DEV_DATABASES = {'datacenter_app_dev', 'mlflow_tracking_dev', 'mlflow_auth_dev'}
SYSTEM_DATABASES = {'mysql', 'sys', 'information_schema', 'performance_schema'}


def database_url(env):
    mode = env.get('MLFLOW_ENV', 'prod')
    database = env.get('DB_NAME')
    if mode not in {'prod', 'dev'} or not database or database in SYSTEM_DATABASES:
        raise RuntimeError('Application DB configuration rejected')
    if mode == 'dev' and database != 'datacenter_app_dev':
        raise RuntimeError('Application DEV DB target rejected')
    if mode == 'prod' and database in DEV_DATABASES:
        raise RuntimeError('Application PROD DB target rejected')
    if 'MLFLOW_ENV' in env:
        for actual, expected in [('DB_HOST', 'DB_EXPECTED_HOST'),
                                 ('DB_USER', 'DB_EXPECTED_USER'),
                                 ('DB_NAME', 'DB_EXPECTED_NAME')]:
            approved = env.get(expected)
            if not approved or approved.startswith('REPLACE_') or env.get(actual) != approved:
                raise RuntimeError('Application DB target rejected')
        if env.get('DB_PORT', '3306') != '3306' or not env.get('DB_SSL_CA'):
            raise RuntimeError('Application DB TLS/port rejected')
    if not all(env.get(key) for key in ('DB_HOST', 'DB_USER', 'DB_PASSWORD')):
        raise RuntimeError('Application DB credentials missing')
    query = {'charset': 'utf8mb4'}
    if env.get('DB_SSL_CA'):
        query.update(ssl_ca=env['DB_SSL_CA'], ssl_check_hostname='true')
    return URL.create('mysql+pymysql', username=env['DB_USER'], password=env['DB_PASSWORD'],
                      host=env['DB_HOST'], port=int(env.get('DB_PORT', '3306')),
                      database=database, query=query)
