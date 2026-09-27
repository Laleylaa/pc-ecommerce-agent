import json

from scripts.import_pc_part_dataset import infer_part_type, load_records, resolve_import_targets


def test_infers_part_type_from_dataset_filename() -> None:
    assert infer_part_type("mouse.json") == "mouse"
    assert infer_part_type("headphones.jsonl") == "headphones"


def test_load_records_supports_json_arrays_and_jsonl(tmp_path) -> None:
    json_path = tmp_path / "mouse.json"
    json_path.write_text(json.dumps([{"name": "A", "price": 1}]), encoding="utf-8")
    jsonl_path = tmp_path / "keyboard.jsonl"
    jsonl_path.write_text('{"name": "B", "price": 2}\n\n', encoding="utf-8")

    assert load_records(json_path) == [{"name": "A", "price": 1}]
    assert load_records(jsonl_path) == [{"name": "B", "price": 2}]


def test_resolve_import_targets_defaults_to_core_peripherals(tmp_path) -> None:
    for name in ["mouse.json", "keyboard.json", "headphones.json", "cpu.json"]:
        (tmp_path / name).write_text("[]", encoding="utf-8")

    targets = resolve_import_targets(tmp_path, part_types=None)

    assert [(target.part_type, target.path.name) for target in targets] == [
        ("headphones", "headphones.json"),
        ("keyboard", "keyboard.json"),
        ("mouse", "mouse.json"),
    ]
