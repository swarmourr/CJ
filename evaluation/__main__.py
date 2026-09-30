"""Allow ``python -m evaluation.run`` and ``python -m evaluation``."""
from evaluation.run import main
import sys

sys.exit(main())
