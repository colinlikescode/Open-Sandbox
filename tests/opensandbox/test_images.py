import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from opensandbox.cluster.registry import Registry
from opensandbox.errors import RuntimeError as SandboxRuntimeError
from opensandbox.errors import ValidationError
from opensandbox.images import validate_context
from opensandbox.sdk.files import DockerIgnore, pack, unpack


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, content in entries.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.parametrize("file_upload", [False, True])
@pytest.mark.parametrize(
    "location",
    [
        "/v2/images/blobs/uploads/id?_state=signed%2Bstate",
        "https://other-host/upload",
        "http://registry/upload",
    ],
)
def test_registry_upload_preserves_state_and_refuses_other_origins(tmp_path, file_upload, location):
    writes = []

    def registry_response(request):
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "POST":
            return httpx.Response(202, headers={"Location": location})
        writes.append(request)
        return httpx.Response(201)

    registry = Registry("https://registry", "user", "password")
    registry.client.close()
    registry.client = httpx.Client(
        base_url=registry.endpoint, transport=httpx.MockTransport(registry_response)
    )
    source = tmp_path / "blob"
    source.write_bytes(b"image content")

    def upload():
        return (
            registry.blob_file("images", source)
            if file_upload
            else registry.blob("images", source.read_bytes())
        )

    try:
        if location.startswith("/"):
            sha = upload()
            assert writes[0].url.params["_state"] == "signed+state"
            assert writes[0].url.params["digest"] == sha
            assert writes[0].content == source.read_bytes()
        else:
            with pytest.raises(SandboxRuntimeError, match="unexpected upload host"):
                upload()
            assert not writes
    finally:
        registry.close()


def test_transfer_rejects_input_symlink(tmp_path):
    (tmp_path / "source").write_text("data")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "source")
    with pytest.raises(ValidationError, match="symlinks"):
        pack(link)


def test_dockerignore_anchoring_and_negated_children(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "ignored").mkdir()
    for name in ["Dockerfile", "secret", "nested/secret", "ignored/include", "ignored/exclude"]:
        (tmp_path / name).write_text("content")
    (tmp_path / ".dockerignore").write_text("secret\nignored\n!ignored/include\n")
    data, name = pack(tmp_path, context=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
    assert name == "Dockerfile"
    assert "secret" not in names
    assert "nested/secret" in names
    assert "ignored/include" in names
    assert "ignored/exclude" not in names


@pytest.mark.parametrize("path", ["../escape", "/escape", "nested/../../escape"])
def test_context_and_download_reject_traversal(tmp_path, path):
    data = archive_bytes({"Dockerfile": b"FROM scratch", path: b"bad"})
    source = tmp_path / "context.tar"
    source.write_bytes(data)
    with pytest.raises(ValidationError):
        validate_context(source, "Dockerfile", 1024)
    with pytest.raises(ValidationError):
        unpack(data, tmp_path / "destination")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("compressed", [False, True])
def test_publish_docker_and_kaniko_archives_without_double_compression(tmp_path, compressed):
    raw_layer = archive_bytes({"hello": b"world"})
    source_layer = gzip.compress(raw_layer) if compressed else raw_layer
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {
                "type": "layers",
                "diff_ids": ["sha256:" + hashlib.sha256(raw_layer).hexdigest()],
            },
        }
    ).encode()
    data = archive_bytes(
        {
            "manifest.json": json.dumps([{"Config": "config.json", "Layers": ["layer"]}]).encode(),
            "config.json": config,
            "layer": source_layer,
        }
    )
    source = tmp_path / "image.tar"
    source.write_bytes(data)
    registry = Registry("https://registry", "user", "password")
    blobs = {}
    manifests = {}

    def blob(repository, data):
        sha = "sha256:" + hashlib.sha256(data).hexdigest()
        blobs[sha] = data
        return sha

    registry.blob = blob
    registry.blob_file = lambda repository, path: blob(repository, Path(path).read_bytes())

    def manifest(repository, tag, body):
        manifests[tag] = body
        return "sha256:image"

    registry.manifest = manifest
    try:
        image = registry.push_docker_archive(source, "images", "test")
    finally:
        registry.close()
    assert image["reference"] == "registry/images@sha256:image"
    layer = manifests["test"]["layers"][0]
    assert gzip.decompress(blobs[layer["digest"]]) == raw_layer


def test_ignore_nested_globstars_and_character_classes():
    ignore = DockerIgnore("**/*.log\n[ab].txt\n!keep.log")
    assert ignore.ignores("nested/debug.log")
    assert ignore.ignores("a.txt")
    assert not ignore.ignores("nested/a.txt")
    assert not ignore.ignores("keep.log")
