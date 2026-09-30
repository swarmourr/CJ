"""CJ evaluation package — reproducible agent system fault-injection evaluation.

CJ commit: 7f5a6fdbee72ce86b7a44f74ee73391e7f5ccc09

AgentChaos reference:
  Paper  : https://arxiv.org/abs/2608.06790
  Repo   : https://github.com/floritange/AgentChaos
  Note   : The adapters in this package are independent implementations of the
           same architectures (AutoGen-style, MAD-style, MapCoder-style).
           The original AgentChaos repository was inspected for architecture
           descriptions; the license (MIT) permits reuse, but the adapter code
           here was written from scratch against CJ's interfaces to avoid
           transitive dependency conflicts.  Every deviation from the published
           architecture is documented in the respective adapter module.

Environment variables:
  CJ_EVAL_BASE_URL    OpenAI-compatible endpoint base URL
  CJ_EVAL_API_KEY     API key (default: "dummy" for local/mock servers)
  CJ_EVAL_MODEL       Model identifier (default: "gpt-4o-mini")
  CJ_EVAL_TEMPERATURE Sampling temperature (default: 0.0 for determinism)
"""

__version__ = "0.1.0"
