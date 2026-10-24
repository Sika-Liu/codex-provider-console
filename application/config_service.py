"""Pure TOML transformations shared by provider activation and configuration editing."""

from __future__ import annotations

import json
import re

def toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def remove_provider_sections(config: str) -> str:
    output: list[str] = []
    skipping = False
    for line in config.splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            skipping = section.group(1).startswith("model_providers.")
        if skipping:
            continue
        if re.match(r"^\s*(model_provider|model|model_catalog_json|experimental_bearer_token)\s*=", line):
            continue
        output.append(line)
    return "\n".join(output).strip()


ROOT_CONFIG_KEYS = {
    "model",
    "model_catalog_json",
    "model_provider",
    "model_reasoning_effort",
    "sandbox_mode",
    "approval_policy",
    "file_opener",
    "web_search",
    "suppress_unstable_features_warning",
    "experimental_bearer_token",
}


def canonicalize_base_config(config: str) -> str:
    """Keep one copy of each existing TOML table and discard misplaced root keys."""
    output: list[str] = []
    seen_sections: set[str] = set()
    current_section = ""
    skipping_section = False

    for line in remove_provider_sections(config).splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            current_section = section.group(1)
            # Repeated [features] blocks are intentionally kept here so
            # normalize_features_sections can retain their last effective key.
            skipping_section = current_section in seen_sections and current_section != "features"
            if not skipping_section:
                seen_sections.add(current_section)
                output.append(line)
            continue
        if skipping_section:
            continue
        assignment = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        if current_section and assignment and assignment.group(1) in ROOT_CONFIG_KEYS:
            continue
        output.append(line)

    return normalize_features_sections("\n".join(output).strip())


def split_toml_document(config: str) -> tuple[str, str]:
    """Separate root assignments from TOML table declarations."""
    lines = config.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\[[^]]+]\s*$", line):
            return "\n".join(lines[:index]).strip(), "\n".join(lines[index:]).strip()
    return config.strip(), ""


def normalize_features_sections(config: str) -> str:
    """Merge repeated [features] tables left by older panel versions."""
    output: list[str] = []
    feature_lines: list[str] = []
    in_features = False
    features_found = False
    top_level_keys = {"model", "model_catalog_json", "model_provider", "model_reasoning_effort"}

    for line in config.splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            in_features = section.group(1) == "features"
            if in_features:
                features_found = True
                continue
        if in_features:
            assignment = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
            # Older malformed output could leave root model settings after a
            # [features] header. Return those settings to the document root.
            if assignment and assignment.group(1) in top_level_keys:
                in_features = False
        if in_features:
            feature_lines.append(line)
        else:
            output.append(line)

    if not features_found:
        return "\n".join(output).strip()

    # TOML rejects duplicate keys too. Preserve the last value, matching the
    # effective value users expect after repeatedly saving the same setting.
    seen_keys: set[str] = set()
    normalized_reversed: list[str] = []
    for line in reversed(feature_lines):
        assignment = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        if assignment:
            key = assignment.group(1)
            if key in seen_keys:
                continue
            seen_keys.add(key)
        normalized_reversed.append(line)
    normalized_features = list(reversed(normalized_reversed))

    result = output
    while result and not result[-1].strip():
        result.pop()
    if result:
        result.append("")
    result.append("[features]")
    result.extend(normalized_features)
    return "\n".join(result).strip()


def set_goals_feature(config: str, enabled: bool) -> str:
    config = normalize_features_sections(config)
    output: list[str] = []
    in_features = False
    features_found = False
    goals_written = False
    for line in config.splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            if in_features and not goals_written:
                output.append(f"goals = {str(enabled).lower()}")
                goals_written = True
            in_features = section.group(1) == "features"
            features_found = features_found or in_features
            output.append(line)
            continue
        if in_features and re.match(r"^\s*goals\s*=", line):
            if not goals_written:
                output.append(f"goals = {str(enabled).lower()}")
                goals_written = True
            continue
        output.append(line)
    if not goals_written:
        if not features_found:
            if output and output[-1].strip():
                output.append("")
            output.append("[features]")
        output.append(f"goals = {str(enabled).lower()}")
    return "\n".join(output).strip()


def remove_goals_feature(config: str) -> str:
    config = normalize_features_sections(config)
    output: list[str] = []
    in_features = False
    for line in config.splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            in_features = section.group(1) == "features"
        if in_features and re.match(r"^\s*goals\s*=", line):
            continue
        output.append(line)
    return "\n".join(output).strip()


