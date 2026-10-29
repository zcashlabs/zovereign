output "droplet_id" {
  value = digitalocean_droplet.node.id
}

output "droplet_ipv4" {
  value = digitalocean_droplet.node.ipv4_address
}

output "volume_id" {
  value = digitalocean_volume.data.id
}

output "space_origin" {
  value = var.enable_spaces ? digitalocean_spaces_bucket.snapshots[0].bucket_domain_name : null
}

output "cdn_endpoint" {
  value = var.enable_spaces ? digitalocean_cdn.snapshots[0].endpoint : null
}

output "latest_manifest_url" {
  value = var.enable_spaces ? "https://${digitalocean_cdn.snapshots[0].endpoint}/${local.snapshot_prefix}/latest.json" : null
}
