"""The Run recipe: `.weave/compose.yaml`, a compose file with the workflow's own fields in `x-weave:` (#49).

Pure logic, no Docker: the recipe is parsed and checked, and the compose command line is built,
without a daemon. Each test is named after an acceptance criterion of #49.
"""

from __future__ import annotations

import pytest
import yaml

from workflow_weave.agent_worker.environment import (
    RECIPE_PATH,
    RecipeError,
    compose_args,
    compose_override,
    parse_recipe,
)

SERVICE_RECIPE = """\
services:
  web:
    image: python:3.12-slim
    command: python -m http.server 8000
  db:
    image: postgres:16-alpine
x-weave:
  readiness:
    web:
      command: python -c "import urllib.request as u; u.urlopen('http://localhost:8000')"
      timeout: 30
    db:
      command: [pg_isready, -U, postgres]
"""


def test_the_recipe_is_an_ordinary_compose_file_with_the_workflow_fields_in_one_x_weave_block():
    # What standard tooling sees: top-level keys compose knows, plus one x- extension it ignores.
    doc = yaml.safe_load(SERVICE_RECIPE)
    assert set(doc) == {"services", "x-weave"}
    assert [k for k in doc if k.startswith("x-")] == ["x-weave"]
    recipe = parse_recipe("svc", SERVICE_RECIPE)
    assert recipe.services == ("web", "db")
    assert recipe.readiness["web"].timeout == 30
    assert recipe.readiness["web"].command == ("sh", "-c", doc["x-weave"]["readiness"]["web"]["command"])
    assert recipe.readiness["db"].command == ("pg_isready", "-U", "postgres")  # a list runs as is
    assert recipe.readiness["db"].timeout == 60  # default


def test_the_recipe_lives_apart_from_the_developers_compose_file():
    assert RECIPE_PATH == ".weave/compose.yaml"


def test_a_recipe_may_have_no_services_of_its_own():
    assert parse_recipe("client", "x-weave: {}\n").services == ()
    assert parse_recipe("client", "services: {}\nx-weave:\n  readiness: {}\n").services == ()


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("services: [", "not valid YAML"),
        ("- just\n- a list\n", "must be a mapping"),
        ("services:\n  web: {image: x}\n", "x-weave"),
        ("services:\n  web: {image: x}\nx-weave: {}\n", "readiness check for service 'web'"),
        ("services:\n  web: {image: x}\nx-weave:\n  readiness: {api: {command: 'true'}}\n", "unknown service 'api'"),
        ("services:\n  web: {image: x}\nx-weave:\n  readiness: {web: {}}\n", "command"),
        ("services:\n  web: {image: x}\nx-weave:\n  readiness: {web: {command: 'true', timeout: 0}}\n", "timeout"),
        ("services:\n  web: {image: x}\nx-weave:\n  readiness: {web: {command: [1, 2]}}\n", "command"),
    ],
)
def test_a_recipe_that_cannot_be_used_is_refused_naming_the_repo_and_what_to_fix(text, fragment):
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", text)
    assert "svc" in str(e.value) and RECIPE_PATH in str(e.value) and fragment in str(e.value)


def test_a_developers_own_compose_file_is_never_used():
    args = compose_args("svc", "/workspace/svc/.weave/compose.yaml", "/weave/env/svc.override.yaml")
    files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
    # Explicit -f, never a default lookup, and nothing that could add a developer file.
    assert files == ["/workspace/svc/.weave/compose.yaml", "/weave/env/svc.override.yaml"]
    assert args[:4] == ["docker", "compose", "-p", "svc"]
    assert "--project-directory" not in args and "docker-compose.yml" not in " ".join(args)


def test_each_service_is_reachable_by_its_service_name_and_repo_name_on_the_environment_network():
    # The address `<service>.<repo>` is given by `docker network connect --alias` (#50, tested in
    # test_environment_several_repos.py), because declaring the network to Compose would also give
    # every service its bare name there. So the override leaves networks alone: the recipe's own
    # network keeps working as with standard tooling.
    override = yaml.safe_load(compose_override("svc", parse_recipe("svc", SERVICE_RECIPE)))
    assert "networks" not in override
    for service in ("web", "db"):
        assert "networks" not in override["services"][service]
        assert override["services"][service]["labels"] == {"weave.repo": "svc", "weave.service": service}
