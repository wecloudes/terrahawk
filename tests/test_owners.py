"""CODEOWNERS → unit owners."""

import pytest

from terrahawk import owners


CO = """\
# default
*                       @org/platform
/live/prod/             @org/prod-team
live/*/database/        @org/dba
apps/                   @org/apps
/live/shared/*          @org/shared
/live/sandbox/          # explicitly unowned
"""


@pytest.fixture
def rules():
    return owners.parse_codeowners(CO)


class TestMatching:
    @pytest.mark.parametrize("path,expected", [
        ("live/dev/app/terragrunt.hcl", ["@org/platform"]),
        ("live/prod/app/terragrunt.hcl", ["@org/prod-team"]),
        ("live/prod/database/terragrunt.hcl", ["@org/dba"]),      # later rule wins
        ("x/apps/web/terragrunt.hcl", ["@org/apps"]),            # unanchored dir, any depth
        ("live/shared/terragrunt.hcl", ["@org/shared"]),         # dir/* direct child
        ("live/shared/vpc/terragrunt.hcl", ["@org/platform"]),   # dir/* not nested (GitHub)
        ("live/sandbox/x/terragrunt.hcl", []),                   # no owners = unowned
    ])
    def test_last_match_wins(self, rules, path, expected):
        assert owners.owners_for(rules, path) == expected

    def test_double_star_and_emails(self):
        rules = owners.parse_codeowners("**/network/** dev@example.com @net\n")
        assert owners.owners_for(rules, "a/b/network/vpc/terragrunt.hcl") == ["dev@example.com", "@net"]

    def test_gitlab_sections_default_owners(self):
        rules = owners.parse_codeowners("[Infra] @org/infra\nlive/\n^[Docs][2] @docs\ndocs/ @writer\n")
        assert owners.owners_for(rules, "live/a/terragrunt.hcl") == ["@org/infra"]
        assert owners.owners_for(rules, "docs/x.md") == ["@writer"]


class TestAttach:
    def test_locations_and_repo_relative_paths(self, tmp_path):
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "CODEOWNERS").write_text("/terragrunt/prod/ @prod\n")
        cfg = tmp_path / "terragrunt"
        results = [{"unit": "prod/app"}, {"unit": "dev/app"}]
        path = owners.attach_owners(results, tmp_path, cfg)
        assert path.endswith(".github/CODEOWNERS")
        assert results[0]["owners"] == ["@prod"] and results[1]["owners"] == []

    def test_no_codeowners(self, tmp_path):
        results = [{"unit": "a"}]
        assert owners.attach_owners(results, tmp_path, tmp_path) is None
        assert "owners" not in results[0]

    def test_override(self, tmp_path):
        (tmp_path / "OWNERS.txt").write_text("* @all\n")
        results = [{"unit": "a"}]
        assert owners.attach_owners(results, tmp_path, tmp_path, "OWNERS.txt")
        assert results[0]["owners"] == ["@all"]
        assert owners.find_codeowners(tmp_path, "missing") is None
