#!/usr/bin/env python3
"""Validate and build the WorkBuddy expert package from the canonical Skill."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "workbuddy"
SKILL_SOURCE_DIR = ROOT / "human-writing"
MANIFEST_PATH = TEMPLATE_DIR / ".codebuddy-plugin" / "plugin.json"
AGENT_PATH = TEMPLATE_DIR / "agents" / "human-writing.md"
AVATAR_PATH = TEMPLATE_DIR / "avatars" / "expert.png"
PLUGIN_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUIRED_MANIFEST_FIELDS = {
    "name",
    "version",
    "description",
    "author",
    "agents",
    "skills",
    "expertType",
    "agentName",
    "displayName",
    "profession",
    "displayDescription",
    "avatar",
    "categoryId",
    "defaultInitPrompt",
    "plugin",
    "tags",
    "quickPrompts",
}


class ValidationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"Missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in {path}: {exc}") from exc
    require(isinstance(data, dict), "plugin.json must contain a JSON object")
    return data


def validate_localized(value: Any, field: str) -> None:
    require(isinstance(value, dict), f"{field} must be an object")
    require(set(value) >= {"en", "zh"}, f"{field} must contain en and zh")
    require(
        all(isinstance(value[lang], str) and value[lang].strip() for lang in ("en", "zh")),
        f"{field}.en and {field}.zh must be non-empty strings",
    )


def cjk_character_count(value: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", value))


def png_dimensions(path: Path) -> tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except FileNotFoundError as exc:
        raise ValidationError(f"Missing avatar: {path}") from exc
    require(header[:8] == PNG_SIGNATURE, f"Avatar must be a PNG: {path}")
    require(header[12:16] == b"IHDR", f"Avatar has an invalid PNG header: {path}")
    return struct.unpack(">II", header[16:24])


def validate_agent(path: Path, expected_name: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationError(f"Missing Agent definition: {path}") from exc

    require(text.startswith("---\n"), "Agent definition must start with YAML frontmatter")
    end_marker = text.find("\n---\n", 4)
    require(end_marker > 4, "Agent definition must close YAML frontmatter")
    frontmatter = text[4:end_marker]
    body = text[end_marker + 5 :].strip()

    require(
        re.search(rf"^name:\s*{re.escape(expected_name)}\s*$", frontmatter, re.MULTILINE)
        is not None,
        "Agent frontmatter name must match plugin.json agentName and the file name",
    )
    for field in ("description", "displayName", "profession"):
        require(
            re.search(rf"^{field}:\s*", frontmatter, re.MULTILINE) is not None,
            f"Agent frontmatter is missing {field}",
        )
    require(
        re.search(r"^tools:\s*", frontmatter, re.MULTILINE) is None,
        "Agent frontmatter must not declare tools",
    )
    require("- human-writing" in frontmatter, "Agent must preload the human-writing skill")
    require(bool(body), "Agent system prompt must not be empty")


def validate_manifest(manifest: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_MANIFEST_FIELDS - manifest.keys())
    require(not missing, f"plugin.json is missing fields: {', '.join(missing)}")

    name = manifest["name"]
    require(isinstance(name, str) and PLUGIN_NAME_PATTERN.fullmatch(name) is not None, "Invalid plugin name")
    require(manifest["plugin"] == name, "plugin must equal name")
    require(manifest["expertType"] == "agent", "expertType must be agent")
    require(manifest["agentName"] == "human-writing", "agentName must be human-writing")
    require(manifest["agents"] == ["./agents/human-writing.md"], "agents path is inconsistent")
    require(manifest["skills"] == ["./skills/human-writing"], "skills path is inconsistent")

    version = manifest["version"]
    require(isinstance(version, str) and SEMVER_PATTERN.fullmatch(version) is not None, "Invalid semantic version")
    skill_version = (SKILL_SOURCE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    require(version == skill_version, f"Plugin version {version} does not match Skill version {skill_version}")

    author = manifest["author"]
    require(isinstance(author, dict), "author must be an object")
    require(isinstance(author.get("name"), str) and author["name"].strip(), "author.name is required")
    require(isinstance(author.get("email"), str) and "@" in author["email"], "author.email is required")

    for field in ("displayName", "profession", "displayDescription", "defaultInitPrompt"):
        validate_localized(manifest[field], field)

    description_length = cjk_character_count(manifest["displayDescription"]["zh"])
    require(
        40 <= description_length <= 50,
        f"displayDescription.zh must contain 40-50 CJK characters; found {description_length}",
    )
    require(manifest["categoryId"] == "06-ContentCreative", "Unexpected categoryId")

    tags = manifest["tags"]
    require(isinstance(tags, list) and len(tags) == 3, "tags must contain exactly 3 items")
    for index, tag in enumerate(tags):
        validate_localized(tag, f"tags[{index}]")

    prompts = manifest["quickPrompts"]
    require(isinstance(prompts, list) and len(prompts) == 3, "quickPrompts must contain exactly 3 items")
    for index, prompt in enumerate(prompts):
        validate_localized(prompt, f"quickPrompts[{index}]")
    require(
        manifest["defaultInitPrompt"] == prompts[0],
        "defaultInitPrompt must exactly match the first quickPrompts item",
    )


def validate_source() -> dict[str, Any]:
    manifest = load_manifest(MANIFEST_PATH)
    validate_manifest(manifest)
    validate_agent(AGENT_PATH, manifest["agentName"])

    require((TEMPLATE_DIR / "README.md").is_file(), "WorkBuddy README.md is missing")
    for forbidden in ("hooks", "commands", ".lsp.json"):
        require(not (TEMPLATE_DIR / forbidden).exists(), f"Forbidden WorkBuddy path: {forbidden}")
    for required_skill_path in (
        SKILL_SOURCE_DIR / "SKILL.md",
        SKILL_SOURCE_DIR / "VERSION",
        SKILL_SOURCE_DIR / "references",
        SKILL_SOURCE_DIR / "scripts" / "check_prose.py",
    ):
        require(required_skill_path.exists(), f"Missing canonical Skill content: {required_skill_path}")

    width, height = png_dimensions(AVATAR_PATH)
    require((width, height) == (512, 512), f"Avatar must be 512x512; found {width}x{height}")
    require(AVATAR_PATH.stat().st_size <= 500 * 1024, "Avatar must be no larger than 500 KB")
    return manifest


def copy_skill(destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in {".DS_Store", "__pycache__"}}
        ignored.update(name for name in names if name.endswith(".pyc"))
        return ignored

    shutil.copytree(SKILL_SOURCE_DIR, destination, ignore=ignore)


def resolve_package_path(package_dir: Path, manifest_path: str) -> Path:
    relative = Path(manifest_path.removeprefix("./"))
    resolved = (package_dir / relative).resolve()
    try:
        resolved.relative_to(package_dir.resolve())
    except ValueError as exc:
        raise ValidationError(f"Manifest path escapes package root: {manifest_path}") from exc
    return resolved


def validate_built_package(package_dir: Path) -> None:
    manifest = load_manifest(package_dir / ".codebuddy-plugin" / "plugin.json")
    validate_manifest(manifest)
    validate_agent(resolve_package_path(package_dir, manifest["agents"][0]), manifest["agentName"])
    require(resolve_package_path(package_dir, manifest["avatar"]).is_file(), "Built package avatar is missing")
    for forbidden in ("hooks", "commands", ".lsp.json"):
        require(not (package_dir / forbidden).exists(), f"Built package contains forbidden path: {forbidden}")
    for skill_path in manifest["skills"]:
        require(
            (resolve_package_path(package_dir, skill_path) / "SKILL.md").is_file(),
            f"Built package Skill is missing SKILL.md: {skill_path}",
        )


def build_package(output_dir: Path) -> tuple[Path, Path]:
    manifest = validate_source()
    output_dir = output_dir.expanduser().resolve()
    package_dir = output_dir / manifest["name"]
    archive_path = output_dir / f"{manifest['name']}-{manifest['version']}.zip"

    output_dir.mkdir(parents=True, exist_ok=True)
    if package_dir.exists():
        shutil.rmtree(package_dir)
    if archive_path.exists():
        archive_path.unlink()

    shutil.copytree(TEMPLATE_DIR, package_dir)
    copy_skill(package_dir / "skills" / "human-writing")
    shutil.copy2(ROOT / "LICENSE", package_dir / "LICENSE")
    validate_built_package(package_dir)

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir))

    required_entries = {
        f"{manifest['name']}/.codebuddy-plugin/plugin.json",
        f"{manifest['name']}/agents/human-writing.md",
        f"{manifest['name']}/avatars/expert.png",
        f"{manifest['name']}/skills/human-writing/SKILL.md",
    }
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        missing = sorted(required_entries - names)
        require(not missing, f"Archive is missing entries: {', '.join(missing)}")
        forbidden = [
            name
            for name in names
            if "/.git/" in name
            or name.endswith(".DS_Store")
            or "/__pycache__/" in name
            or name.endswith(".pyc")
        ]
        require(not forbidden, f"Archive contains forbidden files: {', '.join(sorted(forbidden))}")

    return package_dir, archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist" / "workbuddy",
        help="Directory for the assembled expert folder and ZIP archive",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate source files without building the package",
    )
    args = parser.parse_args()

    try:
        manifest = validate_source()
        if args.check:
            print(f"WorkBuddy source validation passed for {manifest['name']} {manifest['version']}")
            return 0
        package_dir, archive_path = build_package(args.output_dir)
    except ValidationError as exc:
        print(f"WorkBuddy validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Validated package: {package_dir}")
    print(f"Built archive: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
