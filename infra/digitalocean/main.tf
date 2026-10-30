locals {
  name            = "lightwalletd-snapshots"
  snapshot_prefix = "mainnet/v0.6.0"
  common_tags     = ["lightwalletd-snapshots", "mainnet", "managed-by-opentofu"]
}

resource "digitalocean_project" "this" {
  name        = local.name
  description = "Zcash lightwalletd source node and daily public snapshot publication"
  purpose     = "Service or API"
  environment = "Production"
}

resource "digitalocean_vpc" "this" {
  name     = local.name
  region   = var.region
  ip_range = "10.42.0.0/20"
}

resource "digitalocean_ssh_key" "operator" {
  name       = "${local.name}-operator"
  public_key = trimspace(var.ssh_public_key)
}

resource "digitalocean_volume" "data" {
  region      = var.region
  name        = "lightwalletd-data"
  size        = var.volume_size_gib
  description = "Persistent Zakura and lightwalletd state plus snapshot workspace"
  tags        = local.common_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_droplet" "node" {
  name              = "lightwalletd-mainnet-${var.region}-1"
  image             = "ubuntu-24-04-x64"
  region            = var.region
  size              = var.droplet_size
  vpc_uuid          = digitalocean_vpc.this.id
  ssh_keys          = [digitalocean_ssh_key.operator.fingerprint]
  volume_ids        = [digitalocean_volume.data.id]
  ipv6              = true
  monitoring        = true
  backups           = false
  graceful_shutdown = true
  tags              = local.common_tags

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    volume_name = digitalocean_volume.data.name
  })

  lifecycle {
    # Cloud-init is bootstrap-only. Later edits must not replace a stateful node.
    ignore_changes = [user_data]
  }
}

resource "digitalocean_firewall" "node" {
  name        = "${local.name}-node"
  droplet_ids = [digitalocean_droplet.node.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.admin_cidrs
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

resource "digitalocean_spaces_bucket" "snapshots" {
  count = var.enable_spaces ? 1 : 0

  name   = var.space_name
  region = var.region
  acl    = "public-read"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    id      = "expire-timestamped-snapshots"
    prefix  = "${local.snapshot_prefix}/snapshots/"
    enabled = true

    abort_incomplete_multipart_upload_days = 1

    expiration {
      days = var.snapshot_retention_days
    }

    noncurrent_version_expiration {
      days = 2
    }
  }

  lifecycle_rule {
    id      = "expire-old-latest-manifests"
    prefix  = "${local.snapshot_prefix}/latest.json"
    enabled = true

    noncurrent_version_expiration {
      days = 7
    }
  }

  force_destroy = false
}

resource "digitalocean_cdn" "snapshots" {
  count = var.enable_spaces ? 1 : 0

  origin = digitalocean_spaces_bucket.snapshots[0].bucket_domain_name
  ttl    = 3600
}

resource "digitalocean_project_resources" "this" {
  project = digitalocean_project.this.id
  resources = concat(
    [digitalocean_droplet.node.urn],
    var.enable_spaces ? [digitalocean_spaces_bucket.snapshots[0].urn] : []
  )
}
