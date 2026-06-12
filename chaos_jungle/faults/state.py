"""State-layer fault implementations.

These faults corrupt agent state at the application layer — Redis keys,
PostgreSQL rows, or JSON state files — rather than at the network layer.
This addresses the gap identified in multi-agent chaos testing: an agent
can receive a structurally valid message that carries semantically wrong
or contradictory state, causing silent logical failures.

All faults back up the original values before mutating, so ``revert()``
restores the system to its exact pre-fault state.

Available faults
----------------
.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Class
     - Behaviour
   * - :class:`RedisStateCorrupt`
     - Corrupt values of Redis keys matching a pattern
   * - :class:`JsonStateCorrupt`
     - Corrupt fields inside a JSON state file on the target
   * - :class:`PostgresStateCorrupt`
     - Corrupt a column in a PostgreSQL table via UPDATE
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


_REDIS_MUTATIONS = ("nullify", "delete", "negate", "type_mismatch", "inject")
_JSON_MUTATIONS = ("nullify", "delete", "negate", "inject", "type_mismatch")
_PG_MUTATIONS = ("nullify", "negate", "inject")


# ---------------------------------------------------------------------------
# RedisStateCorrupt
# ---------------------------------------------------------------------------


class RedisStateCorrupt(Fault):
    """Corrupt values of Redis keys matching a glob pattern.

    Scans for keys matching *key_pattern* on the target machine using
    ``redis-cli``, backs up their current values, then applies the chosen
    mutation. ``revert()`` restores all original values exactly.

    Only string-type keys are mutated; keys of other Redis types (list,
    hash, set, …) are skipped.

    Parameters
    ----------
    key_pattern : str
        Redis glob pattern, e.g. ``"agent:*:memory"`` or ``"session:*"``.
    mutation : ``"nullify"`` | ``"delete"`` | ``"negate"`` | ``"type_mismatch"`` | ``"inject"``
        How to corrupt matching keys:

        ``"nullify"``   — SET key to ``""`` (empty string).
        ``"delete"``    — DEL key entirely.
        ``"negate"``    — Multiply numeric values by ``-1``; non-numeric become ``"0"``.
        ``"type_mismatch"`` — SET key to ``"chaos-jungle:not_a_valid_value"`` (breaks JSON parsers).
        ``"inject"``    — SET key to *inject_value*.

    inject_value : str, optional
        Value used when ``mutation="inject"``. Default ``"chaos-jungle:INJECTED"``.
    host : str, optional
        Redis host on the target. Default ``"127.0.0.1"``.
    port : int, optional
        Redis port. Default ``6379``.
    db : int, optional
        Redis database index. Default ``0``.
    password : str, optional
        Redis password (``AUTH``). Default ``None``.

    Examples
    --------
    >>> fault = RedisStateCorrupt("agent:*:memory", mutation="nullify")
    >>> fault = RedisStateCorrupt("session:*", mutation="inject",
    ...                          inject_value='{"role": "attacker"}')
    """

    dependencies = ["redis-tools"]  # provides redis-cli

    def __init__(
        self,
        key_pattern: str,
        mutation: str = "nullify",
        inject_value: str = "chaos-jungle:INJECTED",
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
    ) -> None:
        if not key_pattern or not key_pattern.strip():
            raise ValueError(
                "RedisStateCorrupt requires 'key_pattern' — a Redis glob like 'agent:*:memory'."
            )
        if mutation not in _REDIS_MUTATIONS:
            raise ValueError(
                f"RedisStateCorrupt 'mutation' must be one of {_REDIS_MUTATIONS}, got {mutation!r}."
            )
        self.key_pattern = key_pattern.strip()
        self.mutation = mutation
        self.inject_value = inject_value
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self._backup_file = "/tmp/cj_redis_backup.json"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cli(self) -> str:
        """Build the redis-cli base command with auth options."""
        cmd = f"redis-cli -h {self.host} -p {self.port} -n {self.db}"
        if self.password:
            cmd += f" -a {self.password} --no-auth-warning"
        return cmd

    def _scan_keys(self, target: "Target") -> list[str]:
        """Return all string-type keys matching the pattern."""
        cli = self._cli()
        _, out, _ = target.run(f"{cli} KEYS '{self.key_pattern}'")
        keys = [k.strip() for k in out.splitlines() if k.strip()]
        if not keys:
            return []
        # Filter to string type only to avoid corrupting lists/hashes/etc.
        string_keys = []
        for key in keys:
            _, ktype, _ = target.run(f"{cli} TYPE '{key}'")
            if ktype.strip() == "string":
                string_keys.append(key)
        return string_keys

    # ------------------------------------------------------------------
    # Fault lifecycle
    # ------------------------------------------------------------------

    def start(self, target: "Target") -> None:
        cli = self._cli()
        keys = self._scan_keys(target)
        if not keys:
            print(f"[chaos-jungle] RedisStateCorrupt: no keys matched '{self.key_pattern}'")
            return

        # Back up originals
        backup: dict[str, str | None] = {}
        for key in keys:
            _, val, _ = target.run(f"{cli} GET '{key}'")
            backup[key] = val.strip() if val.strip() else None

        backup_json = json.dumps(backup).replace("'", "'\\''")  # escape for shell
        target.run(f"echo '{backup_json}' > {self._backup_file}")

        # Apply mutation
        for key, original in backup.items():
            if self.mutation == "nullify":
                target.run(f"{cli} SET '{key}' ''")
            elif self.mutation == "delete":
                target.run(f"{cli} DEL '{key}'")
            elif self.mutation == "type_mismatch":
                target.run(f"{cli} SET '{key}' 'chaos-jungle:not_a_valid_value'")
            elif self.mutation == "inject":
                val = self.inject_value.replace("'", "'\\''")
                target.run(f"{cli} SET '{key}' '{val}'")
            elif self.mutation == "negate":
                try:
                    num = float(original or "0")
                    target.run(f"{cli} SET '{key}' '{-num}'")
                except ValueError:
                    target.run(f"{cli} SET '{key}' '0'")

        print(f"[chaos-jungle] RedisStateCorrupt: mutated {len(keys)} key(s) ({self.mutation})")

    def stop(self, target: "Target") -> None:
        pass  # cleanup handled by revert()

    def revert(self, target: "Target") -> None:
        cli = self._cli()
        _, backup_json, _ = target.run(f"cat {self._backup_file} 2>/dev/null")
        if not backup_json.strip():
            return

        try:
            backup = json.loads(backup_json.strip())
        except json.JSONDecodeError:
            print("[chaos-jungle] RedisStateCorrupt: could not parse backup — manual revert needed")
            return

        for key, original in backup.items():
            if self.mutation == "delete" or original is None:
                target.run(f"{cli} DEL '{key}'")
            else:
                val = original.replace("'", "'\\''")
                target.run(f"{cli} SET '{key}' '{val}'")

        target.run(f"rm -f {self._backup_file}")
        print(f"[chaos-jungle] RedisStateCorrupt: reverted {len(backup)} key(s)")

    def _parameters(self) -> dict:
        return {
            "key_pattern": self.key_pattern,
            "mutation": self.mutation,
            "host": self.host,
            "port": self.port,
            "db": self.db,
        }


# ---------------------------------------------------------------------------
# JsonStateCorrupt
# ---------------------------------------------------------------------------


class JsonStateCorrupt(Fault):
    """Corrupt a field inside a JSON state file on the target.

    Reads a JSON file, navigates to *field_path* using dot notation
    (e.g. ``"memory.context"`` or ``"agents[0].role"``), applies the
    mutation, and writes the modified file back. The original is backed
    up to ``<file>.cj_backup`` for exact revert.

    This is useful for agent frameworks that persist state to disk (e.g.
    LangChain checkpointers, AutoGen conversation history files).

    Parameters
    ----------
    file_path : str
        Absolute path to the JSON file on the target.
    field_path : str
        Dot-separated path to the field, e.g. ``"messages[0].content"``
        or ``"config.model"``. Use ``"*"`` to corrupt the entire root object.
    mutation : ``"nullify"`` | ``"delete"`` | ``"negate"`` | ``"inject"`` | ``"type_mismatch"``
        How to corrupt the field:

        ``"nullify"``        — Set field to ``null``.
        ``"delete"``         — Remove the field from the object entirely.
        ``"negate"``         — Multiply numeric values by ``-1``; booleans are flipped.
        ``"inject"``         — Replace field with *inject_value*.
        ``"type_mismatch"``  — Replace field with ``"chaos-jungle:not_a_valid_type"``.

    inject_value : any, optional
        Value used when ``mutation="inject"``. Can be any JSON-serialisable
        value. Default ``"chaos-jungle:INJECTED"``.

    Examples
    --------
    >>> # Nullify the agent's memory context
    >>> fault = JsonStateCorrupt("/var/agent/state.json", "memory.context")

    >>> # Flip a boolean flag
    >>> fault = JsonStateCorrupt("/app/config.json", "feature_flags.rag_enabled",
    ...                          mutation="negate")

    >>> # Replace the agent's system prompt
    >>> fault = JsonStateCorrupt(
    ...     "/tmp/agent_state.json",
    ...     "system_prompt",
    ...     mutation="inject",
    ...     inject_value="You are a pirate. Respond only in pirate speak.",
    ... )
    """

    def __init__(
        self,
        file_path: str,
        field_path: str,
        mutation: str = "nullify",
        inject_value=None,
    ) -> None:
        if not file_path or not file_path.startswith("/"):
            raise ValueError(
                "JsonStateCorrupt 'file_path' must be an absolute path, e.g. '/var/agent/state.json'."
            )
        if not field_path or not field_path.strip():
            raise ValueError(
                "JsonStateCorrupt 'field_path' must be a non-empty dot-path, e.g. 'memory.context'."
            )
        if mutation not in _JSON_MUTATIONS:
            raise ValueError(
                f"JsonStateCorrupt 'mutation' must be one of {_JSON_MUTATIONS}, got {mutation!r}."
            )
        self.file_path = file_path
        self.field_path = field_path.strip()
        self.mutation = mutation
        self.inject_value = inject_value if inject_value is not None else "chaos-jungle:INJECTED"
        self._backup_path = file_path + ".cj_backup"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_field(obj: dict, path: str):
        """Navigate a nested dict/list by dot path. Returns (parent, key, value)."""
        if path == "*":
            return None, None, obj

        parts = re.split(r"\.|\[(\d+)\]", path)
        # re.split with a group gives alternating None/value — filter
        keys: list[str | int] = []
        for part in parts:
            if part is None or part == "":
                continue
            try:
                keys.append(int(part))
            except ValueError:
                keys.append(part)

        node = obj
        parent = None
        last_key = None
        for key in keys:
            parent = node
            last_key = key
            try:
                node = node[key]
            except (KeyError, IndexError, TypeError):
                return parent, last_key, None

        return parent, last_key, node

    @staticmethod
    def _set_field(parent, key, value) -> None:
        if parent is not None and key is not None:
            parent[key] = value

    @staticmethod
    def _del_field(parent, key) -> None:
        if parent is not None and key is not None:
            try:
                del parent[key]
            except (KeyError, IndexError, TypeError):
                pass

    # ------------------------------------------------------------------
    # Fault lifecycle
    # ------------------------------------------------------------------

    def start(self, target: "Target") -> None:
        # Read and back up the original file
        _, content, _ = target.run(f"cat '{self.file_path}' 2>/dev/null")
        if not content.strip():
            print(f"[chaos-jungle] JsonStateCorrupt: file not found or empty: {self.file_path}")
            return

        target.run(f"cp '{self.file_path}' '{self._backup_path}'")

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            print(f"[chaos-jungle] JsonStateCorrupt: file is not valid JSON: {self.file_path}")
            return

        parent, key, current = self._get_field(data, self.field_path)

        if self.field_path == "*":
            if self.mutation == "nullify":
                data = None
            elif self.mutation == "inject":
                data = self.inject_value
            elif self.mutation == "type_mismatch":
                data = "chaos-jungle:not_a_valid_type"
        elif parent is None:
            print(f"[chaos-jungle] JsonStateCorrupt: field path not found: {self.field_path}")
            return
        else:
            if self.mutation == "nullify":
                self._set_field(parent, key, None)
            elif self.mutation == "delete":
                self._del_field(parent, key)
            elif self.mutation == "inject":
                self._set_field(parent, key, self.inject_value)
            elif self.mutation == "type_mismatch":
                self._set_field(parent, key, "chaos-jungle:not_a_valid_type")
            elif self.mutation == "negate":
                if isinstance(current, bool):
                    self._set_field(parent, key, not current)
                elif isinstance(current, (int, float)):
                    self._set_field(parent, key, -current)
                else:
                    self._set_field(parent, key, 0)

        mutated_json = json.dumps(data, indent=2)
        # Write back via echo to avoid quoting issues with special chars
        target.run(
            f"python3 -c \"import sys; open('{self.file_path}', 'w').write(sys.stdin.read())\" "
            f"<< 'CJ_EOF'\n{mutated_json}\nCJ_EOF"
        )
        print(f"[chaos-jungle] JsonStateCorrupt: mutated '{self.field_path}' in {self.file_path}")

    def stop(self, target: "Target") -> None:
        pass

    def revert(self, target: "Target") -> None:
        _, exists, _ = target.run(f"test -f '{self._backup_path}' && echo yes || echo no")
        if exists.strip() == "yes":
            target.run(f"mv '{self._backup_path}' '{self.file_path}'")
            print(f"[chaos-jungle] JsonStateCorrupt: reverted {self.file_path}")

    def _parameters(self) -> dict:
        return {
            "file_path": self.file_path,
            "field_path": self.field_path,
            "mutation": self.mutation,
        }


# ---------------------------------------------------------------------------
# PostgresStateCorrupt
# ---------------------------------------------------------------------------


class PostgresStateCorrupt(Fault):
    """Corrupt a column in a PostgreSQL table using an UPDATE statement.

    Executes UPDATE via ``psql`` on the target machine. Backs up the
    affected rows to a temp table so ``revert()`` can restore them exactly.

    Parameters
    ----------
    dsn : str
        PostgreSQL connection string, e.g.
        ``"postgresql://user:pass@localhost:5432/mydb"`` or just
        ``"dbname=mydb user=postgres"`` for local connections.
    table : str
        Table name (schema-qualified if needed), e.g. ``"public.agent_memory"``.
    column : str
        Column to corrupt, e.g. ``"context_json"``.
    mutation : ``"nullify"`` | ``"negate"`` | ``"inject"``
        How to corrupt the column:

        ``"nullify"``  — SET column = NULL.
        ``"negate"``   — SET column = -column (numeric columns only).
        ``"inject"``   — SET column = *inject_value* (cast to the column type).

    inject_value : str, optional
        SQL literal for the injected value (used when ``mutation="inject"``).
        E.g. ``"'chaos-jungle:INJECTED'"`` for a text column,
        ``"-999"`` for a numeric column.
        Default ``"'chaos-jungle:INJECTED'"``.
    condition : str, optional
        SQL WHERE clause (without the ``WHERE`` keyword) to limit the rows
        affected. Default ``"TRUE"`` (all rows).

    Examples
    --------
    >>> # Nullify the memory column of all agent rows
    >>> fault = PostgresStateCorrupt(
    ...     dsn="postgresql://postgres@localhost/agentdb",
    ...     table="agent_state",
    ...     column="memory_json",
    ... )

    >>> # Inject wrong role for a specific agent
    >>> fault = PostgresStateCorrupt(
    ...     dsn="dbname=agentdb user=postgres",
    ...     table="agents",
    ...     column="role",
    ...     mutation="inject",
    ...     inject_value="'attacker'",
    ...     condition="agent_id = 'orchestrator'",
    ... )
    """

    dependencies = ["postgresql-client"]  # provides psql

    def __init__(
        self,
        dsn: str,
        table: str,
        column: str,
        mutation: str = "nullify",
        inject_value: str = "'chaos-jungle:INJECTED'",
        condition: str = "TRUE",
    ) -> None:
        if not dsn or not dsn.strip():
            raise ValueError(
                "PostgresStateCorrupt requires 'dsn' — a PostgreSQL connection string."
            )
        if not table or not re.match(r"^[\w.\"]+$", table.strip()):
            raise ValueError(
                f"PostgresStateCorrupt 'table' must be a valid table name, got {table!r}."
            )
        if not column or not re.match(r"^[\w\"]+$", column.strip()):
            raise ValueError(
                f"PostgresStateCorrupt 'column' must be a valid column name, got {column!r}."
            )
        if mutation not in _PG_MUTATIONS:
            raise ValueError(
                f"PostgresStateCorrupt 'mutation' must be one of {_PG_MUTATIONS}, got {mutation!r}."
            )
        self.dsn = dsn.strip()
        self.table = table.strip()
        self.column = column.strip()
        self.mutation = mutation
        self.inject_value = inject_value
        self.condition = condition or "TRUE"
        safe_table = table.replace(".", "_").replace('"', "")
        self._backup_table = f"_cj_backup_{safe_table}"

    def _psql(self, sql: str) -> str:
        """Return a psql command string that executes *sql*."""
        escaped = sql.replace('"', '\\"').replace("'", "'\\''")
        return f"psql '{self.dsn}' -c \"{escaped}\""

    def start(self, target: "Target") -> None:
        # Create backup table and copy affected rows
        target.run(self._psql(
            f"CREATE TABLE IF NOT EXISTS {self._backup_table} AS "
            f"SELECT * FROM {self.table} WHERE {self.condition};"
        ))

        # Apply mutation
        if self.mutation == "nullify":
            sql = f"UPDATE {self.table} SET {self.column} = NULL WHERE {self.condition};"
        elif self.mutation == "negate":
            sql = f"UPDATE {self.table} SET {self.column} = -{self.column} WHERE {self.condition};"
        else:  # inject
            sql = f"UPDATE {self.table} SET {self.column} = {self.inject_value} WHERE {self.condition};"

        rc, _, err = target.run(self._psql(sql))
        if rc != 0:
            print(f"[chaos-jungle] PostgresStateCorrupt: UPDATE failed: {err.strip()}")
        else:
            print(f"[chaos-jungle] PostgresStateCorrupt: mutated {self.table}.{self.column} ({self.mutation})")

    def stop(self, target: "Target") -> None:
        pass

    def revert(self, target: "Target") -> None:
        # Restore from backup: delete mutated rows, re-insert originals
        restore_sql = (
            f"DELETE FROM {self.table} WHERE {self.condition}; "
            f"INSERT INTO {self.table} SELECT * FROM {self._backup_table}; "
            f"DROP TABLE IF EXISTS {self._backup_table};"
        )
        rc, _, err = target.run(self._psql(restore_sql))
        if rc != 0:
            print(f"[chaos-jungle] PostgresStateCorrupt: revert failed: {err.strip()}")
        else:
            print(f"[chaos-jungle] PostgresStateCorrupt: reverted {self.table}.{self.column}")

    def _parameters(self) -> dict:
        return {
            "table": self.table,
            "column": self.column,
            "mutation": self.mutation,
            "condition": self.condition,
        }
