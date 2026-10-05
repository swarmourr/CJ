#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  evaluation/docker/clean_images.sh [options]

Remove old local CJ evaluation Docker image tags. By default this only targets
the cj-eval-agent repository and keeps the current Git commit tag.

Options:
  --image-name NAME      Docker image repository name. Default: cj-eval-agent
  --keep REF            Image ref to keep. Default: image-name:current-git-commit
  --all                 Remove all tags for the image name, including --keep
  --force               Pass --force to docker image rm
  --prune-dangling      Also run docker image prune --filter dangling=true
  --dry-run             Print what would be removed without deleting anything
  -h, --help            Show this help

Examples:
  evaluation/docker/clean_images.sh --dry-run
  evaluation/docker/clean_images.sh --force --prune-dangling
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

image_name="${CJ_EVAL_IMAGE_NAME:-cj-eval-agent}"
keep_ref=""
remove_all=0
force=0
prune_dangling=0
dry_run=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-name)
      image_name="${2:?--image-name requires a value}"
      shift 2
      ;;
    --keep)
      keep_ref="${2:?--keep requires a value}"
      shift 2
      ;;
    --all)
      remove_all=1
      shift
      ;;
    --force)
      force=1
      shift
      ;;
    --prune-dangling)
      prune_dangling=1
      shift
      ;;
    --dry-run)
      dry_run=1
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

if [[ -z "${keep_ref}" ]]; then
  keep_ref="${image_name}:$(git rev-parse HEAD)"
fi

refs=()
image_refs_output="$(docker image ls "${image_name}" --format '{{.Repository}}:{{.Tag}}')"
while IFS= read -r ref; do
  [[ -n "${ref}" && "${ref}" != *":<none>" ]] && refs+=("${ref}")
done <<< "${image_refs_output}"

remove_refs=()
for ref in "${refs[@]+"${refs[@]}"}"; do
  if [[ "${remove_all}" -eq 0 && "${ref}" == "${keep_ref}" ]]; then
    continue
  fi
  remove_refs+=("${ref}")
done

echo "[cj-eval] image name : ${image_name}"
echo "[cj-eval] keep ref   : ${keep_ref}"
if [[ "${#remove_refs[@]}" -eq 0 ]]; then
  echo "[cj-eval] no matching image tags to remove"
else
  echo "[cj-eval] image tags to remove:"
  printf '  %s\n' "${remove_refs[@]}"
  if [[ "${dry_run}" -eq 0 ]]; then
    args=(image rm)
    if [[ "${force}" -eq 1 ]]; then
      args+=(--force)
    fi
    docker "${args[@]}" "${remove_refs[@]}"
  fi
fi

if [[ "${prune_dangling}" -eq 1 ]]; then
  if [[ "${dry_run}" -eq 1 ]]; then
    echo "[cj-eval] would prune dangling images"
  else
    docker image prune --force --filter dangling=true
  fi
fi
