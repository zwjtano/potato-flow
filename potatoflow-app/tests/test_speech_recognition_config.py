import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from modules.speech_recognition import SpeechRecognizer, create_speech_recognizer_from_config


class SpeechRecognitionConfigTests(unittest.TestCase):
    def test_whisper_config_maps_timestamp_granularities(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'whisper',
            'WHISPER_TIMESTAMP_GRANULARITIES': 'word',
        }, task_id='unit-test-whisper')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.provider, 'whisper')
        self.assertEqual(recognizer.config.api_provider, 'whisper')
        self.assertEqual(recognizer.config.whisper_timestamp_granularities, 'word')

    def test_voxtral_config_keeps_voxtral_timestamp_granularities(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'voxtral',
            'VOXTRAL_TIMESTAMP_GRANULARITIES': 'segment,word',
            'VOXTRAL_BASE_URL': 'https://api.mistral.ai/v1',
        }, task_id='unit-test-voxtral')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.provider, 'voxtral')
        self.assertEqual(recognizer.config.api_provider, 'voxtral')
        self.assertEqual(recognizer.config.voxtral_timestamp_granularities, 'segment,word')

    def test_successful_asr_writes_srt_and_private_transcript_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / 'recording.mp4'
            audio = root / 'audio.wav'
            output = root / 'recording.srt'
            video.write_bytes(b'video')
            audio.write_bytes(b'audio')

            recognizer = SpeechRecognizer.__new__(SpeechRecognizer)
            recognizer.config = SimpleNamespace(
                vad_enabled=False,
                api_provider='voxtral',
                model_name='voxtral-mini',
            )
            recognizer._asr = SimpleNamespace(client=object(), _language_hint='zh')
            recognizer._extract_audio_wav = Mock(return_value=str(audio))
            recognizer._probe_media_duration = Mock(return_value=10.0)
            recognizer._fallback_transcription = Mock(return_value=[object()])
            cues = [{'start': 1.0, 'end': 2.0, 'text': '识别字幕'}]
            recognizer._srt = SimpleNamespace(
                clean_hallucinations=Mock(return_value=cues),
                resolve_overlaps=Mock(return_value=cues),
                apply_text_processing=Mock(return_value=cues),
                finalize_cues=Mock(return_value=cues),
                render_srt=Mock(return_value=(
                    '1\n00:00:01,000 --> 00:00:02,000\n识别字幕\n'
                )),
            )
            recognizer.logger = Mock()
            recognizer._cleanup_temp_files = Mock()
            recognizer.last_warning_message = ''
            recognizer.last_error_message = ''
            recognizer.last_transcript_path = ''

            result = recognizer.transcribe_video_to_subtitles(
                str(video), str(output)
            )
            transcript_path = root / 'recording.transcript.json'
            transcript = json.loads(transcript_path.read_text(encoding='utf-8'))

        self.assertEqual(result, str(output))
        self.assertEqual(transcript['provider'], 'voxtral')
        self.assertEqual(transcript['segments'][0]['text'], '识别字幕')
        self.assertEqual(recognizer.last_transcript_path, str(transcript_path))


if __name__ == '__main__':
    unittest.main()
