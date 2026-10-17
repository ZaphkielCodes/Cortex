import pytest
import asyncio
from unittest.mock import patch, MagicMock
from agent.voice.polly_providers import AmazonPollyTTSProvider

class MockFallbackTTS:
    def __init__(self):
        self.called = False
    
    async def speak(self, text):
        self.called = True
        
    def stop_speaking(self):
        pass

@pytest.mark.asyncio
@patch('boto3.client')
async def test_polly_tts_success(mock_boto3):
    mock_client = MagicMock()
    mock_boto3.return_value = mock_client
    
    # Mock synthesize_speech
    mock_response = {'AudioStream': MagicMock()}
    mock_response['AudioStream'].read.return_value = b'\x00\x00' * 16000 # 1 second of silence
    mock_client.synthesize_speech.return_value = mock_response
    
    fallback = MockFallbackTTS()
    
    with patch('sounddevice.play') as mock_sd_play:
        polly = AmazonPollyTTSProvider(fallback_provider=fallback)
        await polly.speak("Hello")
        
        mock_client.synthesize_speech.assert_called_once()
        mock_sd_play.assert_called_once()
        assert not fallback.called

@pytest.mark.asyncio
@patch('boto3.client')
async def test_polly_tts_fallback_on_api_error(mock_boto3):
    mock_client = MagicMock()
    mock_boto3.return_value = mock_client
    
    from botocore.exceptions import BotoCoreError
    mock_client.synthesize_speech.side_effect = BotoCoreError()
    
    fallback = MockFallbackTTS()
    polly = AmazonPollyTTSProvider(fallback_provider=fallback)
    
    await polly.speak("Hello")
    assert fallback.called

def test_polly_config_override(monkeypatch):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-2')
    monkeypatch.setenv('CORTEX_POLLY_REGION', 'eu-west-1')
    monkeypatch.setenv('CORTEX_POLLY_VOICE', 'Joanna')
    monkeypatch.setenv('CORTEX_POLLY_ENGINE', 'standard')
    
    with patch('boto3.client') as mock_boto3:
        polly = AmazonPollyTTSProvider()
        mock_boto3.assert_called_with('polly', region_name='eu-west-1')
        assert polly.region == 'eu-west-1'
        assert polly.voice == 'Joanna'
        assert polly.engine == 'standard'
        assert polly.client is not None

def test_polly_validation_rejects_unsupported_neural_region(monkeypatch):
    monkeypatch.setenv('CORTEX_POLLY_REGION', 'us-east-2')
    monkeypatch.setenv('CORTEX_POLLY_ENGINE', 'neural')
    
    with patch('boto3.client') as mock_boto3:
        polly = AmazonPollyTTSProvider()
        # Should be rejected before synthesis, client should be None
        assert polly.client is None
        mock_boto3.assert_not_called()

def test_polly_startup_logging(monkeypatch, caplog):
    monkeypatch.setenv('CORTEX_POLLY_REGION', 'us-east-1')
    monkeypatch.setenv('CORTEX_POLLY_VOICE', 'Matthew')
    monkeypatch.setenv('CORTEX_POLLY_ENGINE', 'neural')
    
    import logging
    with patch('boto3.client'):
        with caplog.at_level(logging.INFO):
            polly = AmazonPollyTTSProvider()
            assert "[Polly Config] Resolved region: us-east-1" in caplog.text
            assert "[Polly Config] Resolved voice: Matthew" in caplog.text
            assert "[Polly Config] Resolved engine: neural" in caplog.text
            # Ensure no AWS secrets leaked
            assert "AKIA" not in caplog.text
