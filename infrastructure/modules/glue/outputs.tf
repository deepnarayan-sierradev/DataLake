output "curated_database_name" {
  description = "Name of the Glue Data Catalog database for the curated layer."
  value       = aws_glue_catalog_database.curated.name
}

output "analytics_database_name" {
  description = "Name of the Glue Data Catalog database for the analytics layer."
  value       = aws_glue_catalog_database.analytics.name
}

output "athena_workgroup_name" {
  description = "Name of the Athena workgroup provisioned for analytics queries."
  value       = aws_athena_workgroup.analytics.name
}
