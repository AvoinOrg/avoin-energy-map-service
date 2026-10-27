"""Fixed compatibility pin for the sandbox's stock pg_cron scheduler.

This is NOT an arbitrary preload allowlist. Re-review after any package/image
change. pg_cron 1.6.4 logs scheduler commands, not ordinary utility statements;
see the pinned source rationale and disposable log proof in the operator guide.
Credential DDL must stay on the direct, protected admin connection.
"""
from .private import require

# Observed in running image 0fea25c4..., PGDG Debian package
# postgresql-17-cron 1.6.4-2.pgdg120+1, upstream commit 9490f9cc9803f75105f2f7d89839a998f011f8d8.
CRON_FILES = {
    '/usr/lib/postgresql/17/lib/pg_cron.so':
        'ad59baf7e5c0eb682198caf5db80d94e07a6fa0ddc87e4a40552db84aff65ea8',
    '/usr/share/postgresql/17/extension/pg_cron.control':
        '4f040fceed11594458269d3c0db688fd4164f92bae0455c047323d68a3f71e1b',
}


def check_preloads(connection, scalar):
    shared = scalar(connection, "SELECT current_setting('shared_preload_libraries')")
    for name in ('session_preload_libraries', 'local_preload_libraries'):
        require(scalar(connection, 'SELECT current_setting(%s)', (name,)) == '', 'unsafe_logging')
    if shared == '':
        return
    # Exact spelling deliberately refuses quotes, aliases, paths and additions.
    require(shared == 'pg_cron', 'unsafe_logging')
    require(scalar(connection, "SELECT current_setting('server_version_num')") == '170002', 'unsafe_logging')
    require(scalar(connection, "SELECT setting FROM pg_config WHERE name='PKGLIBDIR'") ==
            '/usr/lib/postgresql/17/lib', 'unsafe_logging')
    require(scalar(connection, "SELECT current_setting('dynamic_library_path')") == '$libdir', 'unsafe_logging')
    require(scalar(connection, "SELECT pending_restart FROM pg_settings WHERE name='shared_preload_libraries'")
            is False, 'unsafe_logging')
    # Fixed Linux deployment: bind on-disk pins to the library actually mapped
    # into this fresh inherited backend. Reject aliases/deleted mappings and any
    # file changed since postmaster startup (including replacement/restoration).
    mappings = scalar(connection,
        "SELECT pg_catalog.pg_read_file('/proc/self/maps',0,1048576)")
    require(isinstance(mappings, str), 'unsafe_logging')
    cron_mappings = [line.split() for line in mappings.splitlines() if 'pg_cron' in line]
    require(bool(cron_mappings) and all(len(fields) == 6 and
            fields[-1] == '/usr/lib/postgresql/17/lib/pg_cron.so' and
            fields[-2].isdigit() and int(fields[-2]) > 0
            for fields in cron_mappings), 'unsafe_logging')
    for path, expected in CRON_FILES.items():
        require(scalar(connection,
            'SELECT encode(pg_catalog.sha256(pg_catalog.pg_read_binary_file(%s)),\'hex\')',
            (path,)) == expected, 'unsafe_logging')
        require(scalar(connection,
            "SELECT s.modification < pg_catalog.date_trunc('second',pg_catalog.pg_postmaster_start_time()) "
            "AND s.change < pg_catalog.date_trunc('second',pg_catalog.pg_postmaster_start_time()) "
            'FROM pg_catalog.pg_stat_file(%s) s', (path,)) is True, 'unsafe_logging')
