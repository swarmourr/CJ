#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  evaluation/docker/rebuild_image.sh [options]

Remove old local CJ evaluation Docker image tags and build a fresh image for
the current Git commit.

Options:
  --image-name NAME      Docker image repository name. Default: cj-eval-agent
  --tag TAG             Docker tag. Default: current git commit SHA
  --commit COMMIT       Build-time CJ_COMMIT value. Default: current git commit SHA
  --force-remove        Pass --force to docker image rm for old image tags
  --keep-old            Do not remove existing image tags before building
  --no-cache            Build without Docker layer cache
  -h, --help            Show this help

Examples:
  evaluation/docker/rebuild_image.sh
  evaluation/docker/rebuild_image.sh --force-remove
  evaluation/docker/rebuild_image.sh --tag local-test --no-cache
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

image_name="${CJ_EVAL_IMAGE_NAME:-cj-eval-agent}"
commit=""
tag=""
force_remove=0
keep_old=0
no_cache=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-name)
      image_name="${2:?--image-name requires a value}"
      shift 2
      ;;
    --tag)
      tag="${2:?--tag requires a value}"
      shift 2
      ;;
    --commit)
      commit="${2:?--commit requires a value}"
      shift 2
      ;;
    --force-remove)
      force_remove=1
      shift
      ;;
    --keep-old)
      keep_old=1
      shift
      ;;
    --no-cache)
      no_cache=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "${repo_root}"

if [[ -z "${commit}" ]]; then
  commit="$(git rev-parse HEAD)"
fi
if [[ -z "${tag}" ]]; then
  tag="${commit}"
fi

new_ref="${image_name}:${tag}"

echo "[cj-eval] repo root : ${repo_root}"
echo "[cj-eval] commit    : ${commit}"
echo "[cj-eval] image     : ${new_ref}"

if [[ "${keep_old}" -eq 0 ]]; then
  mapfile -t old_refs < <(
    docker image ls "${image_name}" --format '{{.Repository}}:{{.Tag}}' \
      | grep -v ':<none>$' || true
  )

  if [[ "${#old_refs[@]}" -gt 0 ]]; then
    echo "[cj-eval] removing old local image tags:"
    printf '  %s\n' "${old_refs[@]}"
    remove_args=(image rm)
    if [[ "${force_remove}" -eq 1 ]]; then
      remove_args+=(--force)
    fi
    docker "${remove_args[@]}" "${old_refs[@]}"
  else
    echo "[cj-eval] no old local ${image_name} image tags found"
  fi
else
  echo "[cj-eval] keeping old local ${image_name} image tags"
fi

build_args=(
  build
  -f evaluation/docker/Dockerfile
  --build-arg "CJ_COMMIT=${commit}"
  -t "${new_ref}"
)
if [[ "${no_cache}" -eq 1 ]]; then
  build_args+=(--no-cache)
fi
build_args+=(.)

echo "[cj-eval] building ${new_ref}"
docker "${build_args[@]}"

echo "[cj-eval] built image:"
docker image ls "${image_name}" --format '  {{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}'
