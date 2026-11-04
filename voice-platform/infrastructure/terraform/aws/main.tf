# Production-shaped AWS footprint: VPC (private subnets), RDS PostgreSQL 16 (encrypted,
# Multi-AZ, PITR), ElastiCache Redis (TLS), S3 (SSE-KMS, versioning, lifecycle), Secrets
# Manager, ECS Fargate services for api/worker/jobs behind an ALB, CloudWatch logs.
# LiveKit (server, SIP, egress) is expected to be LiveKit Cloud or a separate deployment.

data "aws_availability_zones" "available" { state = "available" }
locals {
  name = "vp-${var.environment}"
  azs  = slice(data.aws_availability_zones.available.names, 0, 3)
}

module "vpc" {
  # pinned to an immutable commit (tag v5.13.0) rather than a floating registry version range
  source               = "git::https://github.com/terraform-aws-modules/terraform-aws-vpc.git?ref=e226cc15a7b8f62fd0e108792fea66fa85bcb4b9"
  name                 = local.name
  cidr                 = var.vpc_cidr
  azs                  = local.azs
  private_subnets      = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets       = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i + 8)]
  database_subnets     = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 6, i + 48)]
  enable_nat_gateway   = true
  single_nat_gateway   = var.environment != "prod"
  enable_dns_hostnames = true
}

resource "aws_kms_key" "data" {
  description         = "${local.name} data encryption"
  enable_key_rotation = true
}

# --- PostgreSQL -------------------------------------------------------------------------
resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL: reachable only from the application tasks"
  vpc_id      = module.vpc.vpc_id
  ingress {
    description     = "PostgreSQL from app tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

resource "aws_db_subnet_group" "db" {
  name       = local.name
  subnet_ids = module.vpc.database_subnets
}

resource "aws_db_parameter_group" "pg" {
  name   = "${local.name}-pg16"
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
}

resource "aws_db_instance" "pg" {
  identifier                   = local.name
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.db_instance_class
  allocated_storage            = var.db_allocated_storage
  max_allocated_storage        = var.db_allocated_storage * 4
  db_name                      = "voice_platform"
  username                     = "vp_admin"
  manage_master_user_password  = true
  storage_encrypted            = true
  kms_key_id                   = aws_kms_key.data.arn
  multi_az                     = var.environment == "prod"
  backup_retention_period      = var.environment == "prod" ? 30 : 7
  deletion_protection          = var.environment == "prod"
  copy_tags_to_snapshot        = true
  performance_insights_enabled = true
  #checkov:skip=CKV_AWS_157:Multi-AZ is on for prod (var.environment == "prod"); off in dev/staging for cost
  #checkov:skip=CKV_AWS_293:deletion protection is on for prod; dev/staging are disposable
  #checkov:skip=CKV_AWS_161:the app authenticates with a Secrets Manager password over TLS (rds.force_ssl)
  #checkov:skip=CKV_AWS_118:enhanced monitoring needs a dedicated IAM role; Performance Insights + CloudWatch logs are enabled
  performance_insights_kms_key_id = aws_kms_key.data.arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  auto_minor_version_upgrade      = true
  db_subnet_group_name            = aws_db_subnet_group.db.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  parameter_group_name            = aws_db_parameter_group.pg.name
  skip_final_snapshot             = var.environment != "prod"
  final_snapshot_identifier       = "${local.name}-final"
}

# --- Redis --------------------------------------------------------------------------------
resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis: reachable only from the application tasks"
  vpc_id      = module.vpc.vpc_id
  ingress {
    description     = "Redis from app tasks"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = local.name
  subnet_ids = module.vpc.private_subnets
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = local.name
  description                = "voice platform cache / rate limits"
  engine                     = "redis"
  node_type                  = var.redis_node_type
  num_cache_clusters         = var.environment == "prod" ? 2 : 1
  automatic_failover_enabled = var.environment == "prod"
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn
  transit_encryption_enabled = true
  #checkov:skip=CKV_AWS_31:TLS in transit is on; access is limited to the app security group (no AUTH token yet, see PRODUCTION_READINESS)
  #checkov:skip=CKV2_AWS_50:automatic failover with 2 nodes is on for prod only
  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]
}

# --- S3: documents + recordings -------------------------------------------------------------
resource "aws_s3_bucket" "data" {
  #checkov:skip=CKV_AWS_144:no cross-region replication on purpose — customer data stays in the chosen (Canadian) region
  #checkov:skip=CKV2_AWS_62:no event consumers; ingestion is driven by the API
  bucket = "${local.name}-data-${data.aws_caller_identity.me.account_id}"
}
data "aws_caller_identity" "me" {}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "recording-retention"
    status = "Enabled"
    filter { prefix = "tenants/" }
    # Per-tenant retention is enforced by the application; this is the global upper bound.
    expiration { days = var.recording_retention_days * 4 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  bucket = aws_s3_bucket.data.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# --- secrets (values set out-of-band; never in state files) --------------------------------
resource "aws_secretsmanager_secret" "app" {
  name       = "${local.name}/app"
  kms_key_id = aws_kms_key.data.arn
  # keys: VP_JWT_SECRET, VP_ENCRYPTION_KEYS, VP_DATABASE_URL, VP_MIGRATION_DATABASE_URL,
  #       VP_REDIS_URL, VP_LIVEKIT_API_KEY, VP_LIVEKIT_API_SECRET, provider API keys
}

# --- ECS ----------------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public HTTPS load balancer for the API"
  vpc_id      = module.vpc.vpc_id
  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  # egress only to the API tasks (separate rule to avoid an SG reference cycle)
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB to API tasks"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_security_group" "app" {
  #checkov:skip=CKV_AWS_382:tasks call provider APIs, LiveKit (WebRTC/UDP), carriers and tenant CRMs; app-level egress policy (EgressPolicy) restricts destinations
  name        = "${local.name}-app"
  description = "API, worker and job tasks"
  vpc_id      = module.vpc.vpc_id
  ingress {
    description     = "API from the load balancer"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    description = "Outbound to providers, LiveKit, carriers and integrations"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  #checkov:skip=CKV_AWS_338:30-day retention is a data-minimisation choice (logs are redacted but may reference callers)
  name              = "/ecs/${local.name}"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.data.arn
}

data "aws_iam_policy_document" "assume_ecs" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.app.arn] },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = [aws_kms_key.data.arn] },
    ]
  })
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs.json
}

resource "aws_iam_role_policy" "task_s3" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = ["${aws_s3_bucket.data.arn}/tenants/*"] },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = [aws_kms_key.data.arn] },
    ]
  })
}

locals {
  secret_keys = ["VP_JWT_SECRET", "VP_ENCRYPTION_KEYS", "VP_DATABASE_URL", "VP_MIGRATION_DATABASE_URL",
    "VP_REDIS_URL", "VP_LIVEKIT_API_KEY", "VP_LIVEKIT_API_SECRET", "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY",
  "ELEVEN_API_KEY", "CARTESIA_API_KEY", "ASSEMBLYAI_API_KEY"]
  common_env = [
    { name = "VP_ENVIRONMENT", value = var.environment },
    { name = "VP_BLOB_BACKEND", value = "s3" },
    { name = "VP_S3_BUCKET", value = aws_s3_bucket.data.bucket },
    { name = "VP_S3_REGION", value = var.region },
    { name = "VP_LIVEKIT_URL", value = var.livekit_url },
    { name = "LIVEKIT_URL", value = var.livekit_url },
  ]
  secrets = [for k in local.secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }]
  services = {
    api    = { command = ["vp-api"], cpu = 512, memory = 1024, count = var.api_desired_count, port = 8000 }
    worker = { command = ["vp-worker", "start"], cpu = var.worker_cpu, memory = var.worker_memory, count = var.worker_desired_count, port = null }
    jobs   = { command = ["vp", "jobs"], cpu = 1024, memory = 2048, count = 1, port = null }
  }
}

resource "aws_ecs_task_definition" "svc" {
  for_each                 = local.services
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name         = each.key
    image        = var.image
    command      = each.value.command
    essential    = true
    environment  = local.common_env
    secrets      = local.secrets
    stopTimeout  = each.key == "worker" ? 120 : 30
    portMappings = each.value.port == null ? [] : [{ containerPort = each.value.port }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

resource "aws_lb" "api" {
  name                       = "${local.name}-api"
  load_balancer_type         = "application"
  subnets                    = module.vpc.public_subnets
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "prod"
  #checkov:skip=CKV_AWS_150:deletion protection is on for prod
  #checkov:skip=CKV2_AWS_28:WAF (aws_wafv2_web_acl) recommended before launch; not provisioned here (cost/rule tuning)
  #checkov:skip=CKV_AWS_91:access logs need a dedicated log bucket; recommended before launch
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = module.vpc.vpc_id
  health_check { path = "/readyz" }
  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "svc" {
  for_each        = local.services
  name            = each.key
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.svc[each.key].arn
  desired_count   = each.value.count
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = module.vpc.private_subnets
    security_groups = [aws_security_group.app.id]
  }
  dynamic "load_balancer" {
    for_each = each.value.port == null ? [] : [1]
    content {
      target_group_arn = aws_lb_target_group.api.arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = 20
  min_capacity       = var.worker_desired_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.svc["worker"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_cpu" {
  name               = "${local.name}-worker-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  target_tracking_scaling_policy_configuration {
    target_value = 55 # LiveKit also sheds load per worker (load_threshold)
    predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
    scale_in_cooldown = 600 # draining workers keep calls until they end
  }
}
