"""Versioned MySQL Auth fingerprints; resolve collation, never discard it.

Only the reviewed Auth CHAR/VARCHAR/TEXT DDL grammar is canonicalized. Other
character types fail closed. Unrelated DDL, including keys, constraints, options,
and comments remains byte-for-byte significant. The next AUTO_INCREMENT value
is recorded separately and compared as allocation state, not schema structure.
"""
import hashlib
import json
import re

from sqlalchemy import text

FORMAT = 'mysql-auth-schema-v2'
COLUMN_FIELDS = ('column_name', 'ordinal_position', 'column_type', 'is_nullable',
                 'column_default', 'character_set_name', 'collation_name',
                 'extra', 'column_comment', 'generation_expression')
TABLE_FIELDS = ('table_name', 'table_type', 'engine', 'row_format',
                'table_collation', 'create_options', 'table_comment')
CHARACTER_COLUMN = re.compile(
    r'^(  `([A-Za-z0-9_]+)` (?:char\(\d+\)|varchar\(\d+\)|(?:tiny|medium|long)?text))(.*)$', re.I)
TABLE_OPTIONS = re.compile(
    r'^(\) ENGINE=[A-Za-z0-9_]+(?: AUTO_INCREMENT=\d+)?)'
    r'(?: DEFAULT CHARSET=([A-Za-z0-9_]+))?'
    r'(?: COLLATE=([A-Za-z0-9_]+))?(.*)$')


def require(ok):
    if not ok:
        raise RuntimeError('Auth schema canonicalization rejected')


def effective(charset, collation, inherited, catalog):
    """MySQL's four cases: both, charset only, collation only, inheritance."""
    if collation:
        require(collation in catalog['collations'])
        owner = catalog['collations'][collation]
        require(charset is None or charset == owner)
        return owner, collation
    if charset:
        require(charset in catalog['defaults'])
        return effective(charset, catalog['defaults'][charset], None, catalog)
    require(inherited is not None)
    return effective(*inherited, None, catalog)


def canonical_table(ddl, table, columns, database_defaults, catalog):
    """Resolve DDL inheritance independently, then cross-check effective metadata.

    The expected side must come from reviewed expected DDL/metadata, never from
    copying observed hashes. Information_schema metadata must be complete.
    """
    require(set(table) == set(TABLE_FIELDS) and table['table_type'] == 'BASE TABLE')
    require(columns and all(set(c) == set(COLUMN_FIELDS) for c in columns))
    columns = sorted(columns, key=lambda c: c['ordinal_position'])
    require([c['ordinal_position'] for c in columns] == list(range(1, len(columns) + 1)))
    by_name = {c['column_name']: c for c in columns}
    require(len(by_name) == len(columns))
    lines = ddl.split('\n')
    require(lines[0] == 'CREATE TABLE `'+table['table_name']+'` (')
    options = TABLE_OPTIONS.fullmatch(lines[-1])
    require(options is not None)
    prefix, charset, collation, suffix = options.groups()
    # Reject unknown/reordered table charset syntax instead of guessing.
    require(not re.search(r'\b(?:CHARSET|CHARACTER SET|COLLATE)\b', suffix, re.I))
    table_pair = effective(charset, collation, database_defaults, catalog)
    require(table_pair[1] == table['table_collation'])
    counter = re.search(r' AUTO_INCREMENT=(\d+)$', prefix)
    auto_columns = [c for c in columns if 'auto_increment' in c['extra'].lower().split()]
    require(len(auto_columns) <= 1 and (counter is None or len(auto_columns) == 1))
    next_id = int(counter[1]) if counter else (1 if auto_columns else None)
    require(next_id is None or next_id >= 1)
    if counter:
        prefix = prefix[:counter.start()]
    lines[-1] = prefix+' DEFAULT CHARSET='+table_pair[0]+' COLLATE='+table_pair[1]+suffix
    seen = []
    for i in range(1, len(lines) - 1):
        declaration = re.match(r'^  `([A-Za-z0-9_]+)` ', lines[i])
        if not declaration:
            continue  # Full index/FK/check definitions remain in the hashed DDL.
        name = declaration[1]
        require(name in by_name)
        seen.append(name)
        column = by_name[name]
        character = CHARACTER_COLUMN.fullmatch(lines[i])
        if column['character_set_name'] is None:
            require(column['collation_name'] is None and character is None)
            continue
        require(character is not None)
        prefix, _, rest = character.groups()
        charset = collation = None
        match = re.match(r'^ CHARACTER SET ([A-Za-z0-9_]+)', rest)
        if match:
            charset = match[1]
            rest = rest[match.end():]
        match = re.match(r'^ COLLATE ([A-Za-z0-9_]+)', rest)
        if match:
            collation = match[1]
            rest = rest[match.end():]
        pair = effective(charset, collation, table_pair, catalog)
        require(pair == (column['character_set_name'], column['collation_name']))
        # Only the type-adjacent clauses are rewritten. Defaults, expressions and
        # quoted literals containing COLLATE remain intact and hash-significant.
        lines[i] = prefix+' CHARACTER SET '+pair[0]+' COLLATE '+pair[1]+rest
    require(seen == [c['column_name'] for c in columns])
    return {'format': FORMAT, 'ddl': '\n'.join(lines), 'table': dict(table),
            'columns': [dict(c) for c in columns], 'next_auto_increment': next_id}


def fingerprint(canonical):
    require(canonical.get('format') == FORMAT)
    require(set(canonical) == {'format', 'ddl', 'table', 'columns', 'next_auto_increment'})
    structure = {k: v for k, v in canonical.items() if k != 'next_auto_increment'}
    data = json.dumps(structure, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return FORMAT+':'+hashlib.sha256(data.encode()).hexdigest()


def load_context(connection):
    # Read through the caller's fenced physical session; no engine/reconnect.
    defaults = connection.exec_driver_sql(
        'SELECT DEFAULT_CHARACTER_SET_NAME,DEFAULT_COLLATION_NAME '
        'FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=DATABASE()').one()
    charsets = connection.exec_driver_sql(
        'SELECT CHARACTER_SET_NAME,DEFAULT_COLLATE_NAME FROM information_schema.CHARACTER_SETS')
    collations = connection.exec_driver_sql(
        'SELECT COLLATION_NAME,CHARACTER_SET_NAME FROM information_schema.COLLATIONS')
    return tuple(defaults), {'defaults': dict(charsets.all()), 'collations': dict(collations.all())}


def schema_snapshot(connection, name, ddl, context):
    defaults, catalog = context
    def metadata(source, fields):
        sql = 'SELECT '+','.join(f.upper()+' AS '+f for f in fields)
        sql += ' FROM information_schema.'+source+' WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:name'
        return [dict(row) for row in connection.execute(text(sql), {'name': name}).mappings()]
    tables = metadata('TABLES', TABLE_FIELDS)
    require(len(tables) == 1)
    columns = metadata('COLUMNS', COLUMN_FIELDS)
    canonical = canonical_table(ddl, tables[0], columns, defaults, catalog)
    return {'schema': fingerprint(canonical),
            'next_auto_increment': canonical['next_auto_increment']}


def verify_bootstrap_transition(before, after):
    """Only one initial administrator row may be added; other state stays exact.

    The caller must independently check username, password hash and is_admin.
    Full migration/checkpoint comparisons still include every allocation counter.
    """
    require(before['revision'] == after['revision'] == ['f1a2b3c4d5e6'])
    require(set(before['tables']) == set(after['tables']) and len(before['tables']) == 12)
    require('users' in before['tables'] and 'alembic_version_auth' in before['tables'])
    for name, old in before['tables'].items():
        new = after['tables'][name]
        require(set(old) == set(new) == {'schema', 'data', 'rows', 'next_auto_increment'})
        require(old['schema'].startswith(FORMAT+':') and new['schema'] == old['schema'])
        if name == 'users':
            require(old['rows'] == 0 and new['rows'] == 1)
            require(old['next_auto_increment'] == 1 and new['next_auto_increment'] == 2)
        else:
            require(old == new)
            require(old['rows'] == (1 if name == 'alembic_version_auth' else 0))

