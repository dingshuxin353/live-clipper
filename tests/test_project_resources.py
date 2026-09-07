import pytest

from live_clipper.config import Settings
from live_clipper.project_domain import default_project_config
from live_clipper.project_resources import ResourceUnavailableError, resolve_parameter_snapshot, resource_options
from live_clipper.project_storage import ProjectRepository


def test_global_credentials_do_not_create_resources_or_authorize_execution(tmp_path):
    settings = Settings(cheap_model_api_key='unallocated-key', asr_api_key='unallocated-asr')
    with ProjectRepository(tmp_path / 'service') as repository:
        assert resource_options(repository) == ()
        config = default_project_config(tmp_path / 'source', tmp_path / 'output')
        with pytest.raises(ResourceUnavailableError):
            resolve_parameter_snapshot(config, settings, repository=repository)
