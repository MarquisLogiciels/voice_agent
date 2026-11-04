"""Call recording via LiveKit Egress (audio-only room composite → S3)."""

from __future__ import annotations

from typing import Any

from ..shared.blobstore import tenant_key
from ..shared.logging import get_logger
from ..shared.settings import Settings

logger = get_logger(__name__)


class RecordingService:
    def __init__(self, lkapi: Any, settings: Settings) -> None:
        self._api = lkapi
        self._settings = settings

    def object_key(self, tenant_id: Any, conversation_id: Any) -> str:
        return tenant_key(tenant_id, "recordings", f"{conversation_id}.ogg")

    async def start(self, *, room_name: str, tenant_id: Any, conversation_id: Any) -> str | None:
        """Returns the egress id, or None if recording to object storage isn't configured."""
        from livekit import api

        s = self._settings
        if s.blob_backend != "s3":
            logger.warning("recording requested but blob backend is not s3; skipping")
            return None
        upload = api.S3Upload(
            bucket=s.s3_bucket,
            region=s.s3_region,
            endpoint=s.s3_endpoint_url or "",
            force_path_style=bool(s.s3_endpoint_url),
        )
        if s.s3_access_key_id and s.s3_secret_access_key:
            upload.access_key = s.s3_access_key_id.get_secret_value()
            upload.secret = s.s3_secret_access_key.get_secret_value()
        req = api.RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.EncodedFileType.OGG,
                    filepath=self.object_key(tenant_id, conversation_id),
                    s3=upload,
                )
            ],
        )
        info = await self._api.egress.start_room_composite_egress(req)
        return str(info.egress_id)

    async def stop(self, egress_id: str) -> None:
        from livekit import api

        try:
            await self._api.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
        except Exception:
            # Egress also stops on its own when the room closes.
            logger.info("stop_egress failed (room may already be closed)", exc_info=True)
