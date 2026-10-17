import pytest
from agent.executor import execute

def test_get_device_status_execution():
    """
    Test that the get_status action explicitly mapped from device.status 
    executes successfully through the executor and returns battery data.
    """
    # Simulate what the Hub sends after routing
    # capability="device.status" -> intent="get_device_status" -> action="get_status"
    command = "get_status"
    args = []
    
    result = execute(command, args)
    
    assert result["status"] == "success"
    # The message should contain battery string
    assert "Battery is at" in result["message"] or "Battery status is unavailable" in result["message"]
