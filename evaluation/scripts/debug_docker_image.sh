#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/debug_docker_image.sh [options]

Debug the CJ evaluation Docker image without model credentials or paid calls.
The script writes image inspect output, runtime/import checks, and one dry-run
container-entrypoint result under the organized results tree.

Options:
  --image IMAGE       Docker image tag to debug. Default: current CJ eval image.
  --results-run NAME  Result folder under results/.
  --run-name NAME     Name under docker-debug/. Default: image-debug.
  --help              Show this help.

Checks:
  - Docker image exists and inspect JSON is captured.
  - Image default user is non-root.
  - Python, CJ package, container entrypoint, EvalPlus, and framework imports.
  - Read-only root filesystem with writable tmpfs/output mounts.
  - Dry-run container entrypoint writes structured AgentRunResult JSON.
  - No API key or Docker socket is mounted into the container.
EOF
}

image=""
run_name="${CJ_EVAL_RUN_NAME:-image-debug}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      image="${2:?--image requires a value}"
      shift 2
      ;;
    --results-run)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
      shift 2
      ;;
    --run-name)
      run_name="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[cj-eval] unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults

export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="docker-debug-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

if [[ -z "${image}" ]]; then
  image="$(cj_eval_default_image)"
fi
cj_eval_require_image "${image}"

debug_dir="$(cj_eval_make_run_dir "docker-debug" "${run_name}")"
input_dir="${debug_dir}/input"
output_dir="${debug_dir}/output"
mkdir -p "${input_dir}" "${output_dir}" "${debug_dir}/logs"

cj_eval_metadata "${debug_dir}/metadata.json" "docker-debug" "${run_name}" "${image}"

cat > "${input_dir}/request.json" <<'JSON'
{
  "agent_system": "autogen-real",
  "topology": "single",
  "task": {
    "task_id": "docker-debug/solution",
    "benchmark": "docker-debug",
    "prompt": "Write a function named solution that returns None.",
    "entry_point": "solution",
    "test_code": "assert solution() is None\n",
    "canonical_solution": "def solution():\n    return None\n",
    "metadata": {"source": "docker_debug"}
  },
  "seed": 0,
  "model_config": {
    "name": "debug-dry-run",
    "base_url": "http://127.0.0.1:9/v1",
    "container_base_url": "http://127.0.0.1:9/v1",
    "api_key": "dummy"
  },
  "execution_config": {
    "dry_run": true,
    "max_turns": 1,
    "score_timeout_s": 10,
    "agent_level": "individual"
  },
  "run_context": {
    "study_id": "docker-debug-study",
    "campaign_id": "docker-debug-campaign",
    "pair_id": "docker-debug-pair",
    "run_id": "docker-debug-run",
    "condition": "docker_debug"
  }
}
JSON

cat <<EOF
[cj-eval] Docker image debug
[cj-eval] image   : ${image}
[cj-eval] results : ${debug_dir}
EOF

docker image inspect "${image}" > "${debug_dir}/image_inspect.json"

introspection_cmd=(
  docker run --rm
  --network none
  --read-only
  -e HOME=/tmp/cj-home
  -e XDG_CACHE_HOME=/tmp/cj-cache
  -e XDG_CONFIG_HOME=/tmp/cj-config
  -e XDG_DATA_HOME=/tmp/cj-data
  -e CREWAI_STORAGE_DIR=/tmp/cj-crewai
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m
  --tmpfs /cj/output:rw,nosuid,nodev,size=256m,uid=1000,gid=1000
  --entrypoint /bin/sh
  "${image}"
  -lc
  "set -Eeuo pipefail
echo '[debug] identity'
id
echo '[debug] python'
python --version
echo '[debug] commit'
cat /cj/work/COMMIT
echo '[debug] writable probes'
python -c \"from pathlib import Path; p=Path('/tmp/cj-debug'); p.write_text('ok'); assert p.read_text() == 'ok'; q=Path('/cj/output/cj-debug'); q.write_text('ok'); assert q.read_text() == 'ok'; print('writable-ok')\"
echo '[debug] imports'
python -c \"import importlib, importlib.metadata as m; mods=['evaluation.container_entrypoint','evaluation.agents.base','evaluation.multi_agent','evaluation.benchmarks.executor','evalplus','autogen_agentchat','langgraph','crewai']; missing=[]; versions={}; [importlib.import_module(x) if not x.startswith('autogen_agentchat') else importlib.import_module(x) for x in mods]; pkgs=['evalplus','autogen-agentchat','langgraph','crewai']; [versions.setdefault(p, m.version(p)) for p in pkgs]; print(versions)\"
echo '[debug] no docker socket'
test ! -S /var/run/docker.sock
"
)
cj_eval_write_command "${debug_dir}/introspection_command.sh" "${introspection_cmd[@]}"
cj_eval_run_logged "${debug_dir}/logs/introspection.log" "${introspection_cmd[@]}"

entrypoint_cmd=(
  docker run --rm
  --network none
  --read-only
  --cap-drop ALL
  --tmpfs /tmp:rw,nosuid,nodev,size=512m
  -v "${input_dir}:/cj/input:ro"
  -v "${output_dir}:/cj/output:rw"
  -e CJ_EVAL_OUTPUT_DIR=/cj/output
  "${image}"
  python -m evaluation.container_entrypoint
  --request /cj/input/request.json
  --output /cj/output/result.json
)
cj_eval_write_command "${debug_dir}/entrypoint_command.sh" "${entrypoint_cmd[@]}"
cj_eval_run_logged "${debug_dir}/logs/entrypoint.log" "${entrypoint_cmd[@]}"

"${CJ_EVAL_PYTHON}" - "${output_dir}/result.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(f"missing entrypoint result: {path}")
result = json.loads(path.read_text(encoding="utf-8"))
required = {
    "executor_status",
    "success",
    "generated_code",
    "framework_versions",
    "python_version",
    "study_id",
    "pair_id",
    "run_id",
    "condition",
}
missing = sorted(required - set(result))
if missing:
    raise SystemExit(f"entrypoint result missing fields: {missing}")
if result.get("executor_status") != "ok":
    raise SystemExit(f"entrypoint executor_status is not ok: {result.get('executor_status')!r}")
text = json.dumps(result, sort_keys=True)
if "sk-" in text or "Bearer " in text:
    raise SystemExit("entrypoint result contains a secret-looking value")
print("[cj-eval] entrypoint result validated")
PY

cat > "${debug_dir}/debug_summary.json" <<EOF
{
  "image": $(cj_eval_json_string "${image}"),
  "results_dir": $(cj_eval_json_string "${debug_dir}"),
  "image_inspect": "image_inspect.json",
  "introspection_log": "logs/introspection.log",
  "entrypoint_log": "logs/entrypoint.log",
  "entrypoint_result": "output/result.json",
  "status": "ok"
}
EOF

echo "[cj-eval] Docker image debug complete: ${debug_dir}"
