# Building and publishing Chaos Jungle

The GitLab pipeline runs the core test suite on Python 3.9-3.12, checks Ruff,
builds a wheel and source distribution, validates their metadata, and installs
the wheel for a smoke test. Download the `build_dist` job artifacts to inspect
the exact files that would be published. Runtime scripts under
`chaos_jungle/scripts/`, including `bpf/flow_modify.c`, must remain in the wheel.
The `docs_build` job also renders this Sphinx manual and makes its HTML output
available as a GitLab job artifact for review.

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
