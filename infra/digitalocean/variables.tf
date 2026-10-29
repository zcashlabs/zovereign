variable "digitalocean_token" {
  description = "DigitalOcean API token. Set with TF_VAR_digitalocean_token."
  type        = string
  sensitive   = true
}

variable "spaces_access_id" {
  description = "Spaces access key ID. Required when the Space is enabled."
  type        = string
  sensitive   = true
  default     = ""
}

variable "spaces_secret_key" {
  description = "Spaces secret key. Required when the Space is enabled."
  type        = string
  sensitive   = true
  default     = ""
}

variable "enable_spaces" {
  description = "Create the Space and CDN after Spaces credentials are available."
  type        = bool
  default     = false
}

variable "region" {
  description = "DigitalOcean region for compute, block storage, and Spaces."
  type        = string
  default     = "blr1"
}

variable "droplet_size" {
  description = "Droplet size; g-4vcpu-16gb matches the current host with dedicated CPU and 16 GiB memory."
  type        = string
  default     = "g-4vcpu-16gb"
}

variable "volume_size_gib" {
  description = "Persistent capacity for Zakura and lightwalletd data."
  type        = number
  default     = 500

  validation {
    condition     = var.volume_size_gib >= 400
    error_message = "The volume must be at least 400 GiB for the current node data and snapshot workspace."
  }
}

variable "ssh_public_key" {
  description = "SSH public key installed on the Droplet. Set with TF_VAR_ssh_public_key."
  type        = string

  validation {
    condition     = can(regex("^ssh-(ed25519|rsa) ", trimspace(var.ssh_public_key)))
    error_message = "ssh_public_key must contain an OpenSSH ed25519 or RSA public key."
  }
}

variable "admin_cidrs" {
  description = "IPv4 or IPv6 CIDRs allowed to SSH to the Droplet."
  type        = list(string)

  validation {
    condition     = length(var.admin_cidrs) > 0 && !contains(var.admin_cidrs, "0.0.0.0/0") && !contains(var.admin_cidrs, "::/0")
    error_message = "Provide at least one narrow admin CIDR; global SSH access is forbidden."
  }
}

variable "space_name" {
  description = "Globally unique Spaces bucket name."
  type        = string
  default     = "zcashlabs-lightwalletd-snapshots"
}

variable "snapshot_retention_days" {
  description = "Days to retain immutable timestamped snapshot objects."
  type        = number
  default     = 14
}
