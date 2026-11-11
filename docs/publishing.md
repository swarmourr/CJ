# Building and publishing Chaos Jungle

The GitLab pipeline runs the core test suite on Python 3.9-3.12, checks Ruff,
builds a wheel and source distribution, validates their metadata, and installs
the wheel for a smoke test. Download the `build_dist` job artifacts to inspect
the exact files that would be published. Runtime scripts under
`chaos_jungle/scripts/`, including `bpf/flow_modify.c`, must remain in the wheel.
The `docs_build` job also renders this Sphinx manual and makes its HTML output
available as a GitLab job artifact for review.

The optional `real_experiment` job runs a bounded integration experiment in its
disposable CI container. It makes real HTTP calls through CJ's model proxy to
a local, deterministic OpenAI-compatible service, first without a fault and
then with a latency fault. It does not call an external LLM or require API
credentials. Start it manually from a branch pipeline. Its
`ci-artifacts/real-experiment/` artifact contains the SQLite session database,
`metrics.json` (including the fault's declared default metrics and exposure
report), and `summary.txt`. The job checks fault manifestation and cleanup; it
is optional, so a skipped or failed experiment does not block packaging.

For an actual model endpoint, set `CJ_EVAL_BASE_URL` to an OpenAI-compatible
base URL ending in `/v1`, `CJ_EVAL_MODEL` to a model name, and
`CJ_EVAL_API_KEY` to a masked GitLab CI/CD variable. Then start the separate
manual `real_llm_experiment` job. It sends four baseline and four fault requests
with a small token limit and adds a 250 ms model-proxy delay. Its artifact has
the same database, JSON metrics, and summary as the local job. The endpoint
must be reachable from the Docker runner; loopback URLs refer to the job
container, not your laptop. The database captures the synthetic prompt and
model response, so restrict artifact access if those responses are sensitive.
Do not place credentials in the base URL.
Both jobs are integration smoke checks, not benchmark estimates; their two
trials per condition are too few for stable latency percentiles or confidence
intervals.

The source distribution is limited to the package, README, license, and build
metadata. It does not need experiment archives, evaluation data, local virtual
environments, secrets, or generated results. The `.gitignore` file prevents
local artifacts from being committed; the Hatchling `sdist` allowlist controls
what enters a source release. Check both before each release.

To test a release locally, use a virtual environment with Python 3.9 or newer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m pip install dist/*.whl
chaos-jungle --help
```

For PyPI publication from GitLab.com:

1. Push this repository to a GitLab project and confirm its pipeline passes.
2. Check that the `chaos-jungle` name is available on PyPI and configure a
   PyPI Trusted Publisher for the GitLab namespace, project, and
   `.gitlab-ci.yml` pipeline. Protect release tags and the `pypi` environment.
3. Set the version in `pyproject.toml` and `chaos_jungle/__init__.py` to the
   intended release. Remove the current local-only notice from the README and
   review its public installation instructions; the publish job checks this.
4. Create a matching `vX.Y.Z` tag. After all tests and distribution checks
   pass, explicitly start the manual `publish_pypi` job.

The publish job uses GitLab's OIDC token. It requires a GitLab.com project and
the matching PyPI Trusted Publisher; it does not publish from a normal branch
pipeline. No PyPI credentials should be committed to the repository. After a
successful release, users can install with `python -m pip install chaos-jungle`.
