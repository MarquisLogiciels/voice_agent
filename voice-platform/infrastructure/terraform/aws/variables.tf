variable "region" {
  type    = string
  default = "ca-central-1" # Canadian data residency (PIPEDA / Law 25 considerations)
}
variable "environment" { type = string }
variable "image" { type = string } # ECR image URI for the platform
variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}
variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}
variable "db_allocated_storage" {
  type    = number
  default = 50
}
variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}
variable "api_desired_count" {
  type    = number
  default = 2
}
variable "worker_desired_count" {
  type    = number
  default = 2
}
variable "worker_cpu" {
  type    = number
  default = 2048
}
variable "worker_memory" {
  type    = number
  default = 4096
}
variable "certificate_arn" { type = string } # ACM certificate for the API ALB
variable "livekit_url" { type = string }     # wss://<project>.livekit.cloud
variable "recording_retention_days" {
  type    = number
  default = 90
}
