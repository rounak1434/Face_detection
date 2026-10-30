import numpy as np
import pytest

from inference_service.imaging import InvalidImage, TooLarge, UnsupportedFormat, decode

from . import fixtures as fx

MB = 1024 * 1024


def test_decodes_jpeg_png_webp_to_bgr():
    img = fx.solid(64, 48)
    img[:, :, 0] = 200  # red channel in RGB
    for fmt, mime in (("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")):
        bgr, got = decode(fx.encode(img, fmt, quality=100) if fmt != "PNG" else fx.encode(img, fmt), MB, 10**7)
        assert got == mime
        assert bgr.shape == (48, 64, 3)
        assert bgr[0, 0, 2] > 150  # red ends up in the last (R) channel of BGR


def test_png_with_alpha_is_flattened_to_three_channels():
    rgba = np.zeros((10, 10, 4), dtype=np.uint8)
    rgba[..., 3] = 255
    bgr, _ = decode(fx.encode(rgba, "PNG"), MB, 10**7)
    assert bgr.shape == (10, 10, 3)


def test_exif_orientation_is_applied():
    portrait = fx.solid(40, 80)  # 40 wide, 80 high
    rotated, _ = decode(fx.jpeg_with_orientation(portrait, 6), MB, 10**7)  # "rotate 90 CW" tag
    assert rotated.shape[:2] == (40, 80)


@pytest.mark.parametrize("data", [fx.GIF, fx.TIFF, b"%PDF-1.7 ...", b"just text"])
def test_unsupported_formats(data):
    with pytest.raises(UnsupportedFormat):
        decode(data, MB, 10**7)


def test_content_type_is_not_trusted_magic_bytes_are():
    # A GIF renamed .jpg would still be rejected because the bytes say GIF.
    with pytest.raises(UnsupportedFormat):
        decode(fx.GIF, MB, 10**7)


@pytest.mark.parametrize("data", [b"", b"\xff\xd8\xff\xe0" + b"\x00" * 50, fx.encode(fx.solid(), "JPEG")[:200]])
def test_corrupt_or_truncated_images(data):
    with pytest.raises(InvalidImage):
        decode(data, MB, 10**7)


def test_byte_limit():
    with pytest.raises(TooLarge):
        decode(fx.encode(fx.noise(400, 400), "PNG"), 1000, 10**7)


def test_pixel_limit_checked_before_decoding():
    bomb = fx.png_header_only(50_000, 50_000)  # tiny file, 2.5 gigapixels claimed
    assert len(bomb) < 100
    with pytest.raises(TooLarge):
        decode(bomb, MB, 40_000_000)
