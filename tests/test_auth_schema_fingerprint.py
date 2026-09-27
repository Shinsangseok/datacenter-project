"""Offline comparisons against independently reproduced pre-migration DDL."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'k8s/mlflow/auth'))
sys.path.insert(0, str(ROOT/'k8s/mlflow'))
import schema_fingerprint as schema

FIXTURE = json.loads((ROOT/'tests/fixtures/mlflow_auth_schema.json').read_text())


class CanonicalTests(unittest.TestCase):
    def setUp(self):
        self.item = copy.deepcopy(FIXTURE['tables']['users'])
        self.defaults = FIXTURE['database_defaults']
        self.catalog = FIXTURE['catalog']

    def canonical(self, item=None, ddl=None):
        item = item or self.item
        return schema.canonical_table(ddl or item['actual_ddl'], item['table'], item['columns'],
                                      self.defaults, self.catalog)

    def test_twelve_expected_raw_hashes_have_independent_provenance(self):
        self.assertEqual(len(FIXTURE['tables']), 12)
        for name, item in FIXTURE['tables'].items():
            with self.subTest(table=name):
                self.assertEqual(hashlib.sha256(item['expected_ddl'].encode()).hexdigest(),
                                 item['legacy_expected_schema_hash'])

    def test_all_twenty_five_string_columns_match_despite_eleven_ddl_differences(self):
        differences = strings = 0
        for name, item in FIXTURE['tables'].items():
            with self.subTest(table=name):
                differences += item['actual_ddl'] != item['expected_ddl']
                strings += sum(c['character_set_name'] is not None for c in item['columns'])
                actual = self.canonical(item)
                expected = self.canonical(item, item['expected_ddl'])
                self.assertEqual(actual, expected)
                self.assertEqual(schema.fingerprint(actual), schema.fingerprint(expected))
        self.assertEqual((differences, strings), (11, 25))

    def test_explicit_charset_and_collation_equal_inheritance(self):
        ddl = self.item['expected_ddl'].replace('varchar(255)',
                     'varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        self.assertEqual(self.canonical(ddl=ddl), self.canonical())

    def test_table_defaults_inherited_from_database_are_equal(self):
        ddl = self.item['expected_ddl'].replace(' DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci', '')
        self.assertEqual(self.canonical(ddl=ddl), self.canonical())

    def test_charset_only_uses_charset_default_not_table_collation(self):
        ddl = self.item['expected_ddl'].replace('varchar(255)', 'varchar(255) CHARACTER SET utf8mb4')
        with self.assertRaises(RuntimeError): self.canonical(ddl=ddl)
        for c in self.item['columns']:
            if c['character_set_name']: c['collation_name'] = 'utf8mb4_0900_ai_ci'
        result = self.canonical(ddl=ddl)
        self.assertIn('CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci', result['ddl'])

    def test_real_column_collation_difference_changes_hash(self):
        before = schema.fingerprint(self.canonical())
        self.item['actual_ddl'] = self.item['actual_ddl'].replace(
            '`username` varchar(255) COLLATE utf8mb4_unicode_ci',
            '`username` varchar(255) COLLATE utf8mb4_bin')
        next(c for c in self.item['columns'] if c['column_name']=='username')['collation_name'] = 'utf8mb4_bin'
        self.assertNotEqual(schema.fingerprint(self.canonical()), before)

    def test_real_charset_difference_changes_hash(self):
        before = schema.fingerprint(self.canonical())
        self.item['actual_ddl'] = self.item['actual_ddl'].replace(
            '`username` varchar(255) COLLATE utf8mb4_unicode_ci',
            '`username` varchar(255) CHARACTER SET latin1 COLLATE latin1_swedish_ci')
        column = next(c for c in self.item['columns'] if c['column_name']=='username')
        column.update(character_set_name='latin1', collation_name='latin1_swedish_ci')
        self.assertNotEqual(schema.fingerprint(self.canonical()), before)

    def test_wrong_metadata_or_incompatible_collation_fails_closed(self):
        for replacement in ['COLLATE utf8mb4_bin', 'CHARACTER SET latin1 COLLATE utf8mb4_unicode_ci',
                            'COLLATE unknown_collation']:
            with self.subTest(replacement=replacement), self.assertRaises(RuntimeError):
                self.canonical(ddl=self.item['actual_ddl'].replace('COLLATE utf8mb4_unicode_ci', replacement))

    def test_table_collation_drift_is_not_hidden_by_explicit_columns(self):
        before = schema.fingerprint(self.canonical())
        self.item['actual_ddl'] = self.item['actual_ddl'].replace('COLLATE=utf8mb4_unicode_ci', 'COLLATE=utf8mb4_bin')
        self.item['table']['table_collation'] = 'utf8mb4_bin'
        self.assertNotEqual(schema.fingerprint(self.canonical()), before)

    def test_defaults_literals_indexes_and_table_options_remain_significant(self):
        before = schema.fingerprint(self.canonical())
        variants = [self.item['actual_ddl'].replace('varchar(255)', 'varchar(256)'),
                    self.item['actual_ddl'].replace('DEFAULT NULL', 'NOT NULL'),
                    self.item['actual_ddl'].replace('DEFAULT NULL', "DEFAULT ' COLLATE utf8mb4_bin'"),
                    self.item['actual_ddl'].replace('UNIQUE KEY', 'KEY'),
                    self.item['actual_ddl'].replace('ENGINE=InnoDB', 'ENGINE=MyISAM'),
                    self.item['actual_ddl']+' ROW_FORMAT=COMPACT']
        for ddl in variants:
            with self.subTest(ddl=ddl): self.assertNotEqual(schema.fingerprint(self.canonical(ddl=ddl)), before)

    def test_allocation_counter_is_separate_but_auto_increment_attribute_is_structural(self):
        before = self.canonical()
        after = self.canonical(ddl=self.item['actual_ddl'].replace('ENGINE=InnoDB', 'ENGINE=InnoDB AUTO_INCREMENT=2'))
        self.assertEqual(schema.fingerprint(before), schema.fingerprint(after))
        self.assertEqual((before['next_auto_increment'], after['next_auto_increment']), (1, 2))
        item = copy.deepcopy(self.item)
        item['actual_ddl'] = item['actual_ddl'].replace(' AUTO_INCREMENT,', ',')
        next(c for c in item['columns'] if c['column_name']=='id')['extra'] = ''
        self.assertNotEqual(schema.fingerprint(before), schema.fingerprint(self.canonical(item)))

    def test_foreign_key_target_and_delete_rule_remain_significant(self):
        item = copy.deepcopy(FIXTURE['tables']['experiment_permissions'])
        before = schema.fingerprint(self.canonical(item))
        for ddl in [item['actual_ddl'].replace('REFERENCES `users`', 'REFERENCES `roles`'),
                    item['actual_ddl'].replace('REFERENCES `users` (`id`)', 'REFERENCES `users` (`id`) ON DELETE CASCADE')]:
            self.assertNotEqual(schema.fingerprint(self.canonical(item, ddl)), before)

    def test_metadata_order_is_stable_but_missing_duplicate_or_extra_columns_rejected(self):
        before = self.canonical()
        self.item['columns'].reverse()
        self.assertEqual(before, self.canonical())
        for columns in [self.item['columns'][:-1], self.item['columns']+self.item['columns'][:1]]:
            broken = {**self.item, 'columns': columns}
            with self.assertRaises(RuntimeError): self.canonical(broken)
        self.item['columns'][0].pop('collation_name')
        with self.assertRaises(RuntimeError): self.canonical()

    def test_unsupported_character_type_is_rejected_not_stripped(self):
        with self.assertRaises(RuntimeError):
            self.canonical(ddl=self.item['actual_ddl'].replace('varchar(255)', "enum('a','b')"))

    def test_v2_cannot_match_raw_v1_hash(self):
        digest = schema.fingerprint(self.canonical())
        self.assertTrue(digest.startswith(schema.FORMAT+':'))
        self.assertNotEqual(digest, self.item['legacy_expected_schema_hash'])
        with self.assertRaises(RuntimeError): schema.fingerprint({'format':'mysql-auth-schema-v1'})


class Rows:
    def __init__(self, rows): self.rows = rows
    def all(self): return self.rows
    def one(self):
        if len(self.rows) != 1: raise AssertionError('row count')
        return self.rows[0]
    def scalars(self): return Rows([r[0] for r in self.rows])
    def mappings(self): return iter(self.rows)
    def __iter__(self): return iter(self.rows)


class ReadOnlyConnection:
    def __init__(self): self.queries = []
    def exec_driver_sql(self, sql):
        self.queries.append(sql)
        if sql.startswith('SELECT DEFAULT_CHARACTER_SET_NAME'): return Rows([FIXTURE['database_defaults']])
        if 'information_schema.CHARACTER_SETS' in sql: return Rows(list(FIXTURE['catalog']['defaults'].items()))
        if 'information_schema.COLLATIONS' in sql: return Rows(list(FIXTURE['catalog']['collations'].items()))
        if sql == 'SHOW TABLES': return Rows([(n,) for n in FIXTURE['tables']])
        if sql.startswith('SHOW CREATE TABLE '):
            name = sql.split('`')[1];return Rows([(name,FIXTURE['tables'][name]['actual_ddl'])])
        if sql.startswith('SELECT * FROM '): return Rows([('f1a2b3c4d5e6',)] if 'alembic_version_auth' in sql else [])
        if sql == 'SELECT version_num FROM alembic_version_auth': return Rows([('f1a2b3c4d5e6',)])
        raise AssertionError(sql)
    def execute(self, sql, params):
        sql = str(sql);self.queries.append(sql)
        assert 'TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:name' in sql
        table = FIXTURE['tables'][params['name']]
        if 'information_schema.TABLES' in sql: return Rows([table['table']])
        if 'information_schema.COLUMNS' in sql: return Rows(table['columns'])
        raise AssertionError(sql)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_uses_existing_session_and_preserves_data_and_revision_checks(self):
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            import fenced_migration
            connection = ReadOnlyConnection()
            actual = fenced_migration.snapshot(connection)
        self.assertEqual(actual['revision'], ['f1a2b3c4d5e6'])
        self.assertEqual(len(actual['tables']), 12)
        for name, item in actual['tables'].items():
            self.assertTrue(item['schema'].startswith(schema.FORMAT+':'))
            rows = [['f1a2b3c4d5e6']] if name=='alembic_version_auth' else []
            self.assertEqual(item['rows'], len(rows))
            self.assertEqual(item['data'], hashlib.sha256(json.dumps(rows).encode()).hexdigest())
        self.assertTrue(all(q.startswith(('SELECT ', 'SHOW ')) for q in connection.queries))
        self.assertTrue(any('information_schema.COLUMNS' in q for q in connection.queries))


class BootstrapTransitionTests(unittest.TestCase):
    def setUp(self):
        import fenced_migration
        self.before = fenced_migration.snapshot(ReadOnlyConnection())
        self.after = copy.deepcopy(self.before)
        self.after['tables']['users'].update(rows=1, next_auto_increment=2, data='independently-verified-admin-data')

    def test_expected_single_admin_transition_passes(self):
        schema.verify_bootstrap_transition(self.before, self.after)

    def test_counter_structure_role_data_and_revision_drift_rejected(self):
        variants = []
        for field, value in [('next_auto_increment',3), ('rows',2), ('schema','unapproved')]:
            value_after = copy.deepcopy(self.after)
            value_after['tables']['users'][field] = value
            variants.append(value_after)
        for name, field, value in [('roles','data','unexpected-role'),
                                   ('roles','next_auto_increment',2),
                                   ('users','next_auto_increment',None)]:
            value_after = copy.deepcopy(self.after)
            value_after['tables'][name][field] = value
            variants.append(value_after)
        wrong_head = copy.deepcopy(self.after); wrong_head['revision'] = ['wrong']; variants.append(wrong_head)
        for value_after in variants:
            with self.subTest(after=value_after), self.assertRaises(RuntimeError):
                schema.verify_bootstrap_transition(self.before, value_after)

    def test_legacy_snapshots_are_not_auto_approved(self):
        old = copy.deepcopy(self.before)
        for table in old['tables'].values(): table.pop('next_auto_increment')
        with self.assertRaises(RuntimeError): schema.verify_bootstrap_transition(old, self.after)



if __name__ == '__main__': unittest.main()
