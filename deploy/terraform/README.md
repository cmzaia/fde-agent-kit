# Terraform skeleton (AWS ECS Fargate)

Provisions an ECR repo, log group, ECS cluster, task definition and service. It assumes the
customer's landing zone already provides the VPC, private subnets, ALB and target group, and that
secrets (`ANTHROPIC_API_KEY`, `REVIEW_DB_URL`) live in Secrets Manager.

Not applied or validated by CI. Run `terraform fmt -check && terraform init -backend=false && terraform validate`
before adapting it.
