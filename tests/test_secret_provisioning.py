"""
tests/test_secret_provisioning.py
---------------------------------
Regression tests for device secret provisioning and collision protection:
1. Existing enrolled credential in ~/.cortex/ is strictly protected and never overwritten.
2. Deployment credentials bound to device_id 'lenovo' are ignored when starting as 'asus'.
3. A deployment credential is only consumed by the exact device it was intended for.
4. One-time deployment credentials securely provision and unlink the temporary file.
"""

import json
import tempfile
import shutil
from pathlib import Path
import pytest
from unittest.mock import patch

from agent.agent import _load_secret


def test_existing_credential_never_overwritten(tmp_path):
    """
    Verify that if a device already has an enrolled secret in ~/.cortex/,
    it continues using it and NEVER consumes or overwrites with a deployment credential.
    """
    local_state_dir = tmp_path / ".cortex"
    local_state_dir.mkdir()
    existing_secret_file = local_state_dir / "device_secret"
    existing_secret_file.write_text("secret_A_asus_existing", encoding="utf-8")

    # Create a deployment credential in ROOT intended for lenovo
    mock_root = tmp_path / "workspace"
    mock_root.mkdir()
    enrollment_file = mock_root / ".cortex_enrollment.json"
    enrollment_file.write_text(
        json.dumps({
            "device_id": "lenovo",
            "enrollment_secret": "secret_B_lenovo_deployment",
            "one_time": True,
        }),
        encoding="utf-8"
    )

    with patch("agent.agent.LOCAL_STATE_DIR", local_state_dir), \
         patch("agent.agent.SECRET_FILE", existing_secret_file), \
         patch("agent.agent.ROOT", mock_root):

        # ASUS starts up
        loaded = _load_secret(cli_secret=None, device_id="asus")

        # Must return existing secret A
        assert loaded == "secret_A_asus_existing"
        # Must not overwrite local storage
        assert existing_secret_file.read_text(encoding="utf-8") == "secret_A_asus_existing"
        # The lenovo deployment file must remain untouched for lenovo
        assert enrollment_file.exists()


def test_deployment_device_id_mismatch_rejected(tmp_path):
    """
    Verify that on a fresh node with NO existing credential, a deployment credential
    intended for device 'lenovo' is REJECTED when starting as 'asus'.
    """
    local_state_dir = tmp_path / ".cortex"
    local_state_dir.mkdir()
    secret_file = local_state_dir / "device_secret"

    mock_root = tmp_path / "workspace"
    mock_root.mkdir()
    enrollment_file = mock_root / ".cortex_enrollment.json"
    enrollment_file.write_text(
        json.dumps({
            "device_id": "lenovo",
            "enrollment_secret": "secret_B_lenovo",
            "one_time": True,
        }),
        encoding="utf-8"
    )

    with patch("agent.agent.LOCAL_STATE_DIR", local_state_dir), \
         patch("agent.agent.SECRET_FILE", secret_file), \
         patch("agent.agent.ROOT", mock_root):

        # Starting as asus with lenovo deployment credential must reject and exit
        with pytest.raises(SystemExit):
            _load_secret(cli_secret=None, device_id="asus")

        # Secret file must NOT be created
        assert not secret_file.exists()
        # Deployment file must NOT be unlinked
        assert enrollment_file.exists()


def test_matching_deployment_credential_provisioned(tmp_path):
    """
    Verify that when starting with a matching device_id, the deployment credential
    is provisioned to local state and the one-time file is unlinked.
    """
    local_state_dir = tmp_path / ".cortex"
    local_state_dir.mkdir()
    secret_file = local_state_dir / "device_secret"

    mock_root = tmp_path / "workspace"
    mock_root.mkdir()
    enrollment_file = mock_root / ".cortex_enrollment.json"
    enrollment_file.write_text(
        json.dumps({
            "device_id": "lenovo",
            "enrollment_secret": "secret_B_lenovo",
            "one_time": True,
        }),
        encoding="utf-8"
    )

    with patch("agent.agent.LOCAL_STATE_DIR", local_state_dir), \
         patch("agent.agent.SECRET_FILE", secret_file), \
         patch("agent.agent.ROOT", mock_root):

        # Starting as lenovo with lenovo deployment credential
        loaded = _load_secret(cli_secret=None, device_id="lenovo")

        # Must successfully return secret B
        assert loaded == "secret_B_lenovo"
        # Must write to local storage
        assert secret_file.exists()
        assert secret_file.read_text(encoding="utf-8") == "secret_B_lenovo"
        # One-time deployment file must be removed
        assert not enrollment_file.exists()

        # Subsequent startup must load from local storage
        loaded_again = _load_secret(cli_secret=None, device_id="lenovo")
        assert loaded_again == "secret_B_lenovo"
