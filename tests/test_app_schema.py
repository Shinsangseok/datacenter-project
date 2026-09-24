"""Offline schema gates: mocked SQL only; never initialize a real database."""
import unittest
from unittest.mock import MagicMock, patch
from sqlalchemy.pool import NullPool
from backend.db_config import database_url
from backend import initialize_schema as init

ENV = dict(MLFLOW_ENV='dev', DB_HOST='rds.example.invalid', DB_PORT='3306',
           DB_NAME='datacenter_app_dev', DB_USER='existing_fixture', DB_PASSWORD='synthetic-only',
           DB_SSL_CA='/fixture/ca.pem', DB_EXPECTED_HOST='rds.example.invalid',
           DB_EXPECTED_USER='existing_fixture', DB_EXPECTED_NAME='datacenter_app_dev')
POLICY = dict(expected_host=ENV['DB_HOST'], expected_user=ENV['DB_USER'],
              expected_database=ENV['DB_NAME'], tls_required=True)


class SchemaTests(unittest.TestCase):
    def test_runtime_is_select_only_and_sanitizes_failure(self):
        from backend import main
        with patch.object(main, 'engine') as engine:
            main.validate_database_schema()
            conn = engine.connect.return_value.__enter__.return_value
            queries = [str(c.args[0]) for c in conn.execute.call_args_list]
            self.assertEqual(len(queries), 1)
            self.assertTrue(queries[0].startswith('SELECT '))
            self.assertIn('LIMIT 0', queries[0])
            conn.execute.side_effect = RuntimeError('private connection details')
            with self.assertRaisesRegex(RuntimeError, '^Application schema validation failed; initialization required$'):
                main.validate_database_schema()
            engine.begin.assert_not_called()

    def test_explicit_dev_and_prod_require_independent_targets_and_tls(self):
        for mode, name in [('dev', 'datacenter_app_dev'), ('prod', 'app_production_fixture')]:
            env = dict(ENV, MLFLOW_ENV=mode, DB_NAME=name, DB_EXPECTED_NAME=name)
            self.assertEqual(database_url(env).database, name)
            for key in ['DB_EXPECTED_HOST', 'DB_EXPECTED_USER', 'DB_EXPECTED_NAME', 'DB_SSL_CA']:
                bad = dict(env); bad.pop(key)
                with self.subTest(mode=mode, key=key), self.assertRaises(RuntimeError):
                    database_url(bad)
            for key, value in [('DB_HOST', 'wrong.example.invalid'), ('DB_USER', 'wrong'),
                               ('DB_NAME', 'mysql'), ('DB_PORT', '3307')]:
                with self.subTest(mode=mode, key=key), self.assertRaises(RuntimeError):
                    database_url(dict(env, **{key: value}))

    def test_implicit_prod_compatibility_and_cross_environment_rejection(self):
        env = {k: v for k, v in ENV.items() if k != 'MLFLOW_ENV'}
        self.assertEqual(database_url(dict(env, DB_NAME='legacy_app_fixture')).database, 'legacy_app_fixture')
        for name in ['datacenter_app_dev', 'mlflow_tracking_dev', 'mlflow_auth_dev']:
            with self.assertRaises(RuntimeError): database_url(dict(env, DB_NAME=name))
        with self.assertRaises(RuntimeError):
            database_url(dict(ENV, DB_NAME='prod_fixture', DB_EXPECTED_NAME='prod_fixture'))

    def test_init_bad_policy_or_target_never_connects(self):
        cases = [(dict(ENV, MLFLOW_ENV='prod'), POLICY),
                 (dict(ENV, DB_NAME='prod_fixture'), POLICY),
                 (dict(ENV, DB_PORT='3307'), POLICY),
                 (ENV, dict(POLICY, expected_host='wrong.example.invalid')),
                 (ENV, dict(POLICY, expected_user='wrong')),
                 (ENV, dict(POLICY, tls_required=False))]
        for env, policy in cases:
            with self.subTest(env=env['MLFLOW_ENV']), patch.object(init, 'create_engine') as create:
                with self.assertRaises(RuntimeError): init.initialize(env, policy)
                create.assert_not_called()

    def run_fake(self, objects, *, database='datacenter_app_dev', tls=True, extra_objects=0, fail_ddl=False):
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value
        queries = []
        def execute(statement):
            sql = str(statement); queries.append(sql)
            result = MagicMock()
            if sql == 'SELECT DATABASE()': result.scalar_one.return_value = database
            elif 'Ssl_cipher' in sql: result.one.return_value = ('Ssl_cipher', 'TLS' if tls else '')
            elif 'information_schema.TABLES' in sql: result.all.return_value = objects
            elif 'SELECT COUNT(*)' in sql: result.scalar_one.return_value = extra_objects
            elif 'CREATE TABLE' in sql and fail_ddl: raise RuntimeError('synthetic failure')
            return result
        conn.execute.side_effect = execute
        with patch.object(init, 'create_engine', return_value=engine) as create:
            try:
                result = init.initialize(ENV, POLICY)
                return result, queries
            finally:
                engine.dispose.assert_called_once()
                create.assert_called_once()
                self.assertIs(create.call_args.kwargs['poolclass'], NullPool)
                self.last_queries = queries

    def test_empty_creates_once_existing_only_verifies(self):
        result, queries = self.run_fake([])
        self.assertEqual(result, 'created')
        self.assertEqual(sum('CREATE TABLE' in q for q in queries), 1)
        result, queries = self.run_fake([('measurements', 'BASE TABLE')])
        self.assertEqual(result, 'verified_existing')
        self.assertFalse(any('CREATE TABLE' in q for q in queries))

    def test_wrong_database_tls_or_partial_schema_cannot_write(self):
        for objects, options in [([], {'database': 'wrong'}), ([], {'tls': False}),
                                 ([('unexpected', 'BASE TABLE')], {}),
                                 ([('measurements', 'VIEW')], {}), ([], {'extra_objects': 1})]:
            with self.subTest(objects=objects, options=options), self.assertRaises(RuntimeError):
                self.run_fake(objects, **options)
            self.assertFalse(any('CREATE TABLE' in q for q in self.last_queries))

    def test_ddl_failure_is_not_retried(self):
        with self.assertRaises(RuntimeError): self.run_fake([], fail_ddl=True)
        self.assertEqual(sum('CREATE TABLE' in q for q in self.last_queries), 1)


if __name__ == '__main__': unittest.main()
