variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  type    = string
  default = "fde-agent"
}

variable "image" {
  type        = string
  description = "Full image URI, pinned by digest or immutable tag."
}

variable "llm_provider" {
  type    = string
  default = "anthropic"
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "desired_count" {
  type    = number
  default = 2
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "alb_security_group_id" {
  type = string
}

variable "target_group_arn" {
  type = string
}

variable "anthropic_api_key_secret_arn" {
  type = string
}

variable "review_db_url_secret_arn" {
  type = string
}
