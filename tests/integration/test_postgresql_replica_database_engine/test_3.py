import os.path as p
import random
import threading
import time
import uuid
from random import randrange

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from helpers.cluster import ClickHouseCluster
from helpers.config_cluster import pg_pass
from helpers.postgres_utility import (
    PostgresManager,
    assert_nested_table_is_created,
    assert_number_of_columns,
    check_several_tables_are_synchronized,
    check_tables_are_synchronized,
    create_postgres_schema,
    create_postgres_table,
    create_postgres_table_with_schema,
    create_replication_slot,
    drop_postgres_schema,
    drop_postgres_table,
    drop_postgres_table_with_schema,
    drop_replication_slot,
    get_postgres_conn,
    postgres_table_template,
    postgres_table_template_2,
    postgres_table_template_3,
    postgres_table_template_4,
    postgres_table_template_5,
    postgres_table_template_6,
    queries,
)
from helpers.test_tools import TSV, assert_eq_with_retry

cluster = ClickHouseCluster(__file__)
instance = cluster.add_instance(
    "instance",
    main_configs=["configs/log_conf.xml"],
    user_configs=["configs/users.xml"],
    with_postgres=True,
    stay_alive=True,
)

instance2 = cluster.add_instance(
    "instance2",
    main_configs=["configs/log_conf.xml", "configs/merge_tree_too_many_parts.xml"],
    user_configs=["configs/users.xml"],
    with_postgres=True,
    stay_alive=True,
)


pg_manager = PostgresManager()
pg_manager2 = PostgresManager()
pg_manager_instance2 = PostgresManager()


@pytest.fixture(scope="module")
def started_cluster():
    try:
        cluster.start()
        pg_manager.init(
            instance,
            cluster.postgres_ip,
            cluster.postgres_port,
            default_database="postgres_database",
        )
        pg_manager_instance2.init(
            instance2,
            cluster.postgres_ip,
            cluster.postgres_port,
            default_database="postgres_database",
            postgres_db_exists=True,
        )
        pg_manager2.init(
            instance2, cluster.postgres_ip, cluster.postgres_port, "postgres_database2"
        )

        yield cluster

    finally:
        cluster.shutdown()


@pytest.fixture(autouse=True)
def setup_teardown():
    print("PostgreSQL is available - running test")
    yield  # run test
    pg_manager.restart()


def test_table_override(started_cluster):
    table_name = "table_override"
    materialized_database = "test_database"

    pg_manager.create_postgres_table(table_name, template=postgres_table_template_6)
    instance.query(
        f"insert into postgres_database.{table_name} select number, 'test' from numbers(10)"
    )

    table_overrides = f" TABLE OVERRIDE {table_name} (COLUMNS (key Int32, value String) PARTITION BY key)"
    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[f"materialized_postgresql_tables_list = '{table_name}'"],
        materialized_database=materialized_database,
        table_overrides=table_overrides,
    )

    check_tables_are_synchronized(
        instance, table_name, postgres_database=pg_manager.get_default_database()
    )

    assert 10 == int(
        instance.query(f"SELECT count() FROM {materialized_database}.{table_name}")
    )

    expected = "CREATE TABLE test_database.table_override\\n(\\n    `key` Int32,\\n    `value` String,\\n    `_sign` Int8 MATERIALIZED 1,\\n    `_version` UInt64 MATERIALIZED 1\\n)\\nENGINE = ReplacingMergeTree(_version)\\nPARTITION BY key\\nORDER BY tuple(key)"
    assert (
        expected
        == instance.query(
            f"show create table {materialized_database}.{table_name}"
        ).strip()
    )

    assert (
        "test"
        == instance.query(
            f"SELECT value FROM {materialized_database}.{table_name} WHERE key = 2"
        ).strip()
    )

    conn = get_postgres_conn(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        database_name="postgres_database",
        database=True,
        auto_commit=True,
    )
    cursor = conn.cursor()
    cursor.execute(f"SELECT count(*) FROM {table_name}")
    assert 10 == cursor.fetchall()[0][0]

    pg_manager.execute(f"UPDATE {table_name} SET value='kek' WHERE key=2")

    cursor.execute(f"SELECT value FROM {table_name} WHERE key=2")
    assert "kek" == cursor.fetchall()[0][0]

    pg_manager.execute(f"DELETE FROM {table_name} WHERE key=2")

    cursor.execute(f"SELECT count(*) FROM {table_name}")
    assert 9 == cursor.fetchall()[0][0]

    conn.close()

    check_tables_are_synchronized(
        instance, table_name, postgres_database=pg_manager.get_default_database()
    )

    assert (
        ""
        == instance.query(
            f"SELECT value FROM {materialized_database}.{table_name} WHERE key = 2"
        ).strip()
    )


def test_materialized_view(started_cluster):
    pg_manager.execute(f"DROP TABLE IF EXISTS test_table")
    pg_manager.execute(
        f"CREATE TABLE test_table (key integer PRIMARY KEY, value integer)"
    )
    pg_manager.execute(f"INSERT INTO test_table SELECT 1, 2")
    instance.query("DROP DATABASE IF EXISTS test_database")
    instance.query(
        "CREATE DATABASE test_database ENGINE = MaterializedPostgreSQL(postgres1) SETTINGS materialized_postgresql_tables_list='test_table'"
    )
    check_tables_are_synchronized(instance, "test_table")
    instance.query("DROP TABLE IF EXISTS mv")
    instance.query(
        "CREATE MATERIALIZED VIEW mv ENGINE=MergeTree ORDER BY tuple() POPULATE AS SELECT * FROM test_database.test_table"
    )
    assert "1\t2" == instance.query("SELECT * FROM mv").strip()
    pg_manager.execute(f"INSERT INTO test_table SELECT 3, 4")
    check_tables_are_synchronized(instance, "test_table")
    assert "1\t2\n3\t4" == instance.query("SELECT * FROM mv ORDER BY 1, 2").strip()
    instance.query("DROP VIEW mv")
    pg_manager.drop_materialized_db()


def test_too_many_parts(started_cluster):
    table = "test_table"
    pg_manager2.create_and_fill_postgres_table(table)
    pg_manager2.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = 'test_table', materialized_postgresql_backoff_min_ms = 100, materialized_postgresql_backoff_max_ms = 100"
        ],
    )
    check_tables_are_synchronized(
        instance2, "test_table", postgres_database=pg_manager2.get_default_database()
    )
    assert (
        "50" == instance2.query("SELECT count() FROM test_database.test_table").strip()
    )

    instance2.query("SYSTEM STOP MERGES")
    num = 50
    for i in range(10):
        instance2.query(
            f"""
            INSERT INTO {pg_manager2.get_default_database()}.test_table SELECT {num}, {num};
        """
        )
        num = num + 1
        for i in range(30):
            if num == int(
                instance2.query("SELECT count() FROM test_database.test_table")
            ) or instance2.contains_in_log("DB::Exception: Too many parts"):
                break
            time.sleep(1)
            print(f"wait sync try {i}")
        instance2.query("SYSTEM FLUSH LOGS")
        if instance2.contains_in_log("DB::Exception: Too many parts"):
            break
        assert num == int(
            instance2.query("SELECT count() FROM test_database.test_table")
        ) or num - 1 == int(
            instance2.query("SELECT count() FROM test_database.test_table")
        )

    assert instance2.contains_in_log("DB::Exception: Too many parts")
    print(num)
    assert num == int(
        instance2.query("SELECT count() FROM test_database.test_table")
    ) or num - 1 == int(instance2.query("SELECT count() FROM test_database.test_table"))

    instance2.query("SYSTEM START MERGES")
    check_tables_are_synchronized(
        instance2, "test_table", postgres_database=pg_manager2.get_default_database()
    )

    # assert "200" == instance.query("SELECT count FROM test_database.test_table").strip()
    pg_manager2.drop_materialized_db()


def test_toast(started_cluster):
    table = "test_toast"
    pg_manager.create_postgres_table(
        table,
        "",
        """CREATE TABLE "{}" (id integer PRIMARY KEY, txt text, other text)""",
    )
    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = '{table}'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
    )

    pg_manager.execute(
        f"""\
INSERT INTO {table} (id, txt)\
VALUES (1, (SELECT array_to_string(ARRAY(SELECT chr((100 + round(random() * 25)) :: integer) FROM generate_series(1,30000) as t(i)), '')))
    """
    )

    check_tables_are_synchronized(
        instance,
        table,
        postgres_database=pg_manager.get_default_database(),
        order_by="id",
    )


def test_replica_consumer(started_cluster):
    table = "test_replica_consumer"
    pg_manager_instance2.restart()

    pg_manager.create_postgres_table(table)
    instance.query(
        f"INSERT INTO postgres_database.{table} SELECT number, number from numbers(0, 50)"
    )

    for pm in [pg_manager, pg_manager_instance2]:
        pm.create_materialized_db(
            ip=started_cluster.postgres_ip,
            port=started_cluster.postgres_port,
            settings=[
                f"materialized_postgresql_tables_list = '{table}'",
                "materialized_postgresql_backoff_min_ms = 100",
                "materialized_postgresql_backoff_max_ms = 100",
                "materialized_postgresql_use_unique_replication_consumer_identifier = 1",
            ],
        )

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )
    check_tables_are_synchronized(
        instance2, table, postgres_database=pg_manager_instance2.get_default_database()
    )

    assert 50 == int(instance.query(f"SELECT count() FROM test_database.{table}"))
    assert 50 == int(instance2.query(f"SELECT count() FROM test_database.{table}"))

    instance.query(
        f"INSERT INTO postgres_database.{table} SELECT number, number from numbers(1000, 1000)"
    )

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )
    check_tables_are_synchronized(
        instance2, table, postgres_database=pg_manager_instance2.get_default_database()
    )

    assert 1050 == int(instance.query(f"SELECT count() FROM test_database.{table}"))
    assert 1050 == int(instance2.query(f"SELECT count() FROM test_database.{table}"))

    for pm in [pg_manager, pg_manager_instance2]:
        pm.drop_materialized_db()
    pg_manager_instance2.clear()


def test_bad_connection_options(started_cluster):
    table = "test_bad_connection_options"

    pg_manager.create_postgres_table(table)
    instance.query(
        f"INSERT INTO postgres_database.{table} SELECT number, number from numbers(0, 50)"
    )

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = '{table}'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
        user="postrges",
        password="kek",
    )

    instance.wait_for_log_line('role "postrges" does not exist')
    assert instance.contains_in_log(
        "<Error> void DB::DatabaseMaterializedPostgreSQL::startSynchronization(): std::exception. Code: 1001, type: pqxx::broken_connection"
    )
    assert "test_database" in instance.query("SHOW DATABASES")
    assert "" == instance.query("show tables from test_database").strip()
    pg_manager.drop_materialized_db("test_database")


def test_failed_load_from_snapshot(started_cluster):
    if instance.is_built_with_sanitizer() or instance.is_debug_build():
        pytest.skip(
            "Sanitizers and debug mode are skipped, because this test thrown logical error"
        )

    table = "failed_load"

    pg_manager.create_postgres_table(
        table,
        template="""
    CREATE TABLE IF NOT EXISTS "{}" (
    key text NOT NULL, value text[], PRIMARY KEY(key))
    """,
    )
    instance.query(
        f"INSERT INTO postgres_database.{table} SELECT number, [1, 2] from numbers(0, 1000000)"
    )

    # Create a table with wrong table structure
    assert "Could not convert string to i" in instance.query_and_get_error(
        f"""
        SET allow_experimental_materialized_postgresql_table=1;
        CREATE TABLE {table} (a Int32, b Int32) ENGINE=MaterializedPostgreSQL('{started_cluster.postgres_ip}:{started_cluster.postgres_port}', 'postgres_database', '{table}', 'postgres', '{pg_pass}') ORDER BY a
        """
    )


def test_symbols_in_publication_name(started_cluster):
    id = uuid.uuid4()
    db = f"test_{id}"
    table = f"test_symbols_in_publication_name"

    pg_manager3 = PostgresManager()
    pg_manager3.init(
        instance,
        cluster.postgres_ip,
        cluster.postgres_port,
        default_database=db,
    )

    pg_manager3.create_postgres_table(table)
    instance.query(
        f"INSERT INTO `{db}`.`{table}` SELECT number, number from numbers(0, 50)"
    )

    pg_manager3.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        materialized_database=db,
        settings=[
            f"materialized_postgresql_tables_list = '{table}'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
    )
    check_tables_are_synchronized(
        instance, table, materialized_database=db, postgres_database=db
    )
    pg_manager3.drop_materialized_db(db)
    pg_manager3.execute(f'drop table "{table}"')


def test_generated_columns(started_cluster):
    table = "test_generated_columns"

    pg_manager.create_postgres_table(
        table,
        "",
        f"""CREATE TABLE {table} (
             key integer PRIMARY KEY,
             x integer DEFAULT 0,
             temp integer DEFAULT 0,
             y integer GENERATED ALWAYS AS (x*2) STORED,
             z text DEFAULT 'z');
         """,
    )

    pg_manager.execute(f"alter table {table} drop column temp;")
    pg_manager.execute(f"insert into {table} (key, x, z) values (1,1,'1');")
    pg_manager.execute(f"insert into {table} (key, x, z) values (2,2,'2');")

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = '{table}'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
    )

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )

    pg_manager.execute(f"insert into {table} (key, x, z) values (3,3,'3');")
    pg_manager.execute(f"insert into {table} (key, x, z) values (4,4,'4');")

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )

    pg_manager.execute(f"insert into {table} (key, x, z) values (5,5,'5');")
    pg_manager.execute(f"insert into {table} (key, x, z) values (6,6,'6');")

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )


def test_generated_columns_with_sequence(started_cluster):
    table = "test_generated_columns_with_sequence"

    pg_manager.create_postgres_table(
        table,
        "",
        f"""CREATE TABLE {table} (
             key integer PRIMARY KEY,
             x integer,
             y integer GENERATED ALWAYS AS (x*2) STORED,
             z text);
         """,
    )

    pg_manager.execute(
        f"create sequence {table}_id_seq increment by 1 minvalue 1 start 1;"
    )
    pg_manager.execute(
        f"alter table {table} alter key set default nextval('{table}_id_seq');"
    )
    pg_manager.execute(f"insert into {table} (key, x, z) values (1,1,'1');")
    pg_manager.execute(f"insert into {table} (key, x, z) values (2,2,'2');")

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = '{table}'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
    )

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )


def test_default_columns(started_cluster):
    table = "test_default_columns"

    pg_manager.create_postgres_table(
        table,
        "",
        f"""CREATE TABLE {table} (
             key integer PRIMARY KEY,
             x integer,
             y text DEFAULT 'y1',
             z integer,
             a text DEFAULT 'a1',
             b integer);
         """,
    )

    pg_manager.execute(f"insert into {table} (key, x, z, b) values (1,1,1,1);")
    pg_manager.execute(f"insert into {table} (key, x, z, b) values (2,2,2,2);")

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = '{table}'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
    )

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )

    pg_manager.execute(f"insert into {table} (key, x, z, b) values (3,3,3,3);")
    pg_manager.execute(f"insert into {table} (key, x, z, b) values (4,4,4,4);")

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )

    pg_manager.execute(f"insert into {table} (key, x, z, b) values (5,5,5,5);")
    pg_manager.execute(f"insert into {table} (key, x, z, b) values (6,6,6,6);")

    check_tables_are_synchronized(
        instance, table, postgres_database=pg_manager.get_default_database()
    )


def test_dependent_loading(started_cluster):
    table = "test_dependent_loading"

    pg_manager.create_postgres_table(table)
    instance.query(
        f"INSERT INTO postgres_database.{table} SELECT number, number from numbers(0, 50)"
    )

    instance.query(
        f"""
        SET allow_experimental_materialized_postgresql_table=1;
        CREATE TABLE {table} (key Int32, value Int32)
        ENGINE=MaterializedPostgreSQL('{started_cluster.postgres_ip}:{started_cluster.postgres_port}', 'postgres_database', '{table}', 'postgres', '{pg_pass}') ORDER BY key
        """
    )

    check_tables_are_synchronized(
        instance,
        table,
        postgres_database=pg_manager.get_default_database(),
        materialized_database="default",
    )

    assert 50 == int(instance.query(f"SELECT count() FROM {table}"))

    instance.restart_clickhouse()

    check_tables_are_synchronized(
        instance,
        table,
        postgres_database=pg_manager.get_default_database(),
        materialized_database="default",
    )

    assert 50 == int(instance.query(f"SELECT count() FROM {table}"))

    uuid = instance.query(
        f"SELECT uuid FROM system.tables WHERE name='{table}' and database='default' limit 1"
    ).strip()
    nested_table = f"default.`{uuid}_nested`"
    instance.contains_in_log(
        f"Table default.{table} has 1 dependencies: {nested_table} (level 1)"
    )

    instance.query("SYSTEM FLUSH LOGS")
    nested_time = instance.query(
        f"SELECT event_time_microseconds FROM system.text_log WHERE message like 'Loading table default.{uuid}_nested' and message not like '%like%'"
    ).strip()
    time = (
        instance.query(
            f"SELECT event_time_microseconds FROM system.text_log WHERE message like 'Loading table default.{table}' and message not like '%like%'"
        )
        .strip()
        .split("\n")[-1]
    )
    instance.query(
        f"SELECT toDateTime64('{nested_time}', 6) < toDateTime64('{time}', 6)"
    )

    instance.query(f"DROP TABLE {table} SYNC")


def test_partial_table(started_cluster):
    table = "test_partial_table"

    pg_manager.create_postgres_table(
        table,
        "",
        f"""CREATE TABLE {table} (
             key integer PRIMARY KEY,
             x integer DEFAULT 0,
             y integer,
             z text DEFAULT 'z');
         """,
    )
    pg_manager.execute(f"insert into {table} (key, x, z) values (1,1,'a');")
    pg_manager.execute(f"insert into {table} (key, x, z) values (2,2,'b');")

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = '{table}(z, key)'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
    )
    check_tables_are_synchronized(
        instance,
        table,
        postgres_database=pg_manager.get_default_database(),
        columns=["key", "z"],
    )

    pg_manager.execute(f"insert into {table} (key, x, z) values (3,3,'c');")
    pg_manager.execute(f"insert into {table} (key, x, z) values (4,4,'d');")

    check_tables_are_synchronized(
        instance,
        table,
        postgres_database=pg_manager.get_default_database(),
        columns=["key", "z"],
    )


# it is important to check case when table name, with subset of columns, is substring of table with full set of columns
@pytest.mark.parametrize(
    "table_list",
    [
        "{}(key, x, z), {}_full, {}_full1",
        "{}_full, {}(key, x, z), {}_full1",
        "{}_full,{}(key, x, z),{}_full1",
        "{}_full,{}_full1,{}(key, x, z)",
    ],
)
def test_partial_and_full_table(started_cluster, table_list):
    table = "test_partial_and_full_table"
    table_list = table_list.format(table, table, table)
    print(table_list)

    pg_manager.create_postgres_table(
        table,
        "",
        f"""CREATE TABLE {table} (
             key integer PRIMARY KEY,
             x integer DEFAULT 0,
             y integer,
             z text DEFAULT 'z');
         """,
    )
    pg_manager.execute(f"insert into {table} (key, x, y, z) values (1,1,1,'1');")
    pg_manager.execute(f"insert into {table} (key, x, y, z) values (2,2,2,'2');")

    pg_manager.create_postgres_table(
        table + "_full",
        "",
        f"""CREATE TABLE {table}_full (
             key integer PRIMARY KEY,
             x integer DEFAULT 0,
             y integer,
             z text DEFAULT 'z');
         """,
    )
    pg_manager.execute(f"insert into {table}_full (key, x, y, z) values (3,3,3,'3');")
    pg_manager.execute(f"insert into {table}_full (key, x, y, z) values (4,4,4,'4');")

    pg_manager.create_postgres_table(
        table + "_full1",
        "",
        f"""CREATE TABLE {table}_full1 (
             key integer PRIMARY KEY,
             x integer DEFAULT 0,
             y integer,
             z text DEFAULT 'z');
         """,
    )
    pg_manager.execute(f"insert into {table}_full1 (key, x, y, z) values (5,5,5,'5');")
    pg_manager.execute(f"insert into {table}_full1 (key, x, y, z) values (6,6,6,'6');")

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            f"materialized_postgresql_tables_list = '{table_list}'",
            "materialized_postgresql_backoff_min_ms = 100",
            "materialized_postgresql_backoff_max_ms = 100",
        ],
    )
    check_tables_are_synchronized(
        instance,
        f"{table}",
        postgres_database=pg_manager.get_default_database(),
        columns=["key", "x", "z"],
    )
    check_tables_are_synchronized(
        instance, f"{table}_full", postgres_database=pg_manager.get_default_database()
    )
    check_tables_are_synchronized(
        instance, f"{table}_full1", postgres_database=pg_manager.get_default_database()
    )

    pg_manager.execute(f"insert into {table} (key, x, z) values (7,7,'7');")
    pg_manager.execute(f"insert into {table}_full (key, x, z) values (8,8,'8');")
    pg_manager.execute(f"insert into {table}_full1 (key, x, z) values (9,9,'9');")

    check_tables_are_synchronized(
        instance,
        f"{table}",
        postgres_database=pg_manager.get_default_database(),
        columns=["key", "x", "z"],
    )
    check_tables_are_synchronized(
        instance, f"{table}_full", postgres_database=pg_manager.get_default_database()
    )
    check_tables_are_synchronized(
        instance, f"{table}_full1", postgres_database=pg_manager.get_default_database()
    )


def test_quoting_publication(started_cluster):
    postgres_database = "postgres-postgres"
    pg_manager3 = PostgresManager()
    pg_manager3.init(
        instance,
        cluster.postgres_ip,
        cluster.postgres_port,
        default_database=postgres_database,
    )
    NUM_TABLES = 5
    materialized_database = "test-database"

    pg_manager3.create_and_fill_postgres_tables(NUM_TABLES, 10000)

    check_table_name_1 = "postgresql-replica-5"
    pg_manager3.create_and_fill_postgres_table(check_table_name_1)

    pg_manager3.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        materialized_database=materialized_database,
    )
    check_several_tables_are_synchronized(
        instance,
        NUM_TABLES,
        materialized_database=materialized_database,
        postgres_database=postgres_database,
    )

    result = instance.query(f"SHOW TABLES FROM `{materialized_database}`")
    assert (
        result
        == "postgresql-replica-5\npostgresql_replica_0\npostgresql_replica_1\npostgresql_replica_2\npostgresql_replica_3\npostgresql_replica_4\n"
    )

    check_tables_are_synchronized(
        instance,
        check_table_name_1,
        materialized_database=materialized_database,
        postgres_database=postgres_database,
    )
    instance.query(
        f"INSERT INTO `{postgres_database}`.`{check_table_name_1}` SELECT number, number from numbers(10000, 10000)"
    )
    check_tables_are_synchronized(
        instance,
        check_table_name_1,
        materialized_database=materialized_database,
        postgres_database=postgres_database,
    )

    check_table_name_2 = "postgresql-replica-6"
    pg_manager3.create_and_fill_postgres_table(check_table_name_2)

    instance.query(f"ATTACH TABLE `{materialized_database}`.`{check_table_name_2}`")

    result = instance.query(f"SHOW TABLES FROM `{materialized_database}`")
    assert (
        result
        == "postgresql-replica-5\npostgresql-replica-6\npostgresql_replica_0\npostgresql_replica_1\npostgresql_replica_2\npostgresql_replica_3\npostgresql_replica_4\n"
    )

    check_tables_are_synchronized(
        instance,
        check_table_name_2,
        materialized_database=materialized_database,
        postgres_database=postgres_database,
    )
    instance.query(
        f"INSERT INTO `{postgres_database}`.`{check_table_name_2}` SELECT number, number from numbers(10000, 10000)"
    )
    check_tables_are_synchronized(
        instance,
        check_table_name_2,
        materialized_database=materialized_database,
        postgres_database=postgres_database,
    )

    instance.restart_clickhouse()
    check_tables_are_synchronized(
        instance,
        check_table_name_1,
        materialized_database=materialized_database,
        postgres_database=postgres_database,
    )
    check_tables_are_synchronized(
        instance,
        check_table_name_2,
        materialized_database=materialized_database,
        postgres_database=postgres_database,
    )

    instance.query(
        f"DETACH TABLE `{materialized_database}`.`{check_table_name_2}` PERMANENTLY"
    )
    time.sleep(5)

    result = instance.query(f"SHOW TABLES FROM `{materialized_database}`")
    assert (
        result
        == "postgresql-replica-5\npostgresql_replica_0\npostgresql_replica_1\npostgresql_replica_2\npostgresql_replica_3\npostgresql_replica_4\n"
    )


if __name__ == "__main__":
    cluster.start()
    input("Cluster created, press any key to destroy...")
    cluster.shutdown()
