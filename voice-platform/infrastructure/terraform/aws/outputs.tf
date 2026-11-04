output "api_url" { value = "https://${aws_lb.api.dns_name}" }
output "database_endpoint" { value = aws_db_instance.pg.address }
output "redis_endpoint" { value = aws_elasticache_replication_group.redis.primary_endpoint_address }
output "data_bucket" { value = aws_s3_bucket.data.bucket }
output "app_secret_arn" { value = aws_secretsmanager_secret.app.arn }
