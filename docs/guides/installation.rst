Installation and verification
=============================

Chaos Jungle requires Python 3.9 or newer. Use a virtual environment so the
package and its dependencies do not change the system Python installation.

Install the current source
--------------------------

Until a PyPI release is available, install from the GitHub repository:

.. code-block:: bash

   python3 --version  # confirm Python 3.9 or newer
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install git+https://github.com/swarmourr/CJ.git

On Windows, activate the environment with ``.venv\Scripts\activate``. If
``python3`` selects an older interpreter, use the command for a Python 3.9+
installation when creating the environment.

For development from a local checkout, replace the last command with
``python -m pip install -e ".[dev]"``. After the first PyPI release, users can
install the published package with ``python -m pip install chaos-jungle``.

Verify the installation
-----------------------

.. code-block:: bash

   python -c "import chaos_jungle; print(chaos_jungle.__version__)"
   chaos-jungle --help

These checks verify that Python can import the package and that its CLI entry
point is installed. They do not test fault delivery. To run the core tests in
a development checkout, use ``python -m pytest tests/ -q --strict-markers``.

Target requirements
-------------------

In-process HTTP interception can run without Linux privileges. Infrastructure
faults act on a target machine or container and may require Linux utilities,
container access, or elevated privileges. Check the relevant fault guide and
run preflight before applying a fault. Installing the Python package alone
does not install system tools on remote targets.

For a local example, continue with :doc:`../quickstart`. For the GitLab build,
distribution checks, and PyPI release procedure, see :doc:`../publishing`.
