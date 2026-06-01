"""User-defined metric helpers.

Two ways to define application-specific metrics without subclassing:

1. ``@metric`` decorator — wrap a plain function into a Metric instance.
2. ``ScriptMetric``     — run a shell/Python script on the target and
                          parse its stdout automatically.

Both integrate directly with ``@chaos_measure`` and the built-in metrics
pipeline (baseline + chaos collection, CSV export, dashboard).

Examples
--------
Function-based metric::

    from chaos_jungle.metrics import metric

    @metric("throughput")
    def my_throughput(target):
        _, out, _ = target.run("iperf3 -c 10.0.0.1 -t 5 -J")
        import json
        data = json.loads(out)
        return {"mbps": data["end"]["sum"]["bits_per_second"] / 1e6}

    @chaos_measure(NetworkDelay("100ms"), metrics=[my_throughput])
    def run():
        run_pipeline()

Script-based metric (local script uploaded & run on target)::

    from chaos_jungle.metrics import ScriptMetric

    m = ScriptMetric("app", script="./scripts/measure_app.sh")

    @chaos_measure(NetworkDelay("100ms"), metrics=[m])
    def run():
        run_pipeline()

The script should print either JSON or ``key=value`` lines::

    # measure_app.sh
    echo '{"error_rate": 0.02, "throughput_mbps": 850.3}'
    # or:
    echo "error_rate=0.02"
    echo "throughput_mbps=850.3"
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Callable, TYPE_CHECKING

from chaos_jungle.metrics.base import Metric

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


# ---------------------------------------------------------------------------
# Global metric registry  {name: Metric}
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Metric] = {}


def get_metric(name: str) -> Metric | None:
    """Look up a registered metric by name."""
    return _REGISTRY.get(name)


def list_metrics() -> dict[str, Metric]:
    """Return all registered metrics."""
    return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# @metric decorator
# ---------------------------------------------------------------------------

def metric(name_or_fn=None, *, name: str = ""):
    """Decorator that turns a function into a :class:`~chaos_jungle.metrics.base.Metric`.

    The decorated function must accept a single ``target`` argument and
    return a plain ``dict``.  The result is registered globally and can be
    passed directly to ``@chaos_measure(metrics=[...])`` or
    :class:`~chaos_jungle.runner.ChaosRunner`.

    Parameters
    ----------
    name_or_fn : str or callable
        Either the metric name (when used as ``@metric("name")``) or the
        function itself (when used as bare ``@metric``).
    name : str, keyword-only
        Metric name when using the keyword form ``@metric(name="foo")``.

    Examples
    --------
    With an explicit name::

        from chaos_jungle.metrics import metric

        @metric("throughput")
        def measure_throughput(target):
            _, out, _ = target.run("iperf3 -c server -t 5 -J")
            data = json.loads(out)
            return {"mbps": data["end"]["sum"]["bits_per_second"] / 1e6}

    Using the function name automatically::

        @metric
        def error_rate(target):
            _, out, _ = target.run("curl -sf http://localhost/metrics | grep errors")
            return {"per_s": float(out.split()[-1])}

    Keyword form::

        @metric(name="connections")
        def open_connections(target):
            _, out, _ = target.run("ss -tn | grep ESTAB | wc -l")
            return {"count": int(out.strip() or 0)}

    Use it with ``@chaos_measure``::

        from chaos_jungle.decorators import chaos_measure
        from chaos_jungle.faults import NetworkDelay

        @chaos_measure(NetworkDelay("100ms"), metrics=[measure_throughput])
        def run_experiment():
            run_pipeline()
            return {"retries": 3}
    """

    def _wrap(fn: Callable, metric_name: str) -> "Metric":
        if not callable(fn):
            raise TypeError(f"@metric: expected a callable, got {type(fn).__name__!r}")
        if not metric_name:
            raise ValueError("@metric: 'name' must be a non-empty string.")

        _fn = fn  # capture in closure

        class _FunctionMetric(Metric):
            name = metric_name
            __doc__ = _fn.__doc__

            def collect(self, target) -> dict:  # type: ignore[override]
                return _fn(target)

        _FunctionMetric.__name__ = fn.__name__
        _FunctionMetric.__qualname__ = fn.__qualname__

        instance = _FunctionMetric()
        _REGISTRY[metric_name] = instance
        return instance

    # @metric  (bare, no parens)
    if callable(name_or_fn):
        return _wrap(name_or_fn, name_or_fn.__name__)

    # @metric("name") or @metric(name="name")
    resolved_name = name_or_fn or name
    if not resolved_name:
        raise ValueError("@metric requires a name: @metric('my_name') or @metric(name='my_name')")

    def decorator(fn: Callable) -> "Metric":
        return _wrap(fn, resolved_name)

    return decorator


# ---------------------------------------------------------------------------
# ScriptMetric
# ---------------------------------------------------------------------------

_PARSE_MODES = ("auto", "json", "keyvalue")


class ScriptMetric(Metric):
    """Run a script on the target machine and parse its stdout as metrics.

    The script is uploaded from the controller machine to the target at
    ``collect()`` time (if ``script`` is a local path).  If ``script``
    is already on the target (``remote_script``), it is executed directly.

    Output format
    -------------
    The script must print to stdout in one of two formats:

    **JSON** (recommended)::

        {"error_rate": 0.02, "throughput_mbps": 850.3, "latency_ms": 12.4}

    **key=value pairs** (one per line)::

        error_rate=0.02
        throughput_mbps=850.3
        latency_ms=12.4

    Lines that cannot be parsed are silently ignored.

    Parameters
    ----------
    name : str
        Metric name prefix (e.g. ``"app"`` → keys become ``app_error_rate``).
    script : str, optional
        Path to a **local** script file to upload and run on the target.
        Supports ``.sh`` (bash), ``.py`` (python3), or any executable.
    remote_script : str, optional
        Path to a script **already on the target**. Used instead of
        uploading.  Mutually exclusive with ``script``.
    interpreter : str, optional
        Interpreter to use when running the script on the target.
        Default ``"bash"`` for ``.sh`` files, ``"python3"`` for ``.py``.
        Set to ``""`` to run the script directly (must be executable).
    parse : ``"auto"`` | ``"json"`` | ``"keyvalue"``
        How to parse the script's stdout.
        ``"auto"`` (default) tries JSON first, then falls back to
        key=value parsing.
    extra_args : str, optional
        Extra arguments appended to the script invocation.

    Examples
    --------
    Local shell script::

        m = ScriptMetric("app", script="./scripts/measure.sh")

    Local Python script::

        m = ScriptMetric("app", script="./scripts/measure.py")

    Script already on the target::

        m = ScriptMetric("app", remote_script="/opt/app/metrics.sh")

    With arguments::

        m = ScriptMetric("db", script="./measure_db.sh",
                         extra_args="--host localhost --port 5432")

    Use with ``@chaos_measure``::

        @chaos_measure(NetworkDelay("100ms"), metrics=[m])
        def run():
            run_pipeline()
    """

    def __init__(
        self,
        name: str,
        script: str = "",
        remote_script: str = "",
        interpreter: str = "auto",
        parse: str = "auto",
        extra_args: str = "",
    ) -> None:
        if not name or not name.strip():
            raise ValueError("ScriptMetric 'name' must be a non-empty string.")
        if script and remote_script:
            raise ValueError(
                "ScriptMetric: provide either 'script' (local) or "
                "'remote_script' (already on target), not both."
            )
        if not script and not remote_script:
            raise ValueError(
                "ScriptMetric: provide 'script' (local path) or "
                "'remote_script' (path on target)."
            )
        if parse not in _PARSE_MODES:
            raise ValueError(
                f"ScriptMetric 'parse' must be one of {_PARSE_MODES}, got {parse!r}."
            )

        self.name = name.strip()
        self.script = script
        self.remote_script = remote_script
        self.interpreter = interpreter
        self.parse = parse
        self.extra_args = extra_args
        self._uploaded_remote: str | None = None  # cache after first upload

    # ------------------------------------------------------------------

    def _resolve_interpreter(self, script_path: str) -> str:
        """Pick interpreter based on file extension if set to 'auto'."""
        if self.interpreter != "auto":
            return self.interpreter
        ext = os.path.splitext(script_path)[1].lower()
        return {"py": "python3", "sh": "bash", "bash": "bash"}.get(ext.lstrip("."), "bash")

    def _upload(self, target: "Target") -> str:
        """Upload local script to target if needed. Return remote path."""
        if self._uploaded_remote:
            return self._uploaded_remote

        local = os.path.expanduser(self.script)
        if not os.path.isfile(local):
            raise FileNotFoundError(
                f"ScriptMetric: local script not found: {local!r}\n"
                f"  Fix: check the path or use remote_script= if it is already on the target."
            )

        # Upload to ~/.chaos-jungle/metrics/ on the target
        _, home, _ = target.run("echo $HOME")
        home = home.strip() or "/root"
        remote_dir = f"{home}/.chaos-jungle/metrics"
        target.run(f"mkdir -p {remote_dir}")

        fname = os.path.basename(local)
        remote_path = f"{remote_dir}/{fname}"
        target.put(local, remote_path)
        target.run(f"chmod +x {remote_path}")

        self._uploaded_remote = remote_path
        return remote_path

    def collect(self, target: "Target") -> dict:
        """Upload (if needed) and run the script; parse and return stdout."""
        if self.remote_script:
            script_path = self.remote_script
        else:
            script_path = self._upload(target)

        interp = self._resolve_interpreter(script_path)
        cmd = f"{interp} {script_path} {self.extra_args}".strip() if interp else \
              f"{script_path} {self.extra_args}".strip()

        _, stdout, stderr = target.run(cmd)

        return self._parse_output(stdout.strip(), stderr.strip())

    def _parse_output(self, stdout: str, stderr: str) -> dict:
        """Parse script stdout as JSON or key=value."""
        if not stdout:
            return {"error": f"no output (stderr: {stderr[:120]})"}

        mode = self.parse

        # Try JSON
        if mode in ("auto", "json"):
            try:
                data = json.loads(stdout)
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if isinstance(v, (int, float, str, bool))}
            except json.JSONDecodeError:
                if mode == "json":
                    return {"parse_error": f"invalid JSON: {stdout[:80]}"}

        # key=value
        result: dict = {}
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip().replace(" ", "_")
                val = val.strip()
                try:
                    result[key] = int(val)
                except ValueError:
                    try:
                        result[key] = float(val)
                    except ValueError:
                        result[key] = val
        if not result:
            return {"raw": stdout[:200]}
        return result
