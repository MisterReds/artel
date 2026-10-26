

def test_concurrent_meta_reads_and_writes_from_two_instances(tmp_path):
    import json
    import threading
    import time
    from artel.project import DEFAULT_CONFIG, Project

    a = Project.create("race", DEFAULT_CONFIG, home=tmp_path)
    b = Project.open(a.slug, DEFAULT_CONFIG, home=tmp_path)
    errors: list = []
    stop = time.time() + 1.0

    def writer(p, tag):
        i = 0
        while time.time() < stop:
            try:
                p.set_meta(**{f"k_{tag}": i})
                i += 1
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    def reader(p):
        while time.time() < stop:
            try:
                assert isinstance(p.meta, dict)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    ts = [threading.Thread(target=writer, args=(a, "a")), threading.Thread(target=writer, args=(b, "b")),
          threading.Thread(target=reader, args=(a,)), threading.Thread(target=reader, args=(b,))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert isinstance(json.loads((a.root / "project.json").read_text(encoding="utf-8")), dict)
    assert not list(a.root.glob(".project.json.*.tmp"))
