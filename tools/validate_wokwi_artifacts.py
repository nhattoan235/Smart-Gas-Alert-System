"""Verify firmware outputs in the isolated PlatformIO tree before Wokwi starts."""
from __future__ import annotations

import argparse
import hashlib
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path


NODES = ("node1", "node2", "node3")
ARTIFACTS = (("firmware", "firmware.bin"), ("elf", "firmware.elf"))


class ArtifactValidationError(AssertionError):
    pass


def validate_artifacts(firmware_root: Path, nodes: tuple[str, ...] = NODES) -> list[dict[str, object]]:
    firmware_root = Path(firmware_root).resolve()
    expected_build_root = (firmware_root / ".pio" / "build").resolve()
    try:
        expected_build_root.relative_to(firmware_root)
    except ValueError as exc:
        raise ArtifactValidationError("resolved PlatformIO build directory is outside the isolated firmware root") from exc
    results: list[dict[str, object]] = []
    for node in nodes:
        sim_dir = firmware_root / "sim" / node
        config_path = sim_dir / "wokwi.toml"
        if not config_path.is_file():
            raise ArtifactValidationError(f"{node}: Wokwi config is missing: {config_path}")
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        for artifact_key, filename in ARTIFACTS:
            configured = config.get("wokwi", {}).get(artifact_key)
            if not isinstance(configured, str) or not configured.strip():
                raise ArtifactValidationError(f"{node}: configured {artifact_key} path is missing")
            artifact = (sim_dir / configured).resolve(strict=False)
            expected = (expected_build_root / node / filename).resolve(strict=False)
            try:
                artifact.relative_to(firmware_root)
            except ValueError as exc:
                raise ArtifactValidationError(
                    f"{node}: configured {artifact_key} artifact resolves outside the isolated firmware root: {artifact}"
                ) from exc
            if artifact != expected:
                raise ArtifactValidationError(
                    f"{node}: configured {artifact_key} resolves outside the expected current-run output: {artifact}"
                )
            if not artifact.is_file():
                raise ArtifactValidationError(f"{node}: fresh {artifact_key} artifact is missing: {artifact}")
            size = artifact.stat().st_size
            if size <= 0:
                raise ArtifactValidationError(f"{node}: {artifact_key} artifact is empty: {artifact}")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            modified = datetime.fromtimestamp(artifact.stat().st_mtime, timezone.utc).isoformat()
            results.append({
                "node": node,
                "kind": artifact_key,
                "path": str(artifact),
                "size_bytes": size,
                "sha256": digest,
                "modified_utc": modified,
            })
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        artifacts = validate_artifacts(args.firmware_root)
    except (OSError, KeyError, ValueError, ArtifactValidationError) as exc:
        print(f"FAIL artifact validation: {exc}", file=sys.stderr)
        return 1
    for item in artifacts:
        print(
            "PASS artifact: {node} {kind} size={size_bytes} path={path} "
            "sha256={sha256} modified_utc={modified_utc}".format(**item)
        )
    print(f"PASS artifact validation: {len(artifacts)} non-empty outputs verified for this isolated build tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
