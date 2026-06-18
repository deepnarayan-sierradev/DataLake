"""
Observability package for the Enterprise Data Lake platform.

Provides:
  - configure_platform_logging(): one-time logging setup at application entry point
  - get_platform_logger(): returns a named structlog bound logger
  - CloudWatchMetricsEmitter: emits platform metrics to CloudWatch
"""
