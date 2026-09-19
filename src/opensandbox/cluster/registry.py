"""OCI registry uploads, including the sandbox companion image; no Docker daemon."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from urllib.parse import urljoin, urlparse

import httpx

from opensandbox.errors import RuntimeError

OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Registry:
    def __init__(self, endpoint: str, username: str, password: str, *, verify=True):
        self.endpoint = endpoint.rstrip("/")
        self.client = httpx.Client(
            base_url=self.endpoint,
            auth=(username, password),
            verify=verify,
            timeout=300,
            trust_env=False,
        )

    def close(self):
        self.client.close()

    def blob(self, repository: str, data: bytes):
        sha = digest(data)
        response = self.client.head(f"/v2/{repository}/blobs/{sha}")
        if response.status_code == 200:
            return sha
        response = self.client.post(f"/v2/{repository}/blobs/uploads/")
        response.raise_for_status()
        location = urljoin(self.endpoint + "/", response.headers["Location"])
        # A registry cannot redirect authenticated uploads to another origin.
        if (urlparse(location).scheme, urlparse(location).netloc) != (
            urlparse(self.endpoint).scheme,
            urlparse(self.endpoint).netloc,
        ):
            raise RuntimeError("Registry returned an unexpected upload host")
        response = self.client.put(
            httpx.URL(location).copy_merge_params({"digest": sha}),
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        response.raise_for_status()
        return sha

    def manifest(self, repository: str, reference: str, manifest: dict, media_type=OCI_MANIFEST):
        data = json.dumps(manifest, separators=(",", ":")).encode()
        response = self.client.put(
            f"/v2/{repository}/manifests/{reference}",
            content=data,
            headers={"Content-Type": media_type},
        )
        response.raise_for_status()
        return digest(data)

    def push_agent(self, binary: bytes, architecture: str, tag: str):
        layer = io.BytesIO()
        with tarfile.open(fileobj=layer, mode="w") as archive:
            item = tarfile.TarInfo("agent")
            item.size, item.mode, item.mtime = len(binary), 0o755, 0
            archive.addfile(item, io.BytesIO(binary))
        raw = layer.getvalue()
        compressed = gzip.compress(raw, mtime=0)
        config = json.dumps(
            {
                "architecture": architecture,
                "os": "linux",
                "config": {"Entrypoint": ["/agent"]},
                "rootfs": {"type": "layers", "diff_ids": [digest(raw)]},
            },
            separators=(",", ":"),
        ).encode()
        repository = "opensandbox/agent"
        manifest = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": OCI_CONFIG,
                "digest": self.blob(repository, config),
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": OCI_LAYER,
                    "digest": self.blob(repository, compressed),
                    "size": len(compressed),
                }
            ],
        }
        sha = self.manifest(repository, tag + "-" + architecture, manifest)
        return {
            "mediaType": OCI_MANIFEST,
            "digest": sha,
            "size": len(json.dumps(manifest, separators=(",", ":")).encode()),
            "platform": {"architecture": architecture, "os": "linux"},
        }

    def push_agent_index(self, manifests: list[dict], tag: str):
        media_type = "application/vnd.oci.image.index.v1+json"
        sha = self.manifest(
            "opensandbox/agent",
            tag,
            {"schemaVersion": 2, "mediaType": media_type, "manifests": manifests},
            media_type,
        )
        return f"{urlparse(self.endpoint).netloc}/opensandbox/agent@{sha}"

    def blob_file(self, repository: str, path):
        with open(path, "rb") as source:
            sha = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
        response = self.client.head(f"/v2/{repository}/blobs/{sha}")
        if response.status_code == 200:
            return sha
        response = self.client.post(f"/v2/{repository}/blobs/uploads/")
        response.raise_for_status()
        location = urljoin(self.endpoint + "/", response.headers["Location"])
        if (urlparse(location).scheme, urlparse(location).netloc) != (
            urlparse(self.endpoint).scheme,
            urlparse(self.endpoint).netloc,
        ):
            raise RuntimeError("Registry returned an unexpected upload host")
        from pathlib import Path

        with open(path, "rb") as source:
            response = self.client.put(
                httpx.URL(location).copy_merge_params({"digest": sha}),
                content=iter(lambda: source.read(1024 * 1024), b""),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(Path(path).stat().st_size),
                },
            )
        response.raise_for_status()
        return sha

    def push_docker_archive(self, path, repository: str, tag: str):
        """Publish Kaniko/go-containerregistry or Docker archives without extraction.

        Kaniko saves compressed layers; Docker save generally stores plain tar.
        Preserve gzip layers and compress plain layers exactly once. Temporary
        files keep a multi-gigabyte image from consuming the head's RAM.
        """
        with tarfile.open(path) as archive:

            def member_stream(name, maximum=8 * 1024**3):
                member = archive.getmember(name)
                if not member.isfile() or member.size > maximum:
                    raise RuntimeError("Invalid image archive member")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError("Missing image archive member")
                return stream

            with member_stream("manifest.json", 1024 * 1024) as stream:
                listing = json.load(stream)
            if len(listing) != 1:
                raise RuntimeError("Expected one built image")
            image = listing[0]
            with member_stream(image["Config"], 8 * 1024 * 1024) as stream:
                config = stream.read()
            config_data = json.loads(config)
            if config_data.get("os") != "linux" or config_data.get("architecture") not in {
                "amd64",
                "arm64",
            }:
                raise RuntimeError("Built images must target Linux amd64 or arm64")
            layers = []
            if len(image["Layers"]) > 1000:
                raise RuntimeError("Image has too many layers")
            for name in image["Layers"]:
                with (
                    member_stream(name) as stream,
                    tempfile.NamedTemporaryFile(dir=path.parent) as compressed,
                ):
                    magic = stream.read(2)
                    stream.seek(0)
                    if magic == b"\x1f\x8b":
                        shutil.copyfileobj(stream, compressed, 1024 * 1024)
                    else:
                        with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as output:
                            shutil.copyfileobj(stream, output, 1024 * 1024)
                    compressed.flush()
                    layers.append(
                        {
                            "mediaType": OCI_LAYER,
                            "digest": self.blob_file(repository, compressed.name),
                            "size": compressed.tell(),
                        }
                    )
            manifest = {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": {
                    "mediaType": OCI_CONFIG,
                    "digest": self.blob(repository, config),
                    "size": len(config),
                },
                "layers": layers,
            }
            sha = self.manifest(repository, tag, manifest)
            return {
                "reference": f"{urlparse(self.endpoint).netloc}/{repository}@{sha}",
                "digest": sha,
                "architecture": config_data["architecture"],
                "size": sum(layer["size"] for layer in layers),
            }
