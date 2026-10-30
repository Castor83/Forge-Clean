"""Reviewed reproduction contract; not an unrestricted F-001 builder benchmark."""

EXPECTED_SOURCE = '''def analyze_directory(root_id):
    snapshot = read_snapshot(root_id)
    files = []
    ok_files = 0
    error_files = 0
    total_size_bytes = 0
    total_lines = 0
    for record in snapshot["files"]:
        raw = record["raw"]
        status = record["error"]
        size_bytes = None
        line_count = None
        if status is None:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                status = "invalid_utf8"
            else:
                status = "ok"
                size_bytes = len(raw)
                line_count = len(text.splitlines())
        if status == "ok":
            ok_files += 1
            total_size_bytes += size_bytes
            total_lines += line_count
        else:
            error_files += 1
        files.append({"name": record["name"], "status": status,
                      "size_bytes": size_bytes, "line_count": line_count})
    status = "ok"
    if error_files:
        status = "partial"
    if snapshot["root_error"] is not None:
        status = "error"
    return report_json({"schema_version": 1, "status": status,
                        "root_error": snapshot["root_error"], "files": files,
                        "summary": {"ok_files": ok_files, "error_files": error_files,
                                    "total_size_bytes": total_size_bytes,
                                    "total_lines": total_lines}})
'''
