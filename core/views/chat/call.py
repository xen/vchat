import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import List

import numpy as np
import sqlalchemy as sa
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRecorder
from av.audio.resampler import AudioResampler

from core.ai import get_whisper_model
from core.db import async_session_factory
from core.models import Chat, ChatMsg, Project
from core.utils import json
from core.views.chat._types import Msg
from core.views.chat.views import (
    ai_chat_stream,
    build_generation_context,
    generate_tts_audio,
    get_context,
)

logger = logging.getLogger("core.call")

STATIC_PATH = Path(__file__).parent.parent.parent.parent / "static"
DATA_PATH = Path(__file__).parent.parent.parent.parent / "data"


# Initialize Whisper model (lazy load via getter)
model = get_whisper_model()


class AudioTransformTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track, queue):
        super().__init__()
        self.track = track
        self.queue = queue

    async def recv(self):
        frame = await self.track.recv()
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass
        return frame


async def transcription_worker(queue, ws, chat_id, user_uid, project_id):
    """
    Consumes audio frames from the queue, resamples them, and performs transcription.
    """
    resampler = AudioResampler(format="s16", layout="mono", rate=16000)
    buffer = []
    buffer_duration = 0.0
    MAX_BUFFER_DURATION = 3.0  # Transcribe every 3 seconds

    logger.info("Transcription worker started")

    try:
        while True:
            frame = await queue.get()
            if frame is None:
                break

            # Resample to 16kHz mono
            try:
                resampled_frames = resampler.resample(frame)
            except Exception as e:
                logger.error(f"Resampling error: {e}")
                continue

            for r_frame in resampled_frames:
                # Convert to numpy array (int16)
                arr = r_frame.to_ndarray().flatten()
                # Convert to float32 and normalize to [-1, 1]
                arr = arr.astype(np.float32) / 32768.0

                buffer.append(arr)
                buffer_duration += len(arr) / 16000.0

            if buffer_duration >= MAX_BUFFER_DURATION:
                if not buffer:
                    continue

                full_audio = np.concatenate(buffer)
                buffer = []
                buffer_duration = 0.0

                if model:
                    try:
                        # Run transcription in a separate thread to avoid blocking the event loop
                        segments, _ = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: list(
                                model.transcribe(full_audio, language="ru", beam_size=5)
                            ),
                        )

                        text = " ".join([segment.text for segment in segments]).strip()
                        if text:
                            logger.info(f"Transcribed: {text}")
                            await ws.send_json({"type": "transcription", "text": text})

                            # --- AI Interaction Loop ---
                            try:
                                # 1. Notify frontend: Processing
                                await ws.send_json(
                                    {"type": "status", "state": "processing"}
                                )

                                # 2. Save User Message
                                async with async_session_factory() as db:
                                    await db.execute(
                                        sa.insert(ChatMsg)
                                        .values(
                                            text=text,
                                            role="user",
                                            full_context="",
                                            chat_id=chat_id,
                                            user_uid=str(user_uid),
                                            created_at=sa.func.now(),
                                        )
                                        .returning(ChatMsg.id)
                                    )
                                    await db.commit()

                                # 3. Get Context
                                async with async_session_factory() as db:
                                    project = await db.get(Project, project_id)
                                    history: List[Msg] = await get_context(
                                        db=db,
                                        chat_id=chat_id,
                                        prompt=text,
                                        project_id=project_id,
                                    )

                                gen_context = build_generation_context(project)

                                messages = [dict(m._asdict()) for m in history]
                                messages.append({"role": "user", "content": text})

                                # 4. Get AI Response
                                total_content = ""
                                async for event in ai_chat_stream(
                                    messages, gen_context
                                ):
                                    if event.get("event") == "content":
                                        total_content += event.get("data", "")

                                if not total_content:
                                    continue

                                # 5. Save AI Message
                                async with async_session_factory() as db:
                                    await db.execute(
                                        sa.insert(ChatMsg).values(
                                            text=total_content,
                                            role="assistant",
                                            full_context="",
                                            chat_id=chat_id,
                                            user_uid=str(user_uid),
                                            created_at=sa.func.now(),
                                            provider=gen_context.provider_id,
                                            model=gen_context.model_id,
                                        )
                                    )
                                    await db.commit()

                                # 6. Generate TTS
                                audio_bytes = await generate_tts_audio(total_content)
                                if audio_bytes:
                                    audio_b64 = base64.b64encode(audio_bytes).decode(
                                        "ascii"
                                    )
                                    await ws.send_json(
                                        {
                                            "type": "audio_response",
                                            "audio": audio_b64,
                                            "text": total_content,
                                        }
                                    )

                                # 7. Notify frontend: Ready
                                await ws.send_json({"type": "status", "state": "ready"})

                            except Exception as ai_exc:
                                logger.error(f"AI loop error: {ai_exc}")
                                await ws.send_json({"type": "status", "state": "error"})

                    except Exception as e:
                        logger.error(f"Transcription error: {e}")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Worker error: {e}")
    finally:
        logger.info("Transcription worker stopped")


async def call_websocket_handler(request):
    chat_id_param = request.rel_url.query.get("chat_id")
    if not chat_id_param:
        raise web.HTTPBadRequest(text="chat_id query parameter is required")
    try:
        chat_id = int(chat_id_param)
    except ValueError:
        raise web.HTTPBadRequest(text="chat_id must be an integer")

    project_id = int(request.match_info.get("project_id"))

    async with async_session_factory() as db:
        chat_exists = await db.scalar(
            sa.select(Chat.id).where(Chat.id == chat_id, Chat.project_id == project_id)
        )

    if not chat_exists:
        raise web.HTTPNotFound(text="Chat not found for this project")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    logger.info("Call initiated for project %s, chat %s", project_id, chat_id)

    pc = RTCPeerConnection()
    player = None
    recorder = None
    filename = None
    record_relative_path = None

    # Queue for audio frames
    audio_queue = asyncio.Queue(maxsize=1000)
    # We need user_uid for ChatMsg. Assuming it's available or we use a placeholder.
    # In call_websocket_handler we don't have user_id from session easily unless we parse cookies or token.
    # But wait, the chat_id is linked to a user.
    # Let's fetch the user_uid from the Chat record or use a default.

    user_uid = "system"  # Default fallback
    async with async_session_factory() as db:
        chat_obj = await db.get(Chat, chat_id)
        if chat_obj and chat_obj.user_uid:
            user_uid = chat_obj.user_uid

    transcription_task = asyncio.create_task(
        transcription_worker(audio_queue, ws, chat_id, user_uid, project_id)
    )

    @pc.on("track")
    def on_track(track):
        logger.info("Track received: %s", track.kind)
        if track.kind == "audio":
            # Intercept audio track
            intercepted_track = AudioTransformTrack(track, audio_queue)
            if recorder:
                recorder.addTrack(intercepted_track)

    try:
        # Ensure destination exists and prepare recorder
        DATA_PATH.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        filename = DATA_PATH / f"call_{project_id}_{timestamp}.wav"
        recorder = MediaRecorder(str(filename))
        record_relative_path = f"/data/{filename.name}"

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)

                if data["type"] == "offer":
                    offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
                    await pc.setRemoteDescription(offer)

                    # Start recording
                    await recorder.start()

                    # Create media player for MP3
                    audio_path = STATIC_PATH / "audio" / "dial_tone.mp3"
                    if audio_path.exists():
                        player = MediaPlayer(str(audio_path))
                        pc.addTrack(player.audio)
                    else:
                        logger.error(f"Audio file not found: {audio_path}")

                    # Create answer
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)

                    await ws.send_json(
                        {"type": "answer", "sdp": pc.localDescription.sdp}
                    )

            elif msg.type == web.WSMsgType.ERROR:
                logger.error("ws connection closed with exception %s", ws.exception())

    except Exception as e:
        logger.error(f"Error in call handler: {e}")
    finally:
        # Stop worker
        await audio_queue.put(None)
        transcription_task.cancel()
        try:
            await transcription_task
        except asyncio.CancelledError:
            pass

        # MediaPlayer doesn't have a stop method, it's cleaned up when tracks are closed
        if recorder:
            await recorder.stop()
        await pc.close()
        logger.info("Call ended")

        if filename and filename.exists() and record_relative_path:
            try:
                async with async_session_factory() as db:
                    chat = await db.get(Chat, chat_id)
                    if chat:
                        chat.record = record_relative_path
                        await db.commit()
            except Exception as db_exc:
                logger.error("Failed to update chat record path: %s", db_exc)

    return ws
