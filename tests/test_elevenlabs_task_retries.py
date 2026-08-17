import ast
from pathlib import Path


def test_audio_download_actor_has_explicit_bounded_retries():
    tasks_path = Path(__file__).resolve().parents[1] / "tasks.py"
    tree = ast.parse(tasks_path.read_text(encoding="utf-8"))
    actor = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "download_elevenlabs_audio_task"
    )
    decorator = next(
        item
        for item in actor.decorator_list
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "actor"
    )
    options = {item.arg: ast.literal_eval(item.value) for item in decorator.keywords}

    assert options["max_retries"] > 0
    assert options["min_backoff"] > 0
    assert options["max_backoff"] >= options["min_backoff"]
