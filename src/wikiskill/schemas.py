EDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["op", "target", "content"],
    "properties": {
        "op": {"enum": ["append", "replace", "insert_after"]},
        "target": {"type": "string"},
        "content": {"type": "string"},
    },
}

MAINTAINER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["create_patterns", "update_patterns", "update_index", "append_log"],
    "properties": {
        "create_patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "content"],
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
        "update_patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "edits"],
                "properties": {
                    "name": {"type": "string"},
                    "edits": {"type": "array", "items": EDIT_SCHEMA},
                },
            },
        },
        "update_index": {"type": "string"},
        "append_log": {"type": "string"},
    },
}

PROPOSER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "name", "skill_md", "purpose_md", "edits", "reason"],
    "properties": {
        "action": {"enum": ["create", "patch", "no_action"]},
        "name": {"type": "string"},
        "skill_md": {"type": "string"},
        "purpose_md": {"type": "string"},
        "edits": {"type": "array", "items": EDIT_SCHEMA},
        "reason": {"type": "string"},
    },
}
