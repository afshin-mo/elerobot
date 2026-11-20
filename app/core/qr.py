"""QR code image for an elevator link."""
import io

import segno


def qr_png(text: str, scale: int = 10) -> bytes:
    """The text (a t.me link) as a PNG image, with the quiet zone scanners need."""
    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="png", scale=scale, border=4)
    return buffer.getvalue()
