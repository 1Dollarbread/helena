"""File tool behaviour: reads, edits, the safety properties, and search."""

from __future__ import annotations

import pytest

from helena_harness.tools.base import ToolError
from helena_harness.tools.files import (
    CreateProjectTool,
    DeletePathTool,
    EditFileTool,
    FindFilesTool,
    ListDirTool,
    ReadFileTool,
    SearchTextTool,
    TodoWriteTool,
    WriteFileTool,
)

SAMPLE = "alpha\nbeta\ngamma\ndelta\n"


async def test_create_project_writes_every_file(tool_ctx, workspace):
    result = await CreateProjectTool().run(
        {
            "files": [
                {"path": "package.json", "content": '{"name": "demo"}'},
                {"path": "src/index.js", "content": "console.log('hi');"},
                {"path": "README.md", "content": "# demo"},
            ]
        },
        tool_ctx,
    )
    assert result.ok
    assert (workspace / "package.json").read_text() == '{"name": "demo"}'
    assert (workspace / "src/index.js").read_text() == "console.log('hi');"
    assert (workspace / "README.md").read_text() == "# demo"
    assert len(result.meta["paths"]) == 3


async def test_create_project_respects_cwd(tool_ctx, workspace):
    await CreateProjectTool().run(
        {"cwd": "backend", "files": [{"path": "main.py", "content": "print('hi')"}]},
        tool_ctx,
    )
    assert (workspace / "backend" / "main.py").read_text() == "print('hi')"


async def test_create_project_requires_files(tool_ctx):
    with pytest.raises(ToolError, match="non-empty"):
        await CreateProjectTool().run({"files": []}, tool_ctx)


async def test_create_project_rejects_absolute_paths(tool_ctx):
    with pytest.raises(ToolError, match="relative"):
        await CreateProjectTool().run({"files": [{"path": "/etc/passwd", "content": "x"}]}, tool_ctx)


async def test_create_project_marks_files_as_read(tool_ctx, workspace):
    """Written files should be immediately edit_file-able without a separate
    read_file first — same guarantee write_file already gives.
    """
    result = await CreateProjectTool().run(
        {"files": [{"path": "a.txt", "content": "one\n"}]}, tool_ctx
    )
    assert result.ok
    edited = await EditFileTool().run(
        {"path": "a.txt", "old_string": "one", "new_string": "two"}, tool_ctx
    )
    assert edited.ok


async def test_read_file_numbers_lines(tool_ctx, workspace):
    (workspace / "a.txt").write_text(SAMPLE)
    result = await ReadFileTool().run({"path": "a.txt"}, tool_ctx)
    assert result.ok
    assert "     1\talpha" in result.content
    assert "lines 1-4 of 4" in result.content


async def test_read_file_offset_and_limit(tool_ctx, workspace):
    (workspace / "a.txt").write_text(SAMPLE)
    result = await ReadFileTool().run({"path": "a.txt", "offset": 2, "limit": 2}, tool_ctx)
    assert "beta" in result.content and "delta" not in result.content
    assert "call again with offset=4" in result.content


async def test_read_file_rejects_missing_and_directories(tool_ctx, workspace):
    with pytest.raises(ToolError, match="No such file"):
        await ReadFileTool().run({"path": "nope.txt"}, tool_ctx)
    (workspace / "sub").mkdir()
    with pytest.raises(ToolError, match="use list_dir"):
        await ReadFileTool().run({"path": "sub"}, tool_ctx)


async def test_read_file_points_images_at_the_vision_tool(tool_ctx, workspace):
    (workspace / "shot.png").write_bytes(b"\x89PNG\r\n")
    with pytest.raises(ToolError, match="analyze_image"):
        await ReadFileTool().run({"path": "shot.png"}, tool_ctx)


async def test_paths_are_confined_to_the_workspace(tool_ctx):
    with pytest.raises(ToolError, match="outside the workspace"):
        await ReadFileTool().run({"path": "/etc/passwd"}, tool_ctx)


async def test_confinement_can_be_lifted(tool_ctx, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("visible")
    tool_ctx.config.allow_outside_workspace = True
    result = await ReadFileTool().run({"path": str(outside)}, tool_ctx)
    assert "visible" in result.content


async def test_write_file_creates_parents_and_reports_diff(tool_ctx, workspace):
    result = await WriteFileTool().run({"path": "deep/nested/a.py", "content": "x = 1\n"}, tool_ctx)
    assert result.ok
    assert (workspace / "deep/nested/a.py").read_text() == "x = 1\n"
    assert "Created" in result.content
    assert "+1/-0" in result.content


async def test_edit_requires_a_prior_read(tool_ctx, workspace):
    (workspace / "a.py").write_text("value = 1\n")
    with pytest.raises(ToolError, match="Read .* before editing"):
        await EditFileTool().run(
            {"path": "a.py", "old_string": "value = 1", "new_string": "value = 2"}, tool_ctx
        )


async def test_edit_replaces_once(tool_ctx, workspace):
    path = workspace / "a.py"
    path.write_text("value = 1\nother = 2\n")
    await ReadFileTool().run({"path": "a.py"}, tool_ctx)
    result = await EditFileTool().run(
        {"path": "a.py", "old_string": "value = 1", "new_string": "value = 42"}, tool_ctx
    )
    assert result.ok
    assert path.read_text() == "value = 42\nother = 2\n"
    assert result.meta["diff"].startswith("---")


async def test_edit_refuses_ambiguous_match(tool_ctx, workspace):
    (workspace / "a.py").write_text("x = 1\nx = 1\n")
    await ReadFileTool().run({"path": "a.py"}, tool_ctx)
    with pytest.raises(ToolError, match="appears 2 times"):
        await EditFileTool().run({"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, tool_ctx)


async def test_edit_replace_all(tool_ctx, workspace):
    path = workspace / "a.py"
    path.write_text("x = 1\nx = 1\n")
    await ReadFileTool().run({"path": "a.py"}, tool_ctx)
    result = await EditFileTool().run(
        {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, tool_ctx
    )
    assert result.ok and path.read_text() == "x = 2\nx = 2\n"


async def test_edit_reports_a_missing_match_clearly(tool_ctx, workspace):
    (workspace / "a.py").write_text("x = 1\n")
    await ReadFileTool().run({"path": "a.py"}, tool_ctx)
    with pytest.raises(ToolError, match="must match exactly"):
        await EditFileTool().run({"path": "a.py", "old_string": "y = 9", "new_string": "z"}, tool_ctx)


async def test_edit_refuses_when_the_file_changed_underneath(tool_ctx, workspace):
    path = workspace / "a.py"
    path.write_text("x = 1\n")
    await ReadFileTool().run({"path": "a.py"}, tool_ctx)
    # Something else touches the file after the read.
    import os, time
    path.write_text("x = 99\n")
    os.utime(path, (time.time() + 5, time.time() + 5))
    with pytest.raises(ToolError, match="changed on disk"):
        await EditFileTool().run({"path": "a.py", "old_string": "x = 99", "new_string": "x = 1"}, tool_ctx)


async def test_delete_file_and_refuse_nonempty_directory(tool_ctx, workspace):
    (workspace / "gone.txt").write_text("bye")
    result = await DeletePathTool().run({"path": "gone.txt"}, tool_ctx)
    assert result.ok and not (workspace / "gone.txt").exists()

    (workspace / "full").mkdir()
    (workspace / "full/keep.txt").write_text("x")
    with pytest.raises(ToolError, match="not empty"):
        await DeletePathTool().run({"path": "full"}, tool_ctx)


async def test_list_dir_skips_dependency_directories(tool_ctx, workspace):
    (workspace / "src").mkdir()
    (workspace / "src/app.py").write_text("x")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules/junk.js").write_text("x")
    result = await ListDirTool().run({"path": ".", "depth": 2}, tool_ctx)
    assert "app.py" in result.content
    assert "node_modules" not in result.content


async def test_find_files_by_glob(tool_ctx, workspace):
    (workspace / "src").mkdir()
    (workspace / "src/a.py").write_text("x")
    (workspace / "src/b.js").write_text("x")
    result = await FindFilesTool().run({"pattern": "**/*.py"}, tool_ctx)
    assert "src/a.py" in result.content and "b.js" not in result.content


async def test_search_text_reports_path_and_line(tool_ctx, workspace):
    (workspace / "a.py").write_text("import os\ndef handler():\n    return 1\n")
    (workspace / "b.md").write_text("handler documentation\n")
    result = await SearchTextTool().run({"pattern": r"def handler", "glob": "*.py"}, tool_ctx)
    assert "a.py:2:def handler():" in result.content
    assert "b.md" not in result.content


async def test_search_text_no_matches_is_not_an_error(tool_ctx, workspace):
    (workspace / "a.py").write_text("nothing here\n")
    result = await SearchTextTool().run({"pattern": "zzz"}, tool_ctx)
    assert result.ok and "No matches" in result.content


async def test_search_text_rejects_bad_regex(tool_ctx, workspace):
    with pytest.raises(ToolError, match="Invalid regular expression"):
        await SearchTextTool().run({"pattern": "([unclosed"}, tool_ctx)


async def test_todo_write_updates_context(tool_ctx):
    result = await TodoWriteTool().run(
        {"todos": [{"task": "one", "status": "completed"}, {"task": "two", "status": "in_progress"}]},
        tool_ctx,
    )
    assert result.ok
    assert [t["task"] for t in tool_ctx.todos] == ["one", "two"]
    assert result.display == "1/2 done"
