import pytest

from sift.core.origin import classify_origin

SD = "/sdcard"
WA = f"{SD}/Android/media/com.whatsapp/WhatsApp/Media"
WAB = f"{SD}/Android/media/com.whatsapp.w4b/WhatsApp Business/Media"
LEGACY = f"{SD}/WhatsApp/Media"


@pytest.mark.parametrize(
    ("path", "origin"),
    [
        (f"{SD}/DCIM/Camera/IMG_1.jpg", "camera"),
        (f"{SD}/DCIM/100ANDRO/x.jpg", "camera"),
        (f"{SD}/dcim/camera/IMG_1.jpg", "camera"),
        ("/storage/emulated/0/DCIM/Camera/IMG_1.jpg", "camera"),
        (f"{SD}/DCIM/Screenshots/Screenshot_1.png", "screenshot"),
        (f"{SD}/Pictures/Screenshots/Screenshot_1.png", "screenshot"),
        (f"{SD}/pictures/screenshots/Screenshot_1.png", "screenshot"),
        (f"{WA}/WhatsApp Images/IMG-1.jpg", "whatsapp_received"),
        (f"{WA}/WhatsApp Images/Sent/IMG-1.jpg", "whatsapp_sent"),
        (f"{WA}/WhatsApp Images/Private/IMG-1.jpg", "whatsapp_private"),
        (f"{WA}/WhatsApp Voice Notes/202401/PTT-1.opus", "whatsapp_received"),
        (f"{WA}/WhatsApp Documents/Sent/a.pdf", "whatsapp_sent"),
        (f"{WA}/whatsapp video/sent/VID-1.mp4", "whatsapp_sent"),
        (f"{WA}/.Statuses/VID-1.mp4", "whatsapp_received"),
        (f"{WAB}/WhatsApp Business Images/IMG-1.jpg", "whatsapp_received"),
        (f"{WAB}/WhatsApp Business Images/Sent/IMG-1.jpg", "whatsapp_sent"),
        (f"{LEGACY}/WhatsApp Video/Private/VID-1.mp4", "whatsapp_private"),
        (f"{LEGACY}/WhatsApp Images/IMG-1.jpg", "whatsapp_received"),
        # A file literally named Sent is a file, not a folder.
        (f"{WA}/WhatsApp Images/Sent", "whatsapp_received"),
        (f"{SD}/Download/a.bin", "other"),
        (f"{SD}/notes.txt", "other"),
        (f"{SD}/Recordings/Call/a.m4a", "other"),
        (f"{SD}/DCIMX/a.jpg", "other"),
        (f"{SD}/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db", "other"),
        (f"{SD}/Download/Screenshots/a.png", "other"),
    ],
)
def test_origin_table(path: str, origin: str) -> None:
    assert classify_origin(path) == origin
