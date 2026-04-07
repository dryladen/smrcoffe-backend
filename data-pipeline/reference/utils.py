import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def resolve_materials_dir(date: str, project_root: str | None = None) -> str:
    """Return path to materials/{date}, falling back to the latest folder."""
    if project_root is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    mat_dir = os.path.join(project_root, "materials", date)
    if os.path.isdir(mat_dir):
        return mat_dir

    mat_base = os.path.join(project_root, "materials")
    if os.path.isdir(mat_base):
        folders = sorted(
            [
                f
                for f in os.listdir(mat_base)
                if os.path.isdir(os.path.join(mat_base, f))
            ],
            reverse=True,
        )
        if folders:
            fallback = os.path.join(mat_base, folders[0])
            print(f"ℹ️  materials/{date} not found — using fallback: {fallback}")
            return fallback

    sys.exit(f"❌  No materials directory found under: {mat_base}")


# ---------------------------------------------------------------------------
# plan.md reader
# ---------------------------------------------------------------------------


def parse_topics(plan_path: str) -> list[str]:
    """Return numbered topic titles from plan.md."""
    topics: list[str] = []
    with open(plan_path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\d+\.\s+(.+)", line.strip())
            if m:
                topics.append(m.group(1).strip())
    return topics


# ---------------------------------------------------------------------------
# urls.json I/O
# ---------------------------------------------------------------------------


def load_urls_json(filepath: str) -> list[dict]:
    """Load urls.json and return normalised topic dicts (topic, urls, query)."""
    if not os.path.exists(filepath):
        return []  # Return empty instead of exiting if we want to be more flexible

    with open(filepath, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return []

    return [
        {
            "topic": entry.get("topic", "Untitled"),
            "urls": entry.get("urls", []),
            "query": entry.get("query", []),
        }
        for entry in data
    ]


def save_urls_json(filepath: str, data: list[dict]) -> None:
    """Write data to filepath as pretty-printed JSON, creating dirs as needed."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def slugify_query(text: str) -> str:
    """Convert a phrase to a URL query string (space → +). e.g. 'foo bar' → 'foo+bar'."""
    text = text.split(":")[0].strip()
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip().replace(" ", "+")


def slugify_filename(value: str) -> str:
    """Convert a phrase to a kebab-case filename slug. e.g. 'Foo Bar!' → 'foo-bar'."""
    value = str(value)
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[-\s]+", "-", value)
    return value if value else "file"
