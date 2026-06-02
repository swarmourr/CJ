"""
Shared configuration for all scenarios.
Edit this file with your node IPs and credentials.
"""

# ── SSH credentials ────────────────────────────────────────────────────────
USER     = "hamza"
PASSWORD = "si "          # set to "" to use SSH key
SSH_KEY  = ""             # e.g. "~/.ssh/id_rsa" — ignored if PASSWORD is set

# ── Nodes ──────────────────────────────────────────────────────────────────
# Add your node IPs here
NODES = {
    "node1": "hamza.ads.isi.edu",
}

# Src nodes (serve files via Apache2)
SRC_NODES  = ["hamza.ads.isi.edu"]

# Dest nodes (wget + diff)
DEST_NODES = ["hamza.ads.isi.edu"]

# ── Paths on remote nodes ──────────────────────────────────────────────────
SITE_DIR     = "/var/www/iris"
IRIS_DIR     = "/root/iris"
TEMPLATE_DIR = "/root/iris/testdata/20190425T121649-0700"
CJ_DIR       = "~/chaos-jungle"

# ── Results ────────────────────────────────────────────────────────────────
RESULTS_DIR  = "./results"
