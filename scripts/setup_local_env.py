#!/usr/bin/env python3
"""
Helper script to extract Terraform outputs and set up local environment variables.

This script:
1. Reads Terraform outputs from infrastructure/environments/dev/
2. Creates a .env.local file with all required AWS resource names
3. Validates that all prerequisites are met for local testing

Usage:
    python scripts/setup_local_env.py

This creates .env.local which can be sourced in your shell:
    source .env.local
"""

import json
import os
import subprocess
import sys
from pathlib import Path



def run_command(cmd: list[str]) -> str:
    """Run a shell command and return stdout."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except FileNotFoundError as e:
        print(f"Error: Command not found: {cmd[0]}", file=sys.stderr)
        raise RuntimeError(f"Command not found: {cmd[0]}")
    except subprocess.CalledProcessError as e:
        print(f"Error running {' '.join(cmd)}: {e.stderr}", file=sys.stderr)
        raise

def get_terraform_outputs(environment: str = "dev") -> dict[str, str]:
    """Get Terraform outputs for the specified environment."""
    tf_dir = Path(__file__).parent.parent / "infrastructure" / "environments" / environment
    
    if not tf_dir.exists():
        raise FileNotFoundError(f"Terraform directory not found: {tf_dir}")
    
    # Change to terraform directory and get outputs as JSON
    original_cwd = os.getcwd()
    try:
        os.chdir(tf_dir)
        output_json = run_command(["terraform", "output", "-json"])
        os.chdir(original_cwd)
    except Exception as e:
        os.chdir(original_cwd)
        raise RuntimeError(f"Failed to get Terraform outputs: {e}")
    

    # Parse JSON and flatten to simple key-value pairs
    outputs = json.loads(output_json)
    flattened = {}
    for key, value in outputs.items():
        if isinstance(value, dict) and "value" in value:
            val = value["value"]
            # Convert lists to comma-separated strings (env vars can't be JSON arrays)
            if isinstance(val, list):
                flattened[key] = ",".join(str(v) for v in val)
            else:
                flattened[key] = val
        else:
            flattened[key] = value
    
    return flattened

def validate_aws_profile(profile: str) -> bool:
    """Verify AWS profile exists and is valid."""
    try:
        run_command(["aws", "sts", "get-caller-identity", "--profile", profile])
        return True
    except subprocess.CalledProcessError:
        return False


def validate_secrets(profile: str, region: str) -> dict[str, bool]:
    """Check if required secrets exist in Secrets Manager."""
    secrets = {
        "salesforce": f"{profile.split('-')[0]}/sources/salesforce/credentials",
        "netsuite": f"{profile.split('-')[0]}/sources/netsuite/credentials",
        "mysql-rds": f"{profile.split('-')[0]}/sources/mysql-rds/credentials",
    }
    
    results = {}
    for name, secret_id in secrets.items():
        try:
            run_command([
                "aws", "secretsmanager", "describe-secret",
                "--secret-id", secret_id,
                "--profile", profile,
                "--region", region
            ])
            results[name] = True
        except subprocess.CalledProcessError:
            results[name] = False
    
    return results


def create_env_file(
    outputs: dict[str, str],
    profile: str,
    region: str,
    env_file: Path
) -> None:
    """Create .env.local file with environment variables."""
    
    
    # Validate critical outputs exist
    critical_outputs = ["raw_layer_bucket_id", "watermark_repository_table_name", "state_machine_arn"]
    missing = [key for key in critical_outputs if not outputs.get(key)]
    if missing:
        print(f"⚠️  WARNING: Missing critical Terraform outputs: {missing}")
        print("   Terraform may not be initialized or applied. Check:")
        print("   cd infrastructure/environments/dev && terraform validate && terraform plan")
    
    # Map Terraform outputs to environment variable names
    env_vars = {
        "AWS_PROFILE": profile,
        "AWS_REGION": region,
        "PYTHONPATH": "${PYTHONPATH:-.}",
        "RAW_S3_BUCKET": outputs.get("raw_layer_bucket_id", ""),
        "CURATED_S3_BUCKET": outputs.get("curated_layer_bucket_id", ""),
        "ANALYTICS_S3_BUCKET": outputs.get("analytics_layer_bucket_id", ""),
        "SCHEMA_SNAPSHOT_S3_BUCKET": outputs.get("schema_snapshots_bucket_id", ""),
        "WATERMARK_TABLE": outputs.get("watermark_repository_table_name", ""),
        "AUDIT_LOG_TABLE": outputs.get("run_audit_log_table_name", ""),
        "DLQ_URL": outputs.get("extraction_failure_dlq_url", ""),
        "EXTRACTION_RUNTIME_ROLE_ARN": outputs.get("extraction_runtime_role_arn", ""),
        "STATE_MACHINE_ARN": outputs.get("state_machine_arn", ""),
    }
    
    with open(env_file, "w") as f:
        f.write("#!/bin/bash\n")
        f.write("# Auto-generated environment variables from Terraform outputs\n")
        f.write("# Source this file: source .env.local\n\n")
        
        for key, value in env_vars.items():
            if key == "PYTHONPATH":
                # PYTHONPATH uses shell parameter expansion
                f.write(f"export {key}='${{PYTHONPATH:-.}}'\n")
            elif value:
                # Escape single quotes in values
                escaped_value = str(value).replace("'", "'\\''")
                f.write(f"export {key}='{escaped_value}'\n")
    env_file.chmod(0o600)
    print(f"✅ Created {env_file}")


def print_checklist(outputs: dict[str, str], secrets: dict[str, bool]) -> None:
    """Print a checklist of prerequisites."""
    print("\n" + "="*80)
    print("LOCAL TESTING PREREQUISITES CHECKLIST")
    print("="*80 + "\n")
    
    print("AWS Resources from Terraform:")
    print(f"  ✅ Raw S3 Bucket: {outputs.get('raw_layer_bucket_id', 'NOT FOUND')}")
    print(f"  ✅ Watermark Table: {outputs.get('watermark_repository_table_name', 'NOT FOUND')}")
    print(f"  ✅ Audit Log Table: {outputs.get('run_audit_log_table_name', 'NOT FOUND')}")
    print(f"  ✅ State Machine ARN: {outputs.get('state_machine_arn', 'NOT FOUND')}")
    
    print("\nSecrets Manager Status:")
    for name, exists in secrets.items():
        status = "✅ EXISTS" if exists else "❌ MISSING"
        print(f"  {status}: {name}")
    
    print("\nNext Steps:")
    print("  1. Source the environment: source .env.local")
    print("  2. Verify AWS: aws sts get-caller-identity")
    print("  3. Run tests: pytest --cov --cov-fail-under=80")
    print("\n")


def main() -> None:
    """Main entry point."""
    environment = "dev"
    profile = "dev"
    region = "us-east-1"
    env_file = Path(__file__).parent.parent / ".env.local"
    
    print(f"Setting up local environment for {environment} account...")
    print(f"AWS Profile: {profile}")
    print(f"AWS Region: {region}\n")
    
    # Validate AWS profile
    print("Validating AWS profile...")
    if not validate_aws_profile(profile):
        print(f"❌ AWS profile '{profile}' not found or invalid")
        print("   Run: aws configure --profile dev")
        sys.exit(1)
    print(f"✅ AWS profile '{profile}' is valid\n")
    
    
    # Get Terraform outputs
    print("Reading Terraform outputs...")
    try:
        outputs = get_terraform_outputs(environment)
        if not outputs:
            print("⚠️  No Terraform outputs found (empty state or not applied?)")
        else:
            print(f"✅ Retrieved {len(outputs)} Terraform outputs\n")
    except FileNotFoundError as e:
        print(f"❌ Terraform directory not found: {e}")
        print(f"   Expected: infrastructure/environments/{environment}/")
        sys.exit(1)
    except RuntimeError as e:
        print(f"❌ Failed to read Terraform outputs: {e}")
        print("   Make sure:")
        print("   1. Terraform is installed: brew install terraform")
        print("   2. Terraform is initialized: cd infrastructure/environments/dev && terraform init")
        print("   3. Terraform is applied: cd infrastructure/environments/dev && terraform apply")
        sys.exit(1)
    # Check secrets
    print("Checking Secrets Manager...")
    secrets = validate_secrets(profile, region)
    missing = [name for name, exists in secrets.items() if not exists]
    if missing:
        print(f"⚠️  Missing secrets: {', '.join(missing)}")
        print("   Create them in AWS Secrets Manager before running extraction tests")
    else:
        print("✅ All required secrets exist\n")
    
    # Create .env.local
    create_env_file(outputs, profile, region, env_file)
    
    # Print summary
    print_checklist(outputs, secrets)


if __name__ == "__main__":
    main()
