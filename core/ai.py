import logging
from faster_whisper import WhisperModel

logger = logging.getLogger("core.ai")

model = None


def get_whisper_model():
    global model
    if model is None:
        try:
            # Using 'tiny' for faster real-time performance on CPU
            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            logger.info("Whisper model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            model = None
    return model
