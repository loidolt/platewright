"""Labels on a Brother QL label printer on the network.

Two kinds:

- **spool labels**, drawn by Bambuddy (``POST /inventory/labels/preview`` with a custom layout the
  size of the loaded label), so they look the same as the ones its Spool Inventory prints;
- **bag labels** for finished parts: what's in the bag, for which demand, and a QR code back to it.
  Bambuddy has no project labels, so these are drawn here.

Either way the picture is turned into Brother's raster commands (``brother_ql``) and sent to the
printer, by default as an IPP print job (port 631, the way AirPrint and CUPS reach it): the printer
answers whether it took the job, so a refusal shows up as an error. The raw port, 9100, is the other
choice, for printers without IPP; it gives no answer. Both were checked on a QL-810W (firmware 1.25). The printer is set up in Settings: its address, how to reach it, its model and
the label roll that is loaded. An empty address means no label printer.
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets
import struct
from dataclasses import dataclass
from functools import lru_cache

import httpx

import segno
from brother_ql.conversion import convert
from brother_ql.labels import Color, FormFactor, LabelsManager
from brother_ql.models import ModelsManager
from brother_ql.raster import BrotherQLRaster
from PIL import Image, ImageDraw, ImageFont

DPI = 300
PORT = 9100  # raw
IPP_PORT = 631
IPP_PATH = "/ipp/print"
PROTOCOLS = {"ipp": "IPP print job (port 631)", "raw": "Raw (port 9100)"}
ENDLESS_LENGTH_MM = 90  # how long a label cut from a continuous roll is
MAX_COPIES = 20


class LabelError(Exception):
    pass


@dataclass(frozen=True)
class LabelSize:
    """A label roll, drawn landscape: ``width`` x ``height`` pixels at 300 dpi."""

    identifier: str
    name: str
    width: int
    height: int
    endless: bool
    across: int  # pixels across the roll, which the printer wants as the picture's width

    @property
    def mm(self) -> tuple[float, float]:
        """The printable area in mm, landscape, as Bambuddy's custom label layout takes it."""
        return round(self.width * 25.4 / DPI, 1), round(self.height * 25.4 / DPI, 1)


def models() -> list[str]:
    return list(ModelsManager().identifiers())


def sizes() -> list[LabelSize]:
    """The black-on-white rectangular rolls: die-cut sizes, then continuous."""
    out = []
    for label in LabelsManager().iter_elements():
        if label.color != Color.BLACK_WHITE or label.form_factor == FormFactor.ROUND_DIE_CUT:
            continue
        across, along = label.dots_printable
        endless = label.form_factor == FormFactor.ENDLESS
        if endless:
            along = round(ENDLESS_LENGTH_MM * DPI / 25.4)
            name = f"{label.tape_size[0]} mm continuous, cut at {ENDLESS_LENGTH_MM} mm"
        else:
            name = f"{label.tape_size[0]} × {label.tape_size[1]} mm die-cut"
        # Landscape: the long side across the picture.
        width, height = max(across, along), min(across, along)
        out.append(LabelSize(label.identifier, name, width, height, endless, across))
    return sorted(out, key=lambda s: (s.endless, s.identifier))


def size(identifier: str) -> LabelSize:
    for s in sizes():
        if s.identifier == identifier:
            return s
    raise LabelError(f"unknown label size {identifier!r}")


def problems(host: str, model: str, label: str, protocol: str = "ipp") -> list[str]:
    """What's wrong with the label printer settings; nothing when no printer is set up."""
    if not host.strip():
        return []
    out = []
    if protocol not in PROTOCOLS:
        out.append(f"label printer: unknown connection {protocol!r}")
    if any(c in host for c in "/ :@?#"):
        out.append("label printer: give only its address or host name, e.g. 192.168.1.40")
    if model not in models():
        out.append(f"label printer: unknown model {model!r}")
    try:
        size(label)
    except LabelError:
        out.append(f"label printer: unknown label size {label!r}")
    return out


# -- drawing ------------------------------------------------------------------------------------

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts",
             "/System/Library/Fonts/Supplemental")
FONT_FILES = {True: ("DejaVuSans-Bold.ttf", "Arial Bold.ttf"), False: ("DejaVuSans.ttf", "Arial.ttf")}


@lru_cache(maxsize=64)
def font(px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """DejaVu Sans (in the image), Arial on a Mac, else Pillow's own font."""
    override = os.environ.get("PLATEWRIGHT_LABEL_FONT_BOLD" if bold else "PLATEWRIGHT_LABEL_FONT")
    candidates = ([override] if override else []) + [os.path.join(d, f) for d in FONT_DIRS for f in FONT_FILES[bold]]
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, px)
    return ImageFont.load_default(px)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_w: int, px: int, bold: bool, min_px: int = 22):
    """The largest font up to ``px`` the text fits in, shortened with … if even ``min_px`` is too wide."""
    while px > min_px and draw.textlength(text, font=font(px, bold)) > max_w:
        px -= 2
    f = font(px, bold)
    if draw.textlength(text, font=f) > max_w:
        while text and draw.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, f


def bag_label(spec: LabelSize, *, title: str, headline: str, lines: list[str], qr_url: str = "") -> Image.Image:
    """A bag label: title (the demand), a headline (what's inside), small lines, QR on the right."""
    w, h = spec.width, spec.height
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    pad = max(round(h * 0.06), 10)
    text_right = w - pad
    if qr_url:
        qr = segno.make(qr_url, error="m")
        side = h - 2 * pad
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=max(side // (qr.symbol_size(border=0)[0]), 1), border=0)
        code = Image.open(buf).convert("L")
        code = code.resize((side, side), Image.NEAREST) if code.width > side else code
        img.paste(code, (w - pad - code.width, (h - code.height) // 2))
        text_right = w - 2 * pad - code.width
    max_w = text_right - pad
    rows = [(title, round(h * 0.22), True), (headline, round(h * 0.20), True)] + \
           [(t, round(h * 0.11), False) for t in lines if t]
    fitted = [(*_fit_text(draw, t, max_w, px, bold), px) for t, px, bold in rows if t]
    heights = [px for *_, px in fitted]
    gap = max((h - 2 * pad - sum(heights)) / max(len(fitted) - 1, 1), 4)
    if gap > h * 0.08:  # few lines: keep them together, centered
        gap = h * 0.08
    y = (h - sum(heights) - gap * (len(fitted) - 1)) / 2
    for (text, f, px) in fitted:
        draw.text((pad, y), text, font=f, fill=0)
        y += px + gap
    return img


def fit(img: Image.Image, spec: LabelSize) -> Image.Image:
    """A picture onto the label exactly: grey, landscape, scaled to fit and centered on white."""
    img = img.convert("L")
    if (img.width < img.height) != (spec.width < spec.height):
        img = img.rotate(90, expand=True)
    if img.size != (spec.width, spec.height):
        scale = min(spec.width / img.width, spec.height / img.height)
        img = img.resize((max(round(img.width * scale), 1), max(round(img.height * scale), 1)), Image.LANCZOS)
        canvas = Image.new("L", (spec.width, spec.height), 255)
        canvas.paste(img, ((spec.width - img.width) // 2, (spec.height - img.height) // 2))
        img = canvas
    return img


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# -- printing -------------------------------------------------------------------------------------

def raster(img: Image.Image, model: str, spec: LabelSize, copies: int = 1) -> bytes:
    """Brother raster commands for ``copies`` of a label, each cut."""
    img = fit(img, spec)
    # The printer wants the picture as wide as the roll: turn the landscape picture upright unless
    # its long side already runs across the roll.
    upright = img if img.width == spec.across else img.rotate(90, expand=True)
    qlr = BrotherQLRaster(model)
    qlr.exception_on_warning = True
    convert(qlr, [upright] * min(max(copies, 1), MAX_COPIES), label=spec.identifier, rotate=0, threshold=70,
            dither=False, compress=True, red=False, dpi_600=False, hq=True, cut=True)
    return bytes(qlr.data)


def _ipp_attr(tag: int, name: str, value: str) -> bytes:
    n, v = name.encode(), value.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v


def ipp_print_job(host: str, data: bytes, job_name: str, request_id: int = 1) -> bytes:
    """An IPP/1.1 Print-Job request carrying the raster commands as-is (application/octet-stream,
    which Brother's QL printers list in document-format-supported)."""
    attrs = b"".join((
        _ipp_attr(0x47, "attributes-charset", "utf-8"),
        _ipp_attr(0x48, "attributes-natural-language", "en"),
        _ipp_attr(0x45, "printer-uri", f"ipp://{host}:{IPP_PORT}{IPP_PATH}"),
        _ipp_attr(0x42, "requesting-user-name", "platewright"),
        _ipp_attr(0x42, "job-name", job_name[:100]),
        _ipp_attr(0x49, "document-format", "application/octet-stream"),
    ))
    # version 1.1, operation Print-Job (0x0002), request id; operation attributes; end of attributes
    return struct.pack(">BBHI", 1, 1, 0x0002, request_id) + b"\x01" + attrs + b"\x03" + data


async def send_ipp(host: str, data: bytes, job_name: str = "Platewright label", timeout: float = 20,
                   transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Send raster commands as an IPP print job. Raises LabelError unless the printer accepts it."""
    body = ipp_print_job(host, data, job_name, secrets.randbelow(2**31 - 1) + 1)
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(f"http://{host}:{IPP_PORT}{IPP_PATH}", content=body,
                                         headers={"Content-Type": "application/ipp"})
    except httpx.HTTPError as exc:
        raise LabelError(f"can't reach the label printer at {host}:{IPP_PORT} ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise LabelError(f"the label printer at {host} answered HTTP {response.status_code} to the print job")
    if len(response.content) < 8:
        raise LabelError(f"the label printer at {host} gave no IPP answer")
    status = struct.unpack(">H", response.content[2:4])[0]
    if status >= 0x0100:  # 0x00xx are the successful-ok codes
        raise LabelError(f"the label printer at {host} refused the print job (IPP status 0x{status:04x})")


async def send_raw(host: str, data: bytes, port: int = PORT, timeout: float = 10) -> None:
    """Write raster commands to the printer's raw port."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise LabelError(f"can't reach the label printer at {host}:{port} ({type(exc).__name__})") from exc
    try:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise LabelError(f"the label printer at {host} stopped taking data ({type(exc).__name__})") from exc
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout)
        except (OSError, asyncio.TimeoutError):
            pass


async def print_label(img: Image.Image, host: str, model: str, label: str, copies: int = 1,
                      protocol: str = "ipp") -> None:
    spec = size(label)
    data = await asyncio.to_thread(raster, img, model, spec, copies)
    if protocol == "raw":
        await send_raw(host, data)
    else:
        await send_ipp(host, data)
