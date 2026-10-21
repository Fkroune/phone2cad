"""The live preview reads COLMAP files while COLMAP is still writing them (fast, no scan needed)."""

import struct

import numpy as np

from phone2cad_worker.preview import depth_points

BOX = (np.full(3, -100.0), np.full(3, 100.0))


def _images_bin(names: list[str]) -> bytes:
    out = struct.pack("<Q", len(names))
    for i, name in enumerate(names):
        out += struct.pack("<I4d3dI", i + 1, 1, 0, 0, 0, 0, 0, 0, 1) + name.encode() + b"\0" + struct.pack("<Q", 0)
    return out


def _dense_dir(tmp_path, names: list[str]):
    sp, maps = tmp_path / "sparse", tmp_path / "stereo" / "depth_maps"
    sp.mkdir(parents=True)
    maps.mkdir(parents=True)
    (sp / "images.bin").write_bytes(_images_bin(names))
    # One PINHOLE camera, 4x3 px.
    (sp / "cameras.bin").write_bytes(struct.pack("<QiIQQ4d", 1, 1, 1, 4, 3, 2.0, 2.0, 2.0, 1.5))
    return sp, maps


def test_a_depth_map_still_being_written_is_skipped(tmp_path):
    _, maps = _dense_dir(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])
    (maps / "a.jpg.geometric.bin").write_bytes(b"4&3&1&" + np.full(12, 10.0, np.float32).tobytes())
    (maps / "b.jpg.geometric.bin").write_bytes(b"4&3")  # header cut short
    (maps / "c.jpg.geometric.bin").write_bytes(b"")  # created, nothing written yet
    P, done = depth_points(tmp_path, BOX, stride=1)
    assert done == ["a.jpg"]
    assert len(P) == 12 and np.allclose(P[:, 2], 10.0)


def test_an_images_file_still_being_written_gives_no_points(tmp_path):
    sp, maps = _dense_dir(tmp_path, ["a.jpg"])
    (maps / "a.jpg.geometric.bin").write_bytes(b"4&3&1&" + np.full(12, 10.0, np.float32).tobytes())
    full = (sp / "images.bin").read_bytes()
    (sp / "images.bin").write_bytes(full[:full.index(b"a.jpg") + 2])  # cut inside the name
    P, done = depth_points(tmp_path, BOX)
    assert len(P) == 0 and done == []
