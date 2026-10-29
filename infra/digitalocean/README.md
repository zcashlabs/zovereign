# DigitalOcean infrastructure

This OpenTofu stack is the replacement target for the current AWS deployment.
It creates a project, VPC, SSH key, Droplet, firewall, persistent XFS volume,
and—once Spaces credentials are supplied—a versioned Space with bounded
retention and a CDN endpoint.

No credentials belong in `.tf` or `.tfvars` files. Export them for the current
shell:

```bash
export TF_VAR_digitalocean_token="$(doctl auth token --context lightwalletd)"
export TF_VAR_ssh_public_key="$(cat ~/.ssh/github_ed25519.pub)"
export TF_VAR_admin_cidrs='["203.0.113.10/32"]'
```

Initialize and review the compute-only plan:

```bash
tofu init
tofu plan -out=migration.tfplan
tofu show migration.tfplan
```

The initial plan keeps `enable_spaces = false`. After creating a Space-scoped
key, export `TF_VAR_spaces_access_id` and `TF_VAR_spaces_secret_key`, set
`TF_VAR_enable_spaces=true`, and generate a second plan.

Applying the stack creates billable resources. Do not apply a plan without
reviewing the selected region, Droplet size, volume size, Space name, and SSH
source CIDRs.

The default `blr1` Droplet is the dedicated-CPU `g-4vcpu-16gb`, currently
$126/month. The 500-GiB volume adds $50/month and has `prevent_destroy`
enabled. Spaces is planned separately after credentials exist and starts at
$5/month.
