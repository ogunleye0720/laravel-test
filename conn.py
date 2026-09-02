database_uri = var.enabled ? format(
  "postgresql://%s:%s@%s:%s/%s",
  urlencode(local.master_username),
  urlencode(local.master_password),
  aws_rds_cluster.main[0].endpoint,
  aws_rds_cluster.main[0].port,
  urlencode(local.database_name),
) : ""
