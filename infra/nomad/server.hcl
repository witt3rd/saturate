# Nomad server — roger
#
# RECONSTRUCTED 2026-08-05 from the live agent's /v1/agent/self API. The original
# lived only in the `saturate-wt-5a` git worktree, which was deleted; it was never
# committed on any branch, so this file is a recovery, not a restore. It reproduces
# the config the agent (PID 4616, up since 2026-07-13) was actually running.
#
# Single-server control plane for distributed compute. The compute client is the
# remote node `gx10`, which joins over Tailscale — hence the 100.65.x bind address.

region     = "global"
datacenter = "dc1"

data_dir  = "/home/dt/.nomad/data"
log_level = "INFO"

# ⚠ Tailscale address, not loopback and not 0.0.0.0. The gx10 client reaches the
# server here, so this must stay a tailnet-reachable address. If roger's tailnet IP
# ever changes, update this and restart — the agent will fail to bind otherwise.
bind_addr = "100.65.5.5"

advertise {
  http = "100.65.5.5:4646"
  rpc  = "100.65.5.5:4647"
  serf = "100.65.5.5:4648"
}

ports {
  http = 4646
  rpc  = 4647
  serf = 4648
}

server {
  enabled          = true
  bootstrap_expect = 1
}

# roger is control plane only — it runs no workloads itself.
client {
  enabled = false
}

ui {
  enabled = true
}

# ACLs are OFF. The tailnet is the only access control; do not expose 4646 on a
# public interface without turning ACLs on first.
acl {
  enabled = false
}
