"""
The tenant-boundary interlock (G9).

The 2026-07-29 re-assessment found a security control whose declared status and actual status were
independent variables: `tenant_boundary_mode` could say `enforce` while the tag the policy
conditions
on was never set anywhere. Enforcing in that state is not partial protection — it is asymmetric
breakage. The S3 statements are guarded by `Null aws:PrincipalTag/tenant_code = false`, so for an
untagged principal they never apply and S3 stays open; the DynamoDB and Secrets Manager statements
compare against an unresolvable variable and an absent resource tag, so they deny everything.

So the status is now derived rather than declared, and this asserts that derivation holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_tenant_session_adoption import adoption_flag_states, scan

REPO_ROOT = Path(__file__).resolve().parent.parent
BOUNDARY_TF = REPO_ROOT / "infrastructure" / "modules" / "iam" / "tenant_boundary.tf"


class TestTheInterlockCannotBeBypassed:
    def test_no_environment_claims_adoption_while_untagged_clients_remain(self) -> None:
        untagged = scan()
        claiming = [env for env, state in adoption_flag_states().items() if state == "true"]
        assert not (untagged and claiming), (
            f"{len(untagged)} untagged client(s) remain but {claiming} declare the tagged-session "
            "path adopted. Enforcing then leaves S3 open and denies DynamoDB outright."
        )

    def test_enforce_requires_the_adoption_flag_in_terraform(self) -> None:
        # The interlock itself: mode alone must not be sufficient.
        text = BOUNDARY_TF.read_text(encoding="utf-8")
        assert 'var.tenant_boundary_mode == "enforce" && var.tenant_session_tagging_adopted' in text

    def test_the_boundary_refuses_empty_resource_lists(self) -> None:
        # Every environment left these unset, producing statements with no Resource — which IAM
        # rejects, so the policy could never have applied while `validate` stayed green.
        text = BOUNDARY_TF.read_text(encoding="utf-8")
        assert "length(var.data_bucket_arns) > 0" in text
        assert "length(var.tenant_scoped_table_arns) > 0" in text

    def test_enforce_requires_a_cloudtrail_log_group(self) -> None:
        # Otherwise the observation window that gates the flip measures nothing.
        text = BOUNDARY_TF.read_text(encoding="utf-8")
        assert 'var.cloudtrail_log_group_name != ""' in text

    def test_the_boundary_attaches_to_the_tagged_data_roles(self) -> None:
        # Attaching to the shared stage roles is what made the policy unsatisfiable.
        text = BOUNDARY_TF.read_text(encoding="utf-8")
        assert 'resource "aws_iam_role" "tenant_data"' in text
        assert "sts:TagSession" in text

    def test_the_trust_policy_requires_the_tag_to_be_present(self) -> None:
        # A role assumable without the tag is the untagged state under another name.
        text = BOUNDARY_TF.read_text(encoding="utf-8")
        assert "aws:RequestTag/tenant_code" in text


class TestTheGateReportsHonestly:
    def test_the_scan_finds_the_known_remaining_work(self) -> None:
        # A gate reporting zero while 47 sites remain would be the original defect again.
        assert len(scan()) > 0, "the scanner found nothing — verify it still detects boto3.client"

    def test_every_environment_declares_the_flag(self) -> None:
        states = adoption_flag_states()
        assert set(states) == {"dev", "staging", "prod"}
        assert "unset" not in states.values(), "an unset flag is not a decision"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
