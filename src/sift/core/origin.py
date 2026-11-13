"""Where a file came from, read from its path alone.

`has_camera_exif` refines this later (M4); nothing here opens a file. Phone paths are compared
ignoring case, and /storage/emulated/0 counts as /sdcard.
"""

from typing import Literal

from sift.core.config import phone_path_key

Origin = Literal[
    "camera", "screenshot", "whatsapp_received", "whatsapp_sent", "whatsapp_private", "other"
]

# WhatsApp, WhatsApp Business, and the pre-scoped-storage location.
WHATSAPP_MEDIA_ROOTS: tuple[str, ...] = (
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Media",
    "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media",
    "/sdcard/WhatsApp/Media",
)

_WA_KEYS = tuple(phone_path_key(r) for r in WHATSAPP_MEDIA_ROOTS)
_DCIM = "/sdcard/dcim"
_PICTURES = "/sdcard/pictures"


def classify_origin(path: str) -> Origin:
    key = phone_path_key(path)
    for root in _WA_KEYS:
        if key.startswith(root + "/"):
            # Below the category folder ("WhatsApp Images"), a folder named Sent or Private
            # says who made the file; the file's own name never counts.
            folders = key[len(root) + 1 :].split("/")[1:-1]
            if "private" in folders:
                return "whatsapp_private"
            if "sent" in folders:
                return "whatsapp_sent"
            return "whatsapp_received"
    for base in (_DCIM, _PICTURES):
        if key.startswith(base + "/"):
            if "screenshots" in key[len(base) + 1 :].split("/")[:-1]:
                return "screenshot"
            if base == _DCIM:
                return "camera"
    return "other"
